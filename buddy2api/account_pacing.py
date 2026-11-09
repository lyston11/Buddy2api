"""账号级出站节流：让每个账号的请求形态留在「一个人在用客户端」的量级。

## 为什么要这个

上游对 chat 的风控判定看的是**账号行为画像**，不是单个请求的内容。2026-11-09
事后复盘（库里有完整日志）：

- 10-28 那一整天，6 个国际账号每个都跑了 **1400+ 次请求**，单账号并发峰值 **8~10**，
  相邻请求间隔中位数 **4 秒**，活跃时段覆盖凌晨 2 点到 23 点、连续 19 个小时。
  这是同一个人一天里不可能产生的形态 —— 一个真实客户端同时只有一两个会话在跑。
- 作为对照，国内账号在正常使用的日子里单账号并发峰值是 **3~4**。

上游给国际账号的最终答复是 `403 code=11140`，`displayMsg` 明确写着
「Your account has been restricted due to unusual activity / 账号存在异常行为，
已被限制使用」，`displayTips` 还给了申诉入口 —— 这是**账号行为**维度的处罚，
不是内容审核（9-26 那批 11140 的文案是「内容未通过安全审核」，两者 displayMsg
完全不同，别混为一谈）。

网关这一侧能做的、也确实缺失的，就是不让单账号在时间上变成一台压测机。
节流放在**拿到账号之后、发请求之前**：超并发就先排队，而不是把突发全打上去。

## 设计取舍

- **只限并发，不限总量**。总量是用户自己的用量决策（他有多个账号就是为了摊开），
  硬压总量会让正常使用变慢；而并发是「同一个人能开几个客户端」的直接代理，
  真实客户端的并发天然很低。
- **排队而不是拒绝**。拒绝会让客户端看到 429，而这里的限制纯粹是我们自己的保护，
  不该变成对外错误。排队有超时上限，超时就放行 —— 宁可偶尔放行一次，也不要让
  请求凭空卡死。
- **按账号而不是按模型**。风控画像的主体是账号，同一个账号换模型照样是同一个画像。
- **默认值取保守但不影响日常**：并发 3。实测国内账号正常使用峰值是 3~4，所以这条
  线正好卡在「正常使用不受影响」上；国际账号那 8~10 会被压到 3。
  用 `CB_GATEWAY_ACCOUNT_MAX_INFLIGHT` 覆盖，`0`/`off` 关掉整个机制。
"""

from __future__ import annotations

import asyncio
import os
import sys
from contextlib import asynccontextmanager
from typing import AsyncIterator

_DISABLED_VALUES = {"off", "none", "false", "disable", "0", ""}

DEFAULT_MAX_INFLIGHT = 3
# 排队上限。超时即放行：这是我们自己的保护，不该让请求无限期挂住。
DEFAULT_QUEUE_TIMEOUT = float(os.environ.get("CB_GATEWAY_ACCOUNT_QUEUE_TIMEOUT", "300"))

# 账号 id → (信号量, 事件循环)。绑定 loop 是因为测试会反复新建 loop
# （asyncio.run），把上一个 loop 的 Semaphore 拿来 await 会直接报错。
_semaphores: dict[int, tuple[asyncio.Semaphore, asyncio.AbstractEventLoop]] = {}
_inflight: dict[int, int] = {}


def max_inflight() -> int:
    """单账号允许的在途请求数；0 表示关闭节流。"""
    raw = os.environ.get("CB_GATEWAY_ACCOUNT_MAX_INFLIGHT")
    if raw is None:
        return DEFAULT_MAX_INFLIGHT
    text = str(raw).strip().lower()
    if text in _DISABLED_VALUES:
        return 0
    try:
        return max(0, int(text))
    except (TypeError, ValueError):
        return DEFAULT_MAX_INFLIGHT


def inflight(account_id: int) -> int:
    """当前在途请求数（测试与排障用）。"""
    return _inflight.get(int(account_id), 0)


def reset() -> None:
    """清空全部计数（测试用）。"""
    _semaphores.clear()
    _inflight.clear()


def _semaphore(account_id: int, limit: int) -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    entry = _semaphores.get(account_id)
    if entry is None or entry[1] is not loop:
        entry = (asyncio.Semaphore(limit), loop)
        _semaphores[account_id] = entry
    return entry[0]


@asynccontextmanager
async def slot(account_id: int) -> AsyncIterator[None]:
    """占用一个在途名额；满了就在队列里等（超时放行）。

    用法与 `async with client_scope(...)` 一致，只包住「发请求 + 读完响应」这段。
    """
    limit = max_inflight()
    aid = int(account_id)
    if limit <= 0:
        yield
        return

    sem = _semaphore(aid, limit)
    acquired = False
    try:
        try:
            await asyncio.wait_for(sem.acquire(), DEFAULT_QUEUE_TIMEOUT)
            acquired = True
        except asyncio.TimeoutError:
            # 排队超时：放行。宁可偶尔多一条并发，也不让请求无声挂死。
            print(
                f"[pacing] 账号 {aid} 排队超过 {int(DEFAULT_QUEUE_TIMEOUT)}s，本次放行",
                file=sys.stderr,
            )
        _inflight[aid] = _inflight.get(aid, 0) + 1
        yield
    finally:
        remaining = _inflight.get(aid, 1) - 1
        if remaining > 0:
            _inflight[aid] = remaining
        else:
            _inflight.pop(aid, None)
        if acquired:
            sem.release()


# ============================================================
# 日用量观测（只告警 / 供选路参考，不硬拦）
# ============================================================

# 单个账号「一天成功服务多少请求」在上游眼里是异常行为的直接证据：
#   国际账号被封那天（10-28）：1411 ~ 1424 次
#   国内账号正常使用日（11-05）：350 ~ 612 次
# 取 800 作为分界：明显高于正常使用、又低于被封那天的量级。超了只记一条告警，
# 不改路由 —— 总量是用户自己的用量决策，网关不该替他做取舍；但至少要有可见性，
# 而不是等封号了才回头发现某个号当天跑了一千四百次。
DEFAULT_DAILY_SOFT_LIMIT = 800

_warned_today: set[int] = set()


def daily_soft_limit() -> int:
    """单账号每日软上限；0 表示不告警。

    每次现读环境变量（而不是 import 时读一次）：这样测试与运行时改配置都能立刻生效，
    也不会出现「模块已导入，改动被忽略」的静默陷阱。
    """
    raw = os.environ.get("CB_GATEWAY_ACCOUNT_DAILY_SOFT_LIMIT")
    if raw is None:
        return DEFAULT_DAILY_SOFT_LIMIT
    try:
        return max(0, int(str(raw).strip()))
    except (TypeError, ValueError):
        return DEFAULT_DAILY_SOFT_LIMIT


def reset_daily_warnings() -> None:
    """清空今日已告警的账号（测试与跨天用）。"""
    _warned_today.clear()


def note_daily_usage(account_id: int, count: int) -> Optional[str]:
    """账号当日请求数越过软上限时返回一行告警（同一账号每天只报一次）。

    返回值为 None 表示无需告警。调用方负责打印/展示，本模块不做 I/O 之外的决策。
    """
    limit = daily_soft_limit()
    aid = int(account_id)
    if limit <= 0 or int(count) < limit or aid in _warned_today:
        return None
    _warned_today.add(aid)
    return (
        f"[pacing] 账号 {aid} 今天已成功服务 {int(count)} 次请求（软上限 {limit}）。"
        f"上游的账号行为画像会看这个量级：实测被封的国际账号当日在 1400+ 次，"
        f"而正常使用的国内账号在 350~612 次。建议把负载摊到其它账号上。"
    )
