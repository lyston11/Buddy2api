"""账号级出站节流：别再让单个账号在上游眼里变成一台压测机。

## 事故背景（2026-11-09）

用户报「国际版账号全都被封禁了」。逐个账号实测：`POST /v2/chat/completions` 一律
`403 code=11140`，而同一个 token 打 `get-user-resource-summary` 仍是 200 ——
**账号本身没被注销，是 chat 被限制**。上游给的 `displayMsg` 是：

    Your account has been restricted due to unusual activity
    账号存在异常行为，已被限制使用

`displayTips` 还给了申诉入口。这是**账号行为**维度的处罚，与 9-26 那批
`11140`（`displayMsg` 是「内容未通过安全审核」）是两回事，别混为一谈。

## 从库里能复现的行为画像

对齐同一批账号在 10-28（被封前一周的高峰日）与国内账号正常使用日的真实日志：

| 指标 | 国际 6 个账号（10-28） | 国内 3 个账号（11-05） |
| --- | --- | --- |
| 单账号当日请求 | 1411 ~ 1424 | 350 ~ 612 |
| 单账号并发峰值 | **8 ~ 10** | **3 ~ 4** |
| 相邻请求间隔中位数 | **4 秒** | 53 ~ 95 秒 |
| 活跃时段跨度 | 02:00–23:00（连跑 19 小时） | 09:00–18:00 |

一个真实客户端同时只开一两个会话，不可能对同一个账号压出 8~10 并发、4 秒一条、
连跑 19 小时。这一侧网关以前**完全没有**任何账号级节奏限制，把用户的批量调用原样
打在单个账号上。

## 这一层做什么

`account_pacing.slot(account_id)` 在**拿到账号之后、发请求之前**占一个在途名额：
超过上限就排队（有超时，超时放行），名额覆盖整个流式响应。默认并发 3 —— 正好压在
国内账号正常使用的峰值上，所以正常使用不受影响，而 8~10 那种压测形态会被摊平。

只限并发、不限总量：总量是用户自己的用量决策，硬压会让正常使用变慢；而并发才是
「同一个人能开几个客户端」的代理指标。
"""

import asyncio
import time

import pytest

import buddy2api.account_pacing as pacing

ENV_LIMIT = "CB_GATEWAY_ACCOUNT_MAX_INFLIGHT"


@pytest.fixture(autouse=True)
def _reset_pacing(monkeypatch):
    monkeypatch.delenv(ENV_LIMIT, raising=False)
    pacing.reset()
    yield
    pacing.reset()


def test_default_limit_matches_domestic_normal_peak():
    """默认并发上限取 3：实测国内账号正常使用峰值就是 3~4，卡在这里不影响日常。"""
    assert pacing.max_inflight() == 3


def test_limit_can_be_overridden_and_disabled(monkeypatch):
    monkeypatch.setenv(ENV_LIMIT, "6")
    assert pacing.max_inflight() == 6

    for value in ("0", "off", "none", "false", "disable", ""):
        monkeypatch.setenv(ENV_LIMIT, value)
        assert pacing.max_inflight() == 0, f"{value!r} 应当关掉节流"

    monkeypatch.setenv(ENV_LIMIT, "不是数字")
    assert pacing.max_inflight() == 3, "脏值回落到默认值，不能变成 0（等于静默关闭）"


def test_disabled_limit_is_a_pass_through(monkeypatch):
    monkeypatch.setenv(ENV_LIMIT, "off")
    seen = []

    async def run():
        async with pacing.slot(1):
            seen.append(pacing.inflight(1))

    asyncio.run(run())
    assert seen == [0], "关掉节流时连计数都不该动"


def test_concurrent_requests_queue_instead_of_bursting(monkeypatch):
    """同一账号第 4 个请求要排队，直到前面有名额释放。

    这就是「别再压出 8~10 并发」的核心断言：把突发摊成排队。
    """
    monkeypatch.setenv(ENV_LIMIT, "3")
    peak = 0
    order: list[str] = []

    async def worker(label: str):
        nonlocal peak
        async with pacing.slot(7):
            peak = max(peak, pacing.inflight(7))
            order.append(f"start-{label}")
            await asyncio.sleep(0.05)
            order.append(f"end-{label}")

    async def run():
        await asyncio.gather(*(worker(str(i)) for i in range(9)))

    asyncio.run(run())

    assert peak == 3, f"在途请求峰值应被压到 3，实际 {peak}"
    assert len(order) == 18
    # 前三个一定是最先开始的（排队不打乱顺序）
    assert order[:3] == ["start-0", "start-1", "start-2"]


def test_different_accounts_do_not_block_each_other(monkeypatch):
    """节流按账号隔离：账号 A 排满不该拖住账号 B —— 多账号摊负载正是用户的用法。"""
    monkeypatch.setenv(ENV_LIMIT, "1")
    events: list[str] = []

    async def hold(aid: int, label: str):
        async with pacing.slot(aid):
            events.append(label)
            await asyncio.sleep(0.05)

    async def run():
        await asyncio.gather(
            hold(1, "a"), hold(1, "b"),  # 同账号：第二个必须等
            hold(2, "c"), hold(2, "d"),
        )

    asyncio.run(run())

    # a 与 c 是各自账号的第一个，必须都立刻拿到名额
    assert {"a", "c"} <= set(events[:2])


def test_slot_is_released_on_exception(monkeypatch):
    """请求抛错必须释放名额，否则该账号会被永久卡死。"""
    monkeypatch.setenv(ENV_LIMIT, "1")

    async def boom():
        async with pacing.slot(5):
            raise RuntimeError("upstream exploded")

    async def run():
        with pytest.raises(RuntimeError):
            await boom()
        assert pacing.inflight(5) == 0, "异常路径也必须把名额还回去"
        # 还回去之后同一个账号还能继续用
        async with pacing.slot(5):
            assert pacing.inflight(5) == 1

    asyncio.run(run())


def test_queue_timeout_lets_request_through(monkeypatch):
    """排队超时就放行：这是我们自己的保护，不该让请求无声挂死。"""
    monkeypatch.setenv(ENV_LIMIT, "1")
    monkeypatch.setattr(pacing, "DEFAULT_QUEUE_TIMEOUT", 0.05)
    entered: list[str] = []

    async def hold():
        async with pacing.slot(9):
            entered.append("holder")
            await asyncio.sleep(0.2)

    async def run():
        task = asyncio.ensure_future(hold())
        await asyncio.sleep(0.01)
        # holder 占满唯一名额，这一个必然等超时；超时后放行而不是抛错
        async with pacing.slot(9):
            entered.append("late")
        await task

    asyncio.run(run())

    assert entered == ["holder", "late"], "超时后必须放行，不能抛异常也不能永远卡住"


def test_counter_returns_to_zero_after_burst(monkeypatch):
    """一轮突发结束后计数必须归零，不能有泄漏。"""
    monkeypatch.setenv(ENV_LIMIT, "2")

    async def run():
        for _ in range(20):
            async with pacing.slot(3):
                pass

    asyncio.run(run())
    assert pacing.inflight(3) == 0


def test_semaphore_is_rebound_when_event_loop_changes(monkeypatch):
    """每个事件循环一套信号量。

    asyncio.Semaphore 绑定创建时的事件循环；测试与 CLI 会反复新建 loop，
    复用旧 loop 的对象会直接抛「attached to a different loop」。
    """
    monkeypatch.setenv(ENV_LIMIT, "2")

    async def use():
        async with pacing.slot(4):
            return pacing.inflight(4)

    for _ in range(3):
        assert asyncio.run(use()) == 1


# ---------------------------------------------------------------- 日用量观测

def test_daily_usage_warning_fires_once_per_day(monkeypatch):
    """越过日用量软上限时告警，同一账号每天只报一次（避免刷屏）。"""
    monkeypatch.setenv("CB_GATEWAY_ACCOUNT_DAILY_SOFT_LIMIT", "800")
    pacing.reset_daily_warnings()

    assert pacing.note_daily_usage(1, 799) is None
    note = pacing.note_daily_usage(1, 800)
    assert note and "800" in note
    assert pacing.note_daily_usage(1, 1500) is None, "同一账号同一天只报一次"
    # 不同账号各自独立
    assert pacing.note_daily_usage(2, 900)


def test_daily_soft_limit_can_be_disabled(monkeypatch):
    monkeypatch.setenv("CB_GATEWAY_ACCOUNT_DAILY_SOFT_LIMIT", "0")
    pacing.reset_daily_warnings()
    assert pacing.note_daily_usage(1, 99999) is None


def test_daily_soft_limit_default_sits_between_normal_and_banned():
    """默认分界 800：正常使用（350~612）不触发，被封那天的量级（1400+）必然触发。"""
    assert pacing.daily_soft_limit() == 800
    assert 612 < 800 < 1411
