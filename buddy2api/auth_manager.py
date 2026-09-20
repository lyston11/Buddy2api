"""
auth_manager.py — 多账号凭据管理

功能：
  - 从本机 auth 文件扫描导入账号
  - 手动添加账号（粘贴 auth JSON）
  - Token 自动刷新（提前 60s 判定过期）
  - 账号粘性路由（优先级优先，同级尽量固定账号）
  - 凭据缓存与线程安全
"""

import asyncio
import contextvars
import json
import os
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx

import buddy2api.database as db
import buddy2api.fingerprint as fingerprint
import buddy2api.sites as sites

BACKEND = "https://copilot.tencent.com"
DEFAULT_DOMAIN = "www.codebuddy.cn"

# 官方 Work Buddy / CodeBuddy CLI 客户端指纹（见 fingerprint.py）。
# 兼容保留 CB_GATEWAY_USER_AGENT 覆盖；如上游不接受新 UA，
# 设 CB_GATEWAY_USER_AGENT=codebuddy2openai/2.0 可回退到历史 UA。
USER_AGENT = fingerprint.user_agent()

_lock = threading.Lock()
_token_locks: dict[int, asyncio.Lock] = {}
_token_locks_guard = threading.Lock()
_route_lock = threading.Lock()
# 键是 (provider, model) 的组合（见 _sticky_key），不是单纯的 provider：
# 国内模型与国际模型能用的账号集不同，各留各的粘性槽才不会互相顶掉。
_sticky_account_id: dict[str, int] = {}
_failure_lock = threading.Lock()
_account_failures: dict[int, tuple[int, float]] = {}
# 按 (账号, 模型) 的限流冷却。与 _account_failures 分开存：前者是「这个账号这个
# 模型暂时没额度」，不影响该账号服务其它模型；后者是账号级别的失败退避。
_rate_limit_lock = threading.Lock()
_model_rate_limits: dict[tuple[int, str], float] = {}

# 429（WorkBuddy code 6004「使用量已超出频率限制」）不是瞬时抖动，而是额度用尽：
# 上游报文明确说「您也可以切换其他模型继续使用」。实测 2026-09-20：
#   - 账号在 deepseek 上吃 429 后，十几分钟内仍在正常服务 glm-5.3-flash；
#   - 重置窗口是分钟到小时级（国内账号回「将在 18:26 重置」，距报错 1h44m）。
# 所以限流必须【按模型】冷却，不能按账号：
#   - 按账号冷却会把该账号在其它模型上的正常服务一起堵掉（回归）；
#   - 用通用的 30s×2ⁿ、封顶 300s 退避又太短 —— 冷却一过，被限流的账号就以
#     「零负载」的身份回到候选池最前面（负载只统计 2xx），把健康账号挤出重试
#     窗口。这正是「四个老账号限额后，新账号明明可用却请求不到」的成因。
# 这里取一个保守下限（小于上游给出的真实重置窗口）：到期后再试一次，若仍被限
# 就重新计时，自愈且不会像「按报文直接封到几小时后」那样误判。
RATE_LIMIT_COOLDOWN_SECONDS = int(
    os.environ.get("CB_GATEWAY_RATE_LIMIT_COOLDOWN_SECONDS", "900")
)

# 换号重试的账号数上限。必须大于「可用账号数」才能保证轮到健康账号：
# 3 个账号时试 3 个够用，但 6 个账号时限额账号会先把 3 次机会占满。
# pick_account 在候选耗尽时返回 None，循环会提前 break，所以给足上限是安全的
# —— 实际尝试次数仍受可用账号数约束，不会空转。
MAX_ACCOUNT_ATTEMPTS = int(os.environ.get("CB_GATEWAY_MAX_ACCOUNT_ATTEMPTS", "8"))

# API Key 级账号绑定：0 = 不绑定（走优先级 + 粘性调度），>0 = 只用该 accounts.id。
# 用 contextvars 而不是给 pick_account 加参数：这样 providers/* 与 proxy.py 的调用
# 签名都不用改，由 server.py 在一次请求的入口处写入，本次请求的异步生成器与
# run_in_threadpool 都会继承它（流式响应里选号也生效）。
_pinned_account_id: contextvars.ContextVar[int] = contextvars.ContextVar(
    "cb_pinned_account_id", default=0
)

# 连续鉴权失败计数（401/403 或刷新被拒）。只有达到阈值才会触发复核/失效，
# 避免一次瞬时 401 就把账号永久停用。
_auth_failure_lock = threading.Lock()
_auth_failures: dict[int, int] = {}
_verify_lock = threading.Lock()
_verify_inflight: set[int] = set()

# 连续 N 次鉴权失败才判定账号失效；可用 settings.auth_failure_threshold 覆盖。
AUTH_FAILURE_THRESHOLD = 3

# 站点偏好：settings.model_site_preference = {"default": <站点>, "models": {模型: <站点>}}。
# 同一个模型在国内站与国际站的计费不同（实测 deepseek-v4.1-flash 国际站免费、国内站
# 扣额度），所以「优先用哪边的账号」只能按模型配置，不能在选号逻辑里写死。
#
# "auto" 是特殊的默认值：不写死站点，而是从历史请求日志里学「哪个站点对这个模型免费/
# 更便宜」再优先用那一边（见 `_auto_site_preference`）。用户不用手工查价，也不用为每个
# 模型维护一条配置。未配置时默认值就是 auto，因为「优先用不花钱的那边」不会让任何
# 账号被错误降级 —— 它仍然只是优先级，学不到结论时不区分站点。
SITE_PREFERENCE_SETTING = "model_site_preference"
# "auto"：不写死站点，从实测计费里学「哪边免费/更便宜」再优先（见 _auto_site_preference）。
# 取值定义在 sites.py，写入侧（site_preference.py）与本文件共用同一份。
SITE_PREFERENCE_AUTO = sites.SITE_AUTO
SITE_PREFERENCE_DEFAULT = SITE_PREFERENCE_AUTO
# 自动模式要求多少样本才下结论：太少不足以判断计费，宁可先不区分站点。
SITE_AUTO_MIN_REQUESTS = 5

# observed_site_costs 每次调用要扫 30 天日志（实测 1.9ms），不能放在每次请求的热路径上。
_cost_profile_lock = threading.Lock()
_cost_profile_cache: tuple[float, dict] = (0.0, {})
COST_PROFILE_TTL = 60.0


def cost_profile() -> dict:
    """带缓存的站点计费画像（见 db.observed_site_costs）。"""
    global _cost_profile_cache
    now = time.monotonic()
    with _cost_profile_lock:
        stamp, cached = _cost_profile_cache
        if cached and now - stamp < COST_PROFILE_TTL:
            return cached
    try:
        fresh = db.observed_site_costs()
    except Exception:
        return {}
    with _cost_profile_lock:
        _cost_profile_cache = (now, fresh)
    return fresh


def forget_cost_profile() -> None:
    """清掉计费画像缓存（测试与手工调参用）。"""
    global _cost_profile_cache
    with _cost_profile_lock:
        _cost_profile_cache = (0.0, {})


def _get_token_lock(aid: int) -> asyncio.Lock:
    with _token_locks_guard:
        if aid not in _token_locks:
            _token_locks[aid] = asyncio.Lock()
        return _token_locks[aid]


def backend_url() -> str:
    value = str(db.get_setting("backend_url", BACKEND) or BACKEND).strip().rstrip("/")
    return value if value.startswith("https://") else BACKEND


# 国内版站点后缀的判定统一在 sites.py，这里不再自带一份（历史上
# fingerprint.origin_for 另有一套规则，导致请求发往国内站却自称国际站）。
def backend_url_for(account: Optional[dict] = None) -> str:
    """按账号所属站点选择上游域名，而不是一律使用全局 backend_url。

    国内版与国际版的凭证互不通用：把国内版账号的凭证发到 www.workbuddy.ai，会被
    openresty/APISIX 直接 401，随后 mark_account_failure 会把该账号误标成 expired
    （其实它的 token 还有效）。所以 domain 指向国内站点的账号必须走它自己的站点。

    三类情况分开处理：

    - `.cn` 账号：一律走它自己的站点。
    - 国际版账号（`*.workbuddy.ai`）+ `backend_url` 是官方内部入口
      （默认值 `copilot.tencent.com`）：走它自己的站点。那个入口只认内部 realm 签发的
      token，国际版 token 打过去会被 APISIX 直接 401（返回 HTML，不是业务 JSON）。
    - 其余（用户配了真正的自定义 relay、或域未知）：回退全局 `backend_url`。
      自定义 relay 必须继续生效，这是刻意保留的口子。
    """
    domain = (account or {}).get("domain")
    url = sites.site_url(domain)
    if url:
        return url
    fallback = backend_url()
    if sites.is_intl_domain(domain) and sites.is_internal_entry(fallback):
        return f"https://{sites.normalize_domain(domain)}"
    return fallback


def request_timeout(default: int) -> int:
    try:
        return max(5, min(600, int(db.get_setting("timeout", default))))
    except (TypeError, ValueError):
        return default


def auth_failure_threshold() -> int:
    """连续多少次鉴权失败才判定账号失效。"""
    try:
        value = int(db.get_setting("auth_failure_threshold", AUTH_FAILURE_THRESHOLD))
    except (TypeError, ValueError):
        return AUTH_FAILURE_THRESHOLD
    return max(1, min(20, value))


def _clean_site(value) -> str:
    """只接受 sites.SITE_GROUPS 里的值；"auto" 原样保留；其余视为「不区分」。"""
    text = str(value or "").strip().lower()
    if text == SITE_PREFERENCE_AUTO:
        return text
    return text if text in sites.SITE_GROUPS else ""


def model_site_preference() -> dict:
    """容错读取站点偏好设置，返回 {"default": str, "models": {模型: str}}。

    设置缺失或格式不对时给默认值而不是报错：路由不能因为一条配置写坏就整个失效。
    注意区分「设置不存在」与「显式留空」：不存在 → 用内置默认值，空串 → 不区分站点。
    """
    try:
        raw = db.get_setting(SITE_PREFERENCE_SETTING, None)
    except Exception:
        raw = None
    if not isinstance(raw, dict):
        return {"default": SITE_PREFERENCE_DEFAULT, "models": {}}

    models = raw.get("models")
    clean: dict[str, str] = {}
    if isinstance(models, dict):
        for key, value in models.items():
            model = str(key or "").strip()
            if model:
                clean[model] = _clean_site(value)

    default = raw.get("default") if "default" in raw else SITE_PREFERENCE_DEFAULT
    return {"default": _clean_site(default), "models": clean}


def _auto_site_preference(model: str) -> str:
    """自动模式：从实测计费里挑「不花钱 / 更便宜」的那个站点。

    同一个模型在两个站点的计费可以完全不同（deepseek-v4.1-flash 国际站免费、国内站
    扣额度；glm-5.3 反过来国际站收费），而「免费」是 (模型 × 站点) 的属性，模型目录里
    也没有价格字段，所以只能从历史请求日志里学。

    判定顺序：
      1. 只看收过费的次数，不收钱的那边优先（完全免费是最优解）；
      2. 两边都收钱 → 按平均单次扣费取低（就是你说的「收费低」）；
      3. 一边免费一边收费 → 免费那边；
      4. 样本不足（SITE_AUTO_MIN_REQUESTS）→ 不区分站点，先让路由自己探索。
    返回空串表示不区分。
    """
    by_site = cost_profile().get(model) or {}
    scored = []
    for group in sites.SITE_GROUPS:
        stats = by_site.get(group) or {}
        requests = int(stats.get("requests") or 0)
        if requests < SITE_AUTO_MIN_REQUESTS:
            continue
        credit = float(stats.get("credit") or 0)
        scored.append((group, int(stats.get("paid") or 0), credit / requests))
    if len(scored) < 2:
        return ""
    # 先比「收过费的次数」（0 = 完全免费），再比平均单次扣费
    scored.sort(key=lambda item: (item[1], item[2]))
    best, worst = scored[0], scored[-1]
    # 两边计费表现一样时不必偏向任何一边，保持不区分（负载才能摊平）
    if (best[1], best[2]) == (worst[1], worst[2]):
        return ""
    return best[0]


def preferred_site_for(model: Optional[str]) -> str:
    """该模型优先用哪一组站点的账号。返回空串表示不区分站点。

    只影响优先级、不会排除账号：偏好站点没账号或都被试过时，`pick_account` 会退回
    全部候选，不会退化成「明明有账号却报无可用账号」。

    值为 "auto" 时按实测计费自动选（见 `_auto_site_preference`）。
    """
    preference = model_site_preference()
    mid = str(model or "").strip()
    chosen = preference["models"][mid] if mid and mid in preference["models"] else preference["default"]
    if chosen == SITE_PREFERENCE_AUTO:
        return _auto_site_preference(mid) if mid else ""
    return chosen


def _bump_auth_failure(aid: int) -> int:
    with _auth_failure_lock:
        count = _auth_failures.get(aid, 0) + 1
        _auth_failures[aid] = count
        return count


def _reset_auth_failure(aid: int):
    with _auth_failure_lock:
        _auth_failures.pop(aid, None)


def auth_failure_count(aid: int) -> int:
    with _auth_failure_lock:
        return _auth_failures.get(aid, 0)


def mark_account_success(aid: int, model: Optional[str] = None):
    with _failure_lock:
        _account_failures.pop(aid, None)
    if model is not None:
        clear_model_rate_limit(aid, model)
    _reset_auth_failure(aid)


def _normalize_route_model(model) -> str:
    return str(model or "").strip()


def clear_model_rate_limit(aid: int, model) -> None:
    """解除某账号在某模型上的限流冷却（请求真的成功了）。"""
    key = (aid, _normalize_route_model(model))
    with _rate_limit_lock:
        _model_rate_limits.pop(key, None)


def account_model_rate_limited(aid: int, model) -> bool:
    """该账号在该模型上是否还在限流冷却中。"""
    mid = _normalize_route_model(model)
    if not mid:
        return False
    key = (aid, mid)
    with _rate_limit_lock:
        expires = _model_rate_limits.get(key)
        if expires is None:
            return False
        if expires <= time.monotonic():
            _model_rate_limits.pop(key, None)
            return False
        return True


def mark_account_failure(aid: int, status_code: int = 0, model: Optional[str] = None):
    # 限流是模型级的（上游提示「可切换其他模型继续使用」）：只给这个模型打冷却，
    # 账号本身是好的，不记账号级失败 —— 否则该账号在其它模型上的正常服务也会被堵住。
    if status_code == 429 and _normalize_route_model(model):
        key = (aid, _normalize_route_model(model))
        with _rate_limit_lock:
            _model_rate_limits[key] = time.monotonic() + RATE_LIMIT_COOLDOWN_SECONDS
        return
    with _failure_lock:
        count, _ = _account_failures.get(aid, (0, 0.0))
        count += 1
        base = 30 if status_code in {401, 403, 429} else 5
        cooldown = min(300, base * (2 ** min(count - 1, 4)))
        _account_failures[aid] = (count, time.monotonic() + cooldown)
    if status_code in {401, 403}:
        # 401/403 常常只是一次瞬时抖动（上游边界网关抽风、账号被发到非所属站点等），
        # 一次就置 expired 会让一个 token 还有效的账号被永久停用、只能手工恢复。
        # 改为：先累积计数 + 冷却；达到阈值后做一次真实复核，复核确认失效才置 expired。
        if _bump_auth_failure(aid) >= auth_failure_threshold():
            _schedule_credentials_verification(aid)


def _schedule_credentials_verification(aid: int):
    """把凭证复核排进事件循环；同步上下文（无运行中的 loop）下放弃。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    with _verify_lock:
        if aid in _verify_inflight:
            return
        _verify_inflight.add(aid)

    async def _runner():
        try:
            await verify_account_credentials(aid)
        except Exception as exc:  # 复核绝不能影响正常请求链路
            print(f"[auth_manager] 凭证复核异常 (account={aid}): {exc}", file=sys.stderr)
        finally:
            with _verify_lock:
                _verify_inflight.discard(aid)

    loop.create_task(_runner())


async def probe_account_credentials(account: dict) -> tuple[str, str]:
    """用账号自己的站点做一次轻量真实请求，判断凭证是否真的还有效。

    返回 ("ok" | "invalid" | "unknown", 说明)。只有明确被上游拒绝鉴权才返回
    "invalid"；网络错误、5xx 等一律 "unknown"，不下结论。
    """
    aid = (account or {}).get("id")
    if not account:
        return "unknown", "account not found"

    if is_token_expired(account):
        # access token 已过期：能否刷新是唯一判据
        if await refresh_token(account):
            return "ok", "access token refreshed"
        return "invalid", "token refresh rejected"

    headers = build_billing_headers(_fingerprint_account(account))
    url = f"{backend_url_for(account)}/v2/billing/meter/get-user-resource"
    try:
        async with httpx.AsyncClient(timeout=request_timeout(15)) as c:
            r = await c.post(url, headers=headers, json={})
    except httpx.HTTPError as exc:
        return "unknown", f"network error: {str(exc)[:120]}"

    if r.status_code in (401, 403):
        return "invalid", f"HTTP {r.status_code} from {url.split('/v2/')[0]}"
    if not (200 <= r.status_code < 300):
        return "unknown", f"HTTP {r.status_code}"
    try:
        r.json()
    except ValueError:
        return "unknown", f"non-json body (HTTP {r.status_code})"
    print(f"[auth_manager] 账号 {aid} 凭证复核通过（HTTP {r.status_code}）", file=sys.stderr)
    return "ok", f"HTTP {r.status_code}"


async def verify_account_credentials(aid: int) -> dict:
    """鉴权失败达到阈值后的复核：只有确认凭证真的失效才把账号置为 expired。"""
    account = db.get_account(aid)
    if not account:
        _reset_auth_failure(aid)
        return {"account_id": aid, "action": "skipped", "detail": "account not found"}

    status = str(account.get("status") or "")
    if status != "active":
        # 人工停用 / 已判定失效的账号不在这里干预
        _reset_auth_failure(aid)
        return {"account_id": aid, "action": "skipped", "detail": f"status={status}"}

    result, detail = await probe_account_credentials(account)
    if result == "ok":
        _reset_auth_failure(aid)
        print(
            f"[auth_manager] 账号 {aid} 连续鉴权失败后复核通过（{detail}），保持 active",
            file=sys.stderr,
        )
        return {"account_id": aid, "action": "kept_active", "detail": detail}

    if result == "invalid":
        _reset_auth_failure(aid)
        db.update_account(aid, {"status": "expired"})
        print(
            f"[auth_manager] 账号 {aid} 连续 {auth_failure_threshold()} 次鉴权失败且复核失败"
            f"（{detail}），已置为 expired",
            file=sys.stderr,
        )
        return {"account_id": aid, "action": "expired", "detail": detail}

    # 上游本身有问题时不下结论，只保留冷却
    print(f"[auth_manager] 账号 {aid} 凭证复核结果不确定（{detail}），保持 active", file=sys.stderr)
    return {"account_id": aid, "action": "inconclusive", "detail": detail}


def account_is_cooling_down(aid: int) -> bool:
    with _failure_lock:
        failure = _account_failures.get(aid)
        if not failure:
            return False
        if failure[1] <= time.monotonic():
            _account_failures.pop(aid, None)
            return False
        return True


# ============================================================
# Auth 文件扫描
# ============================================================

def _expand_auth_path(path: Optional[str]) -> Optional[Path]:
    if not path:
        return None
    value = str(path).strip().strip('"')
    if not value:
        return None
    return Path(os.path.expandvars(value)).expanduser()


def _running_in_container() -> bool:
    value = os.environ.get("CB_DOCKER", "").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    return Path("/.dockerenv").exists()


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    result = []
    seen = set()
    for p in paths:
        key = str(p.resolve(strict=False))
        if os.name == "nt":
            key = key.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(p)
    return result


def _mask_value(value: str, left: int = 6, right: int = 4) -> str:
    value = value or ""
    if not value:
        return ""
    if len(value) <= left + right:
        return value[:2] + "..." if len(value) > 2 else "***"
    return f"{value[:left]}...{value[-right:]}"


def _safe_is_dir(path: Path) -> bool:
    """is_dir() 在权限不足时会抛 OSError（如 macOS 受保护目录），降级为 False。"""
    return _dir_state(path) == "dir"


def _dir_state(path: Path) -> str:
    """返回 'dir' / 'unreadable' / 'missing'。

    is_dir() 抛 PermissionError(EPERM) 时说明路径存在、只是读不了（不存在会是 ENOENT），
    因此单独区分出来，避免在权限受限时误报成「目录不存在」。
    """
    try:
        return "dir" if path.is_dir() else "missing"
    except PermissionError:
        return "unreadable"
    except OSError:
        return "missing"


def candidate_auth_dirs(auth_dir: Optional[str] = None) -> list[Path]:
    """返回会被扫描的 auth 目录候选项，包括不存在的路径。"""
    custom = _expand_auth_path(auth_dir)
    if custom:
        return [custom.parent if custom.suffix.lower() == ".info" else custom]

    explicit = _expand_auth_path(os.environ.get("CB_AUTH_DIR"))
    if explicit:
        return [explicit.parent if explicit.suffix.lower() == ".info" else explicit]

    home = Path.home()
    plat = sys.platform
    dirs = []
    if _running_in_container():
        dirs.append(Path(os.environ.get("CB_CONTAINER_AUTH_DIR", "/auth")))
    if plat == "darwin":
        dirs.append(home / "Library" / "Application Support" / "CodeBuddyExtension" / "Data" / "Public" / "auth")
    if plat == "win32":
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        dirs.append(local / "CodeBuddyExtension" / "Data" / "Public" / "auth")
    xdg = Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share"))
    dirs.append(xdg / "CodeBuddyExtension" / "Data" / "Public" / "auth")
    return _dedupe_paths(dirs)


def scan_auth_dirs(auth_dir: Optional[str] = None) -> list[Path]:
    """返回所有存在的 auth 目录路径。"""
    return [d for d in candidate_auth_dirs(auth_dir) if _safe_is_dir(d)]


def find_auth_files(auth_dir: Optional[str] = None) -> list[Path]:
    """扫描所有 auth 目录下的 *.info 文件。"""
    custom = _expand_auth_path(auth_dir)
    if custom:
        try:
            is_file = custom.is_file()
        except OSError:
            is_file = False
        if is_file:
            return [custom] if custom.suffix.lower() == ".info" else []

    files = []
    for d in scan_auth_dirs(auth_dir):
        try:
            files.extend(sorted(d.glob("*.info")))
        except OSError:
            continue
    return _dedupe_paths(files)


# 桌面端每次重新登录会把旧凭据留成时间戳快照，例如：
# workbuddy-desktop.2026-09-12T16-22-52-595Z.43534.c1e1c986-….info
# 这些是备份不是新账号，且旧 token 覆盖现有账号会造成降级 —— 必须识别出来。
_BACKUP_SUFFIX_RE = re.compile(
    r"\.\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}(?:-\d+)?Z?"  # .2026-09-12T16-22-52-595Z
    r"\.\d+"                                            # .43534 (pid)
    r"\.[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}"  # .uuid
    r"\.info$",
    re.IGNORECASE,
)


def is_backup_auth_file(name: str) -> bool:
    """判断文件名是否为桌面端自动留下的时间戳备份快照。"""
    return bool(_BACKUP_SUFFIX_RE.search(name or ""))


def is_encrypted_field_wrapper(value) -> bool:
    """判断字段值是否为桌面端的 `$wbEncrypted` 加密信封。

    新版客户端把 accessToken/refreshToken 等写成
    {"$wbEncrypted": 1, "envelope": "<base64 JSON>"}，密钥只存在客户端侧，
    网关解不开 —— 见到即按「不可导入」处理（见 seamless_login 模块）。
    """
    if not isinstance(value, dict):
        return False
    if value.get("$wbEncrypted") != 1:
        return False
    return isinstance(value.get("envelope"), str)


def _safe_auth_file_meta(path: Path, existing_uids: set[str]) -> dict:
    meta = {
        "name": path.name,
        "path": str(path),
        "dir": str(path.parent),
        "size": 0,
        "mtime": None,
        "valid": False,
        "reason": "",
        "account_name": "",
        "uid": "",
        "uid_masked": "",
        "domain": "",
        "expires_at": 0,
        "already_imported": False,
        "is_backup": is_backup_auth_file(path.name),
        # 新版桌面端把 token 写成 $wbEncrypted 信封：文件本身是合法的官方
        # 文件，但网关解不开、导不进来 —— 必须单独标出来，否则面板会显示
        # 「可导入/已导入」误导用户以为凭据在被使用。
        "encrypted": False,
    }
    try:
        st = path.stat()
        meta["size"] = st.st_size
        meta["mtime"] = int(st.st_mtime)
    except OSError as e:
        meta["reason"] = f"无法读取文件: {e}"
        return meta

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError:
        meta["reason"] = "不是有效 JSON"
        return meta
    except OSError as e:
        meta["reason"] = f"无法读取文件: {e}"
        return meta

    account = data.get("account", {}) if isinstance(data, dict) else {}
    auth = data.get("auth", {}) if isinstance(data, dict) else {}
    if not auth.get("accessToken"):
        meta["reason"] = "未发现 accessToken"
        return meta

    uid = account.get("uid", "")
    meta.update({
        "valid": True,
        "reason": "ok",
        "account_name": account.get("nickname", "") or path.stem,
        "uid": uid,
        "uid_masked": _mask_value(uid),
        "domain": auth.get("domain", DEFAULT_DOMAIN),
        "expires_at": auth.get("expiresAt", 0),
        "already_imported": bool(uid and uid in existing_uids),
    })
    if is_encrypted_field_wrapper(auth.get("accessToken")):
        meta["encrypted"] = True
        meta["reason"] = "凭据是新版客户端加密信封，网关无法导入（可用无感登录获取明文凭据）"
    return meta


def _account_credential_sources(files: list[dict], accounts: list[dict]) -> list[dict]:
    """把「本机文件」与「账号」对起来，返回按账号聚合的凭据来源视图。

    面板此前直接列文件，用户会把文件当账号数（16 个文件看起来像 16 个账号），
    也看不出某个账号到底有没有本机凭据。这里按账号聚合：每行一个账号，标注
    活跃凭据文件、备份数量，以及「仅数据库（靠刷新续期）」这类无文件账号。
    另外把本机有文件、库里却没有的 uid 单列成待导入行，避免漏掉可导入账号。
    """
    buckets: dict[str, dict] = {}
    for meta in files:
        bucket = buckets.setdefault(meta.get("uid") or "", {"live": [], "backups": []})
        (bucket["backups"] if meta.get("is_backup") else bucket["live"]).append(meta)

    known_uids = {str(a.get("uid") or "") for a in accounts if a.get("uid")}
    rows: list[dict] = []
    for account in accounts:
        uid = str(account.get("uid") or "")
        bucket = buckets.get(uid, {"live": [], "backups": []})
        live = bucket["live"]
        usable = [m for m in live if not m.get("encrypted")]
        if usable:
            source = "file"
        elif live:
            source = "encrypted"   # 有文件但解不开，实际仍靠数据库里的凭据
        else:
            source = "db"
        rows.append({
            "id": account.get("id"),
            "name": account.get("nickname") or account.get("name") or "",
            "provider": account.get("provider") or "workbuddy",
            "status": account.get("status") or "",
            "uid_masked": _mask_value(uid) if uid else "",
            "source": source,
            "live_files": [m["name"] for m in live],
            "importable_files": [m["name"] for m in usable if not m.get("already_imported")],
            "backup_count": len(bucket["backups"]),
            "last_mtime": max((m.get("mtime") or 0 for m in live), default=0),
        })

    for uid, bucket in buckets.items():
        if not uid or uid in known_uids:
            continue
        live = bucket["live"]
        if not live:
            continue
        rows.append({
            "id": None,
            "name": live[0].get("account_name") or "未导入账号",
            "provider": "workbuddy",
            "status": "",
            "uid_masked": live[0].get("uid_masked") or "",
            "source": "unimported" if any(not m.get("encrypted") for m in live) else "encrypted",
            "live_files": [m["name"] for m in live],
            "importable_files": [m["name"] for m in live if not m.get("encrypted")],
            "backup_count": len(bucket["backups"]),
            "last_mtime": max((m.get("mtime") or 0 for m in live), default=0),
        })
    rows.sort(key=lambda r: (r["id"] is None, -(r["last_mtime"] or 0)))
    return rows


def discover_auth_files(auth_dir: Optional[str] = None) -> dict:
    """返回本机 auth 文件的安全元信息，不返回任何 token 内容。"""
    candidates = candidate_auth_dirs(auth_dir)
    existing_dirs = [d for d in candidates if _safe_is_dir(d)]
    visible_dirs = existing_dirs or candidates
    in_container = _running_in_container()
    auth_mount = Path(os.environ.get("CB_CONTAINER_AUTH_DIR", "/auth"))

    dirs = []
    for d in visible_dirs:
        info_files = []
        state = _dir_state(d)
        # 'unreadable' 表示路径存在但读不了（如 macOS 受保护目录）：
        # 仍报 exists=True 并标记 readable=False，避免整个发现流程抛 500。
        exists = state != "missing"
        readable = state == "dir"
        if readable:
            try:
                info_files = sorted(d.glob("*.info"))
            except OSError:
                info_files = []
                readable = False
        # readable 必须始终输出：前端是按 `d.readable ? ... : ...` 取值的，
        # 字段缺失时 undefined 为假，会把正常可读的目录误标成「无权限」。
        entry = {
            "path": str(d),
            "exists": exists,
            "readable": readable,
            "file_count": len(info_files),
        }
        dirs.append(entry)

    accounts = db.list_accounts()
    existing_uids = {a.get("uid", "") for a in accounts if a.get("uid")}
    files = [_safe_auth_file_meta(f, existing_uids) for f in find_auth_files(auth_dir)]
    # 这个发现流程只扫 WorkBuddy 的 auth 目录（*.info），凭据来源视图也必须只列
    # WorkBuddy 账号 —— 否则 QClaw / TraeWork 账号会出现在面板里，并被标成
    # 「仅数据库 · 刷新续期」，看起来像是丢了本机凭据。
    workbuddy_accounts = [
        a for a in accounts if str(a.get("provider") or "workbuddy") == "workbuddy"
    ]
    return {
        "dirs": dirs,
        "files": files,
        "accounts": _account_credential_sources(files, workbuddy_accounts),
        "file_count": len(files),
        "backup_count": sum(1 for f in files if f.get("is_backup")),
        "valid_count": sum(1 for f in files if f.get("valid")),
        "importable_count": sum(
            1 for f in files
            if f.get("valid") and not f.get("already_imported")
            and not f.get("is_backup") and not f.get("encrypted")
        ),
        "runtime": {
            "container": in_container,
            "auth_mount": str(auth_mount),
            "auth_mount_exists": auth_mount.is_dir() if in_container else False,
        },
    }


def parse_auth_file(path: Path) -> Optional[dict]:
    """解析 auth 文件，返回结构化凭据。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    account = data.get("account", {})
    auth = data.get("auth", {})
    access = auth.get("accessToken")
    if not isinstance(access, str) or not access:
        # 2026-09-20：桌面端新版 auth 文件把 accessToken/refreshToken 写成了
        # {"$wbEncrypted": .., "envelope": ..} 加密信封，网关解不开。这里必须
        # 按无凭据跳过 —— 若把信封 dict 透传，_protect_account_data 加密时
        # 直接 AttributeError，启动自动导入崩溃循环；更糟的是库里可用的存量
        # 令牌会被信封垃圾覆盖。
        return None
    refresh = auth.get("refreshToken")
    if refresh is not None and not isinstance(refresh, str):
        return None
    session_state = auth.get("sessionState", "")
    if not isinstance(session_state, str):
        session_state = ""

    return {
        "name": account.get("nickname", "") or path.stem,
        "uid": account.get("uid", ""),
        "nickname": account.get("nickname", ""),
        "phone": account.get("phoneNumber", ""),
        "account_type": account.get("type", "personal"),
        "access_token": access,
        "refresh_token": refresh if isinstance(refresh, str) else "",
        "expires_at": auth.get("expiresAt", 0),
        "refresh_expires_at": auth.get("refreshExpiresAt", 0),
        "domain": auth.get("domain", DEFAULT_DOMAIN),
        "enterprise_id": account.get("enterpriseId", ""),
        "session_state": session_state,
    }


def import_auth_file(path: Path) -> Optional[int]:
    """扫描并导入 auth 文件到数据库。如果 uid 已存在则更新。"""
    parsed = parse_auth_file(path)
    if not parsed:
        return None

    # 检查是否已存在（按 uid 去重）
    existing = db.list_accounts()
    for acc in existing:
        if acc.get("uid") == parsed["uid"]:
            db.update_account(acc["id"], parsed)
            return acc["id"]

    return db.add_account(parsed)


def auto_scan_and_import(auth_dir: Optional[str] = None) -> dict:
    """自动扫描本机 auth 文件并导入。返回 {imported, updated, skipped}。"""
    result = {"imported": 0, "updated": 0, "skipped": 0, "errors": []}
    existing_by_uid = {
        account.get("uid"): account
        for account in db.list_accounts()
        if account.get("uid")
    }
    for f in find_auth_files(auth_dir):
        if is_backup_auth_file(f.name):
            # 备份快照不是新账号：按 uid 会匹配到现有账号，导入等于用旧
            # token 覆盖较新凭据（降级），一律跳过。
            result["skipped"] += 1
            continue
        parsed = parse_auth_file(f)
        if not parsed:
            result["skipped"] += 1
            continue
        existing = existing_by_uid.get(parsed.get("uid"))
        if existing:
            patch = {
                key: parsed[key]
                for key in (
                    "access_token",
                    "refresh_token",
                    "expires_at",
                    "refresh_expires_at",
                    "session_state",
                    "nickname",
                    "name",
                    "phone",
                )
                if key in parsed
            }
            db.update_account(existing["id"], patch)
            result["updated"] += 1
        else:
            aid = db.add_account(parsed)
            if parsed.get("uid"):
                existing_by_uid[parsed["uid"]] = {**parsed, "id": aid}
            result["imported"] += 1
    return result


# ============================================================
# Token 刷新
# ============================================================

async def refresh_token(account: dict) -> bool:
    """调后端刷新 token，写回数据库。返回是否成功。"""
    aid = account["id"]
    lock = _get_token_lock(aid)
    async with lock:
        headers = build_refresh_headers(account)
        url = f"{backend_url_for(account)}/v2/plugin/auth/token/refresh"

        try:
            async with httpx.AsyncClient(timeout=request_timeout(15)) as c:
                r = await c.post(url, headers=headers, json={})
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            print(f"[auth_manager] 刷新 token 网络失败 (account={aid}): {e}", file=sys.stderr)
            return False

        if not isinstance(data, dict) or data.get("code") != 0 or not data.get("data"):
            message = data.get("msg", "upstream rejected refresh") if isinstance(data, dict) else "invalid response"
            # 单次刷新被拒不足以判定失效（上游也可能瞬时抽风），同样累积到阈值再置 expired。
            if _bump_auth_failure(aid) >= auth_failure_threshold():
                _reset_auth_failure(aid)
                db.update_account(aid, {"status": "expired"})
                print(
                    f"[auth_manager] 账号 {aid} 连续 {auth_failure_threshold()} 次刷新被拒，已置为 expired"
                    f"（{str(message)[:240]}）",
                    file=sys.stderr,
                )
            else:
                print(f"[auth_manager] 刷新 token 失败 (account={aid}): {str(message)[:240]}", file=sys.stderr)
            return False

        new_auth = data["data"]
        now_ms = int(time.time() * 1000)
        next_status = "inactive" if account.get("status") == "inactive" else "active"
        update_data = {
            "access_token": new_auth.get("accessToken", ""),
            "refresh_token": new_auth.get("refreshToken", ""),
            "expires_at": new_auth.get("expiresAt") or (
                now_ms + new_auth.get("expiresIn", 0) * 1000
            ),
            "refresh_expires_at": new_auth.get("refreshExpiresAt") or (
                now_ms + new_auth.get("refreshExpiresIn", 0) * 1000
            ),
            "domain": new_auth.get("domain", DEFAULT_DOMAIN),
            "status": next_status,
        }
        db.update_account(aid, update_data)
        _reset_auth_failure(aid)
        return True


def is_token_expired(account: dict) -> bool:
    expires_at = account.get("expires_at", 0)
    if not expires_at:
        return True
    return time.time() * 1000 >= (expires_at - 60_000)


async def ensure_token_valid(account: dict) -> bool:
    """如果 token 快过期则刷新。返回是否有效。"""
    if not is_token_expired(account):
        return True
    return await refresh_token(account)


# ============================================================
# Header 构造
# ============================================================

def _fingerprint_account(account: dict) -> dict:
    """为指纹构造补齐默认 domain（不修改原 dict）。"""
    if (account.get("domain") or "").strip():
        return account
    domain = str(db.get_setting("default_domain", DEFAULT_DOMAIN) or DEFAULT_DOMAIN)
    return {**account, "domain": domain}


def build_headers(account: dict) -> dict:
    """Chat 请求头：官方 CLI 完整指纹（通用 + 账号 + IDE/CLI + SDK）。"""
    return fingerprint.chat_headers(_fingerprint_account(account))


def build_billing_headers(account: dict) -> dict:
    """Billing 接口（余额/积分）请求头指纹。"""
    return fingerprint.billing_headers(_fingerprint_account(account))


def build_refresh_headers(account: dict) -> dict:
    """Token 刷新接口请求头指纹（X-Refresh-Token 只出现在这里）。"""
    return fingerprint.refresh_headers(_fingerprint_account(account))


async def get_valid_headers(account: dict) -> Optional[dict]:
    """确保 token 有效后返回 chat 指纹 header。失败返回 None。"""
    if not await ensure_token_valid(account):
        return None
    # 重新从数据库读取最新凭据
    fresh = db.get_account(account["id"])
    if not fresh:
        return None
    return build_headers(fresh)


async def get_billing_headers(account: dict) -> Optional[dict]:
    """确保 token 有效后返回 billing 指纹 header。失败返回 None。"""
    if not await ensure_token_valid(account):
        return None
    # 重新从数据库读取最新凭据
    fresh = db.get_account(account["id"])
    if not fresh:
        return None
    return build_billing_headers(fresh)


# ============================================================
# 每日积分领取
# ============================================================

def _checkin_result(
    account: dict,
    *,
    ok: bool,
    status_code: int = 0,
    message: str = "",
    payload: Optional[dict] = None,
    claimed: bool = False,
    already_claimed: bool = False,
) -> dict:
    payload = payload or {}
    credit = payload.get("credit", payload.get("today_credit", 0)) or 0
    try:
        credit = float(credit)
    except (TypeError, ValueError):
        credit = 0
    return {
        "account_id": account.get("id"),
        "account_name": account.get("nickname") or account.get("name") or str(account.get("id")),
        "ok": ok,
        "claimed": claimed,
        "already_claimed": already_claimed,
        # 上游把该账号的签到判为「未开启或已过期」（active=false）。
        # 这是上游的活动开关状态，不是账号故障、也不是领取失败。
        # 单独成字段，便于上游汇总把它排除出 failed（见 control_plane.checkin_all）。
        "unavailable": False,
        "status_code": status_code,
        "message": message,
        "credit": credit,
        "active": payload.get("active"),
        "today_checked_in": payload.get("today_checked_in"),
        "today_credit": payload.get("today_credit"),
        "streak_days": payload.get("streak_days"),
        "is_streak_day": payload.get("is_streak_day"),
    }


def _unwrap_response(data: object) -> tuple[bool, str, dict]:
    if not isinstance(data, dict):
        return False, "响应不是 JSON 对象", {}
    code = data.get("code")
    msg = str(data.get("msg") or data.get("message") or "")
    payload = data.get("data") if isinstance(data.get("data"), dict) else data
    if code not in (None, 0):
        return False, msg or f"code={code}", payload
    return True, msg or "OK", payload


# ============================================================
# 官方额度资源
# ============================================================

def _to_float(value, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _parse_resource_time(value) -> tuple[int | None, str]:
    """把官方资源时间统一为秒级时间戳和原始可读字符串。"""
    if value in (None, "", 0, "0", "9999-99-99 99:99:99"):
        return None, str(value or "")

    if isinstance(value, (int, float)):
        raw = float(value)
        if raw > 10_000_000_000:
            raw = raw / 1000
        return int(raw), time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(raw))

    text = str(value).strip()
    if not text:
        return None, ""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text, fmt)
            return int(dt.timestamp()), text
        except ValueError:
            continue
    return None, text


def _safe_resource_item(item: dict, now_ts: int) -> dict:
    cycle_end_ts, cycle_end = _parse_resource_time(item.get("CycleEndTime"))
    cycle_start_ts, cycle_start = _parse_resource_time(item.get("CycleStartTime"))
    deduction_end_ts, deduction_end = _parse_resource_time(item.get("DeductionEndTime"))
    deduction_start_ts, deduction_start = _parse_resource_time(item.get("DeductionStartTime"))
    expired_ts, expired_time = _parse_resource_time(item.get("ExpiredTime"))

    remain = _to_float(item.get("CapacityRemainPrecise"), _to_float(item.get("CapacityRemain")))
    used = _to_float(item.get("CapacityUsedPrecise"), _to_float(item.get("CapacityUsed")))
    size = _to_float(item.get("CapacitySizePrecise"), _to_float(item.get("CapacitySize")))
    cycle_remain = _to_float(item.get("CycleCapacityRemainPrecise"), _to_float(item.get("CycleCapacityRemain")))
    cycle_used = _to_float(item.get("CycleCapacityUsedPrecise"), _to_float(item.get("CycleCapacityUsed")))
    cycle_size = _to_float(item.get("CycleCapacitySizePrecise"), _to_float(item.get("CycleCapacitySize"), size))
    effective_remain = cycle_remain if cycle_remain > 0 or cycle_used > 0 else remain

    expire_ts = expired_ts or deduction_end_ts or cycle_end_ts
    days_to_expire = None
    if expire_ts:
        days_to_expire = int((expire_ts - now_ts) / 86400)

    package_name = str(item.get("PackageName") or item.get("DealName") or item.get("ProductName") or "额度包")
    product_name = str(item.get("ProductName") or item.get("SubProductName") or "")
    status = _to_int(item.get("Status"))
    is_expired = bool(expire_ts and expire_ts < now_ts)

    return {
        "package_name": package_name,
        "product_name": product_name,
        "package_type": str(item.get("PackageType") or ""),
        "resource_type": str(item.get("ResourceType") or ""),
        "capacity_unit": str(item.get("CapacityUnit") or item.get("OriginUnit") or "credit"),
        "status": status,
        "remaining": round(remain, 4),
        "remaining_precise": round(effective_remain, 4),
        "used": round(used, 4),
        "size": round(size, 4),
        "cycle_remaining": round(cycle_remain, 4),
        "cycle_used": round(cycle_used, 4),
        "cycle_size": round(cycle_size, 4),
        "cycle_start": cycle_start,
        "cycle_start_ts": cycle_start_ts,
        "cycle_end": cycle_end,
        "cycle_end_ts": cycle_end_ts,
        "deduction_start": deduction_start,
        "deduction_start_ts": deduction_start_ts,
        "deduction_end": deduction_end,
        "deduction_end_ts": deduction_end_ts,
        "expired_time": expired_time,
        "expired_ts": expired_ts,
        "expire_ts": expire_ts,
        "expire_time": expired_time or deduction_end or cycle_end,
        "days_to_expire": days_to_expire,
        "expired": is_expired,
        "auto_renew": bool(_to_int(item.get("AutoRenewFlag"))),
        "remain_cycles": _to_int(item.get("RemainCycles")),
        "total_cycles": _to_int(item.get("TotalCycles")),
    }


def _resource_failure(
    account: dict,
    *,
    message: str,
    status_code: int = 0,
    allow_stale: bool = True,
) -> dict:
    cached = db.get_account_resource_cache(account.get("id")) if allow_stale and account.get("id") else None
    if cached:
        cached["stale"] = True
        cached["message"] = message
        cached["status_code"] = status_code
        return cached
    return {
        "ok": False,
        "status_code": status_code,
        "message": message,
        "account_id": account.get("id"),
        "account_name": account.get("nickname") or account.get("name") or str(account.get("id")),
        "total_dosage": 0,
        "resource_count": 0,
        "package_count": 0,
        "active_package_count": 0,
        "expired_package_count": 0,
        "expiring_package_count": 0,
        "available_total": 0,
        "expiring_7d_total": 0,
        "expiring_30d_total": 0,
        "next_expire_time": "",
        "next_expire_ts": None,
        "next_expire_amount": 0,
        "next_expire_days": None,
        "updated_at": int(time.time()),
        "cached": False,
        "stale": False,
        "age_seconds": 0,
        "packages": [],
        "expiring_packages": [],
    }


async def fetch_account_resources(
    account: dict,
    *,
    force: bool = False,
    max_age_seconds: int = 60,
    allow_stale: bool = True,
) -> dict:
    """查询官方额度资源，只返回安全摘要和额度包明细。"""
    if account.get("id") and not force:
        cached = db.get_account_resource_cache(account["id"])
        if cached and int(cached.get("age_seconds") or 0) <= max_age_seconds:
            cached["stale"] = False
            return cached

    headers = await get_billing_headers(account)
    if not headers:
        return _resource_failure(
            account,
            message="token refresh failed or account credentials are invalid",
            allow_stale=allow_stale,
        )

    try:
        async with httpx.AsyncClient(timeout=request_timeout(25)) as c:
            r = await c.post(f"{backend_url_for(account)}/v2/billing/meter/get-user-resource", headers=headers, json={})
            data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        return _resource_failure(
            account,
            message=str(e)[:240],
            allow_stale=allow_stale,
        )

    ok, msg, payload = _unwrap_response(data)
    if r.status_code < 200 or r.status_code >= 300:
        ok = False

    response = payload.get("Response") if isinstance(payload.get("Response"), dict) else {}
    raw_data = response.get("Data") if isinstance(response.get("Data"), dict) else {}
    raw_items = raw_data.get("Accounts") if isinstance(raw_data.get("Accounts"), list) else []
    now_ts = int(time.time())
    packages = [_safe_resource_item(x, now_ts) for x in raw_items if isinstance(x, dict)]
    packages.sort(key=lambda x: (
        x.get("expired", False),
        x.get("expire_ts") or 9_999_999_999,
        -float(x.get("remaining_precise") or 0),
    ))

    active_packages = [p for p in packages if not p.get("expired")]
    expiring_packages = [
        p for p in active_packages
        if p.get("expire_ts") and 0 <= (p["expire_ts"] - now_ts) <= 7 * 86400
        and float(p.get("remaining_precise") or 0) > 0
    ]
    expiring_30d_packages = [
        p for p in active_packages
        if p.get("expire_ts") and 0 <= (p["expire_ts"] - now_ts) <= 30 * 86400
        and float(p.get("remaining_precise") or 0) > 0
    ]
    next_expiring = next(
        (
            p for p in active_packages
            if p.get("expire_ts") and float(p.get("remaining_precise") or 0) > 0
        ),
        None,
    )
    expired_packages = [p for p in packages if p.get("expired")]

    result = {
        "ok": ok,
        "status_code": r.status_code,
        "message": msg,
        "account_id": account.get("id"),
        "account_name": account.get("nickname") or account.get("name") or str(account.get("id")),
        "total_dosage": round(_to_float(raw_data.get("TotalDosage")), 4),
        "resource_count": _to_int(raw_data.get("TotalCount"), len(packages)),
        "package_count": len(packages),
        "active_package_count": len(active_packages),
        "expired_package_count": len(expired_packages),
        "expiring_package_count": len(expiring_packages),
        "available_total": round(sum(float(p.get("remaining_precise") or 0) for p in active_packages), 4),
        "expiring_7d_total": round(sum(float(p.get("remaining_precise") or 0) for p in expiring_packages), 4),
        "expiring_30d_total": round(sum(float(p.get("remaining_precise") or 0) for p in expiring_30d_packages), 4),
        "next_expire_time": next_expiring.get("expire_time") if next_expiring else "",
        "next_expire_ts": next_expiring.get("expire_ts") if next_expiring else None,
        "next_expire_amount": round(float(next_expiring.get("remaining_precise") or 0), 4) if next_expiring else 0,
        "next_expire_days": next_expiring.get("days_to_expire") if next_expiring else None,
        "updated_at": now_ts,
        "cached": False,
        "stale": False,
        "age_seconds": 0,
        "packages": packages,
        "expiring_packages": expiring_packages,
    }
    if ok and account.get("id"):
        db.upsert_account_resource_cache(account["id"], result)
    if not ok:
        return _resource_failure(
            account,
            message=msg,
            status_code=r.status_code,
            allow_stale=allow_stale,
        )
    return result


def _checkin_failure(
    account: dict,
    *,
    message: str,
    status_code: int = 0,
    allow_stale: bool = True,
) -> dict:
    cached = db.get_account_checkin_cache(account.get("id")) if allow_stale and account.get("id") else None
    if cached:
        cached["stale"] = True
        cached["message"] = message
        cached["status_code"] = status_code
        return cached
    return _checkin_result(account, ok=False, status_code=status_code, message=message)


async def fetch_checkin_status(
    account: dict,
    *,
    force: bool = False,
    max_age_seconds: int = 300,
    allow_stale: bool = True,
) -> dict:
    """查询每日积分领取状态。只返回安全摘要，不返回凭据。"""
    if account.get("id") and not force:
        cached = db.get_account_checkin_cache(account["id"])
        if cached and int(cached.get("age_seconds") or 0) <= max_age_seconds:
            cached["stale"] = False
            return cached

    headers = await get_billing_headers(account)
    if not headers:
        return _checkin_failure(
            account,
            message="token refresh failed or account credentials are invalid",
            allow_stale=allow_stale,
        )

    try:
        async with httpx.AsyncClient(timeout=request_timeout(20)) as c:
            r = await c.post(f"{backend_url_for(account)}/v2/billing/meter/checkin-activity-status", headers=headers, json={})
            data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        return _checkin_failure(account, status_code=0, message=str(e)[:240], allow_stale=allow_stale)

    ok, msg, payload = _unwrap_response(data)
    if r.status_code < 200 or r.status_code >= 300:
        ok = False
    result = _checkin_result(
        account,
        ok=ok,
        status_code=r.status_code,
        message=msg,
        payload=payload,
        already_claimed=bool(payload.get("today_checked_in")),
    )
    result["updated_at"] = int(time.time())
    result["cached"] = False
    result["stale"] = False
    result["age_seconds"] = 0
    if ok and account.get("id"):
        db.upsert_account_checkin_cache(account["id"], result)
    if not ok:
        return _checkin_failure(account, status_code=r.status_code, message=msg, allow_stale=allow_stale)
    return result


async def claim_daily_checkin(account: dict) -> dict:
    """手动领取单个账号的每日积分。不会绕过验证或做自动定时。"""
    status = await fetch_checkin_status(account, force=True, allow_stale=False)
    if not status.get("ok"):
        return status
    if status.get("active") is False:
        # 上游把该账号的签到活动判为「未开启或已过期」。
        #
        # 证据（2026-09-16 实测）：直接打 daily-checkin，国际站返回
        # 400 code=10001「签到活动未开启或已过期」，国内站同码返回「今天已签到，请明天再来」。
        # 即 active=false 是**上游的活动开关状态**，不是我们能做到的事，
        # 也不代表账号/凭证坏了。所以 ok 保持 True、只标 unavailable，
        # 避免「一键领取」把它计进 failed 造成「系统坏了」的误判。
        status["unavailable"] = True
        status["message"] = "签到活动未开启或已过期"
        return status
    if status.get("today_checked_in"):
        status["already_claimed"] = True
        status["message"] = "今日已领取"
        return status

    fresh = db.get_account(account["id"])
    if not fresh:
        return _checkin_result(account, ok=False, message="account not found")
    headers = await get_billing_headers(fresh)
    if not headers:
        return _checkin_result(
            account,
            ok=False,
            message="token refresh failed or account credentials are invalid",
        )

    try:
        async with httpx.AsyncClient(timeout=request_timeout(30)) as c:
            r = await c.post(f"{backend_url_for(fresh)}/v2/billing/meter/daily-checkin", headers=headers, json={})
            data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        return _checkin_result(account, ok=False, status_code=0, message=str(e)[:240])

    ok, msg, payload = _unwrap_response(data)
    if r.status_code < 200 or r.status_code >= 300:
        ok = False
    credit = payload.get("credit", payload.get("today_credit", 0)) or 0
    try:
        credit = float(credit)
    except (TypeError, ValueError):
        credit = 0
    claimed = bool(ok and credit > 0)
    if claimed and float(fresh.get("credit_limit") or 0) > 0:
        db.update_account(fresh["id"], {"credit_limit": float(fresh.get("credit_limit") or 0) + credit})

    result = _checkin_result(
        account,
        ok=ok,
        status_code=r.status_code,
        message=("领取成功" if claimed else msg),
        payload=payload,
        claimed=claimed,
        already_claimed=bool(payload.get("today_checked_in")) and not claimed,
    )
    result["updated_at"] = int(time.time())
    result["cached"] = False
    result["stale"] = False
    result["age_seconds"] = 0
    if ok and account.get("id"):
        db.upsert_account_checkin_cache(account["id"], result)
    return result


# ============================================================
# 账号路由（同级粘性 + 模型能力感知）
# ============================================================

# 账号级模型能力。
#
# 国内站与国际站暴露的模型集几乎不重叠（实测国际站 18 个、国内站 29 个，交集 3 个），
# 而模型目录是按「通道」存的 —— 混装账号时目录必然是并集，单看目录无法判断某个账号
# 能不能服务某个模型。于是会出现：客户端请求只在国际站存在的模型，路由却把它发给了
# 国内账号，上游回 400（模型不存在 / 未授权），而 400 不在换号重试的集合里，客户端
# 直接看到报错。
#
# 这里记住两件事：
#   _account_models  —— 供应商模型列表说这个账号能服务哪些模型（主动能力）
#   _account_denied  —— 实测被上游明确拒绝过的模型（被动纠正，能覆盖列表不准的情况）
# 两者取交集的反面：被拒过 → 不能；已知列表里没有 → 不能；其余未知 → 先试，错了再记。
_model_capability_lock = threading.Lock()
_account_models: dict[int, frozenset[str]] = {}
_account_denied: dict[int, set[str]] = {}


def record_account_models(aid: int, model_ids) -> None:
    """记录账号在自己站点上能服务的模型（来自供应商模型列表）。"""
    ids = frozenset(str(mid).strip() for mid in (model_ids or []) if str(mid).strip())
    if not ids:
        return
    with _model_capability_lock:
        _account_models[aid] = ids
        # 已经被拒、但新列表里也没有的模型不可能再被选中，顺手清掉避免无限增长
        denied = _account_denied.get(aid)
        if denied:
            denied &= ids
            if not denied:
                _account_denied.pop(aid, None)


def mark_model_denied(aid: int, model: str) -> None:
    """记录「这个账号服务不了这个模型」——上游已经明确拒绝过。"""
    mid = str(model or "").strip()
    if not mid:
        return
    with _model_capability_lock:
        _account_denied.setdefault(aid, set()).add(mid)


def account_supports_model(aid: int, model: str) -> bool:
    """账号能否服务该模型。能力未知时返回 True（先试，由 400 自愈）。"""
    mid = str(model or "").strip()
    if not mid:
        return True
    with _model_capability_lock:
        if mid in _account_denied.get(aid, ()):
            return False
        known = _account_models.get(aid)
    if known is not None and mid not in known:
        return False
    return True


def forget_account(aid: int) -> None:
    """账号被删除后清掉它的全部路由状态。

    能力、粘性、失败计数都是按账号 id 存在内存里的，账号删了不清就会一直留着 ——
    粘性槽指向一个不存在的 id 会让该槽每次都要重新挑选，能力记录则是纯泄漏。
    """
    with _model_capability_lock:
        _account_models.pop(aid, None)
        _account_denied.pop(aid, None)
    with _route_lock:
        for key, sticky_id in list(_sticky_account_id.items()):
            if sticky_id == aid:
                _sticky_account_id.pop(key, None)
    with _failure_lock:
        _account_failures.pop(aid, None)
    with _rate_limit_lock:
        for key in [k for k in _model_rate_limits if k[0] == aid]:
            _model_rate_limits.pop(key, None)
    _reset_auth_failure(aid)


def _route_int(value, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _route_priority(account: dict) -> int:
    return _route_int(account.get("priority"), 0)


def is_route_excluded(account: dict) -> bool:
    """该账号是否被排除在自动选路之外（只允许被显式绑定调用）。

    优先级（priority）做不到这件事：它只分档，最低档仍然是「会被用到」的档，
    账号照样会在别人忙/被耗尽时被选中。要真正隔离一个账号，必须显式标记。

    标记放在 `extra.route_exclude`（该字段本就是自由格式的账号附属信息），
    不改 schema。被标记的账号：
      - 不进入 `pick_account` 的候选池（即 auto / 默认调度不会用它）；
      - 仍可被 API Key 的 default_account 绑定调用 —— 绑定走 `_pick_pinned_account`，
        刻意不经过这里，所以「隔离」不等于「禁用」。
    """
    extra = account.get("extra")
    if not isinstance(extra, dict):
        return False
    return bool(extra.get("route_exclude"))


def _route_weight(account: dict) -> int:
    return max(1, _route_int(account.get("weight"), 1))


def _route_load(account: dict, loads: Optional[dict[int, int]] = None) -> float:
    """按权重归一后的负载，越小越空闲。

    loads 是 db.recent_account_loads() 的近期窗口计数（选路用）。没传时回退到
    accounts.total_requests —— 那个终身累计值只用于展示/兼容，拿它做负载信号会让
    历史欠债永远追不平（见 db.ROUTE_WINDOW_SECONDS 的注释）。
    """
    if loads is None:
        count = _route_int(account.get("total_requests"), 0)
    else:
        count = int(loads.get(_route_int(account.get("id"), 0), 0))
    return count / _route_weight(account)


def _route_sort_key(account: dict, loads: Optional[dict[int, int]] = None):
    weight = _route_weight(account)
    return (
        -_route_priority(account),
        -weight,
        _route_load(account, loads),
        _route_int(account.get("id"), 0),
    )


# 粘性按 (通道, 模型) 分槽。不同模型能服务的账号集本来就不同（国内模型只有国内账号
# 有），共用一个槽会互相顶掉 —— 那正是「混装账号之后所有请求都压在一个账号上」的来源。
_STICKY_WILDCARD = "*"

# 粘性容差：按权重归一后的负载允许领先多少（1.0 = 一个权重单位的请求量）。
_STICKY_LOAD_SLACK = 1.0


def _sticky_key(provider: str, model: Optional[str]) -> str:
    return f"{provider}\x1f{str(model or '').strip() or _STICKY_WILDCARD}"


def _set_sticky_account(aid: int, provider: str = "workbuddy", model: Optional[str] = None):
    with _route_lock:
        _sticky_account_id[_sticky_key(provider, model)] = aid


def _sticky_overloaded(
    sticky: dict,
    chosen: dict,
    loads: Optional[dict[int, int]] = None,
) -> bool:
    """粘住的账号是否已经明显比同级最空闲的账号更累。

    容差是常量而不是按账号权重放大：权重只影响「多少请求算一个负载单位」，
    若再拿权重当容差，高权重账号会被允许领先过多，均衡就失去意义了。
    没有容差则会在两个账号之间来回抖动。
    """
    return _route_load(sticky, loads) > _route_load(chosen, loads) + _STICKY_LOAD_SLACK


def set_pinned_account(aid) -> None:
    """把当前请求绑定到固定账号。0/None/非法值 = 不绑定。"""
    try:
        value = int(aid or 0)
    except (TypeError, ValueError):
        value = 0
    _pinned_account_id.set(value if value > 0 else 0)


def pinned_account_id() -> int:
    """当前请求绑定的账号 id，0 表示未绑定。"""
    try:
        return int(_pinned_account_id.get() or 0)
    except (TypeError, ValueError):
        return 0


def _pick_pinned_account(aid: int, exclude_ids: set[int], provider: str, model: Optional[str] = None) -> Optional[dict]:
    """绑定账号时的选择逻辑：只认这一个账号，不可用就返回 None。

    刻意不做「换个账号重试」：Key 绑定账号的语义就是「这把 Key 只花这个账号的额度」，
    静默换号会让调用方以为额度没动、实际已经在吃别的账号。要允许回退就别绑定。
    """
    if aid in exclude_ids or account_is_cooling_down(aid):
        return None
    target = db.get_account(aid)
    if not target:
        return None
    if str(target.get("provider") or "workbuddy") != provider:
        return None
    if str(target.get("status") or "") != "active":
        return None
    # 绑定的账号正在这个模型上被限流时也返回 None：绑定语义是「只用这个账号」，
    # 不是「无视它的限流状态反复撞墙」。
    if model and account_model_rate_limited(aid, model):
        return None
    return target


def pick_account(
    exclude_ids: set[int] = None,
    provider: str = "workbuddy",
    model: Optional[str] = None,
) -> Optional[dict]:
    """选择一个可用账号。

    优先级越高越先用；同优先级下先按「能不能服务这个模型」过滤，再按站点偏好过滤，
    最后尽量粘住该模型上次用的账号（保住 prompt cache）。粘性只在没有明显跑偏时
    保留：粘住的账号比同级最空闲的账号多干了不少活就让位，避免长期只压一个账号。

    负载看的是「最近 ROUTE_WINDOW_SECONDS 内实际服务了多少请求」，不是 accounts 表里
    的终身累计计数 —— 后者只增不减，历史欠债会让「少的先用」退化成「永远只用计数
    最低的那个」，看起来就像固定路由到一个账号。

    过滤分两类，绝不能混为一谈：
      - 硬过滤（排除候选）：调用方显式排除、账号正在冷却、账号被隔离、
        **该账号在该模型上正在限流冷却**。这些情况下选它只会重演同一次失败。
      - 软过滤（只调优先级）：能力未知、站点偏好。
    限流冷却属于前者，且必须参与候选计算：否则请求会反复选中同一个正在被限流的
    账号、把换号重试的次数耗尽，而真正健康的账号从头到尾没被试过。

    最后：API Key 若绑定了账号（default_account>0），只返回那个账号。绑定优先于所有
    调度规则 —— 它存在的意义就是让调用方指定用哪个账号。
    """
    exclude_ids = exclude_ids or set()
    pinned = pinned_account_id()
    if pinned:
        return _pick_pinned_account(pinned, exclude_ids, provider, model)
    accounts = db.get_active_accounts(provider)
    candidates = [
        a for a in accounts
        if a["id"] not in exclude_ids
        and not account_is_cooling_down(a["id"])
        and not is_route_excluded(a)
    ]
    if not candidates:
        return None

    if model:
        # 限流按模型记：同一个账号在别的模型上可能完全正常，所以只把它从本模型的
        # 候选里去掉。若全部候选都在限流，保留原候选（宁可让上游再判一次，也不要
        # 报 No available accounts）。
        not_limited = [a for a in candidates if not account_model_rate_limited(a["id"], model)]
        if not_limited:
            candidates = not_limited
        capable = [a for a in candidates if account_supports_model(a["id"], model)]
        # 已知全都不支持时保留原候选：让上游来判，顺便把结论学回来
        if capable:
            candidates = capable

    # 站点偏好只调优先级、不排除账号：同一个模型两边计费不同，优先用不花钱的那边。
    # 偏好那边的账号都被试过（exclude_ids）时退回全部候选，不能让请求无账号可用。
    preferred = preferred_site_for(model) if model else ""
    if preferred:
        on_preferred = [a for a in candidates if sites.site_group(a.get("domain")) == preferred]
        if on_preferred:
            candidates = on_preferred

    highest_priority = max(_route_priority(a) for a in candidates)
    top_candidates = [a for a in candidates if _route_priority(a) == highest_priority]
    loads = db.recent_account_loads()
    chosen = sorted(top_candidates, key=lambda a: _route_sort_key(a, loads))[0]
    key = _sticky_key(provider, model)
    with _route_lock:
        sticky_id = _sticky_account_id.get(key)
        if sticky_id is not None:
            sticky = next((a for a in top_candidates if a["id"] == sticky_id), None)
            if sticky is not None and not _sticky_overloaded(sticky, chosen, loads):
                return sticky
        _sticky_account_id[key] = chosen["id"]
        return chosen


async def pick_account_with_fallback(
    exclude_ids: set[int] = None,
    provider: str = "workbuddy",
    model: Optional[str] = None,
) -> Optional[dict]:
    """选账号，如果全部过期则尝试刷新过期账号。只刷新同一 provider。"""
    account = pick_account(exclude_ids, provider=provider, model=model)
    if account:
        return account

    pinned = pinned_account_id()
    if pinned:
        # 绑定了账号：只刷新这一个，不碰其它账号。
        target = db.get_account(pinned)
        if (
            target
            and str(target.get("provider") or "workbuddy") == provider
            and pinned not in (exclude_ids or set())
            and await refresh_token(target)
        ):
            fresh = db.get_account(pinned)
            if fresh:
                _set_sticky_account(fresh["id"], provider, model)
            return fresh
        return None

    expired_accounts = sorted(
        (
            account
            for account in db.list_accounts(provider=provider)
            if account.get("status") == "expired"
        ),
        key=_route_sort_key,
    )
    for a in expired_accounts:
        if a["id"] in (exclude_ids or set()):
            continue
        # 被隔离的账号同样不参与「过期账号刷新」这条回退路径：否则它会先被刷新、
        # 再以 active 身份回到候选池，隔离就形同虚设。
        if is_route_excluded(a):
            continue
        if model and account_model_rate_limited(a["id"], model):
            continue
        if model and not account_supports_model(a["id"], model):
            continue
        if await refresh_token(a):
            fresh = db.get_account(a["id"])
            if fresh:
                _set_sticky_account(fresh["id"], provider, model)
            return fresh
    return None


# ============================================================
# 账号状态检查
# ============================================================

def get_account_status(account: dict) -> dict:
    """返回账号状态摘要。"""
    expired = is_token_expired(account)
    now_ms = int(time.time() * 1000)
    remaining_hours = 0
    if account.get("expires_at"):
        remaining_hours = max(0, int((account["expires_at"] - now_ms) / 1000 / 3600))
    credit_snapshot = max(0.0, float(account.get("credit_limit") or 0))
    credit_baseline = max(0.0, float(account.get("credit_baseline") or 0))
    total_credits = round(float(account.get("total_credits") or 0), 4)
    credit_since_snapshot = round(max(0.0, total_credits - credit_baseline), 4)
    credit_remaining = None
    credit_used_pct = 0
    if credit_snapshot > 0:
        credit_remaining = round(max(0.0, credit_snapshot - credit_since_snapshot), 4)
        credit_used_pct = min(100, round(credit_since_snapshot / credit_snapshot * 100, 1))

    return {
        "id": account["id"],
        "name": account.get("name", ""),
        "nickname": account.get("nickname", ""),
        "uid": account.get("uid", ""),
        "status": account.get("status", "unknown"),
        "weight": int(account.get("weight") or 1),
        "priority": int(account.get("priority") or 0),
        "token_expired": expired,
        "remaining_hours": remaining_hours,
        "total_requests": account.get("total_requests", 0),
        "total_tokens": account.get("total_tokens", 0),
        "total_credits": total_credits,
        "credit_limit": round(credit_snapshot, 4),
        "credit_snapshot": round(credit_snapshot, 4),
        "credit_baseline": round(credit_baseline, 4),
        "credit_since_snapshot": credit_since_snapshot,
        "credit_remaining": credit_remaining,
        "credit_used_pct": credit_used_pct,
        "credit_source": "local_snapshot" if credit_snapshot > 0 else "usage_only",
        "last_used_at": account.get("last_used_at"),
        "site_group": sites.site_group(account.get("domain")),
    }


def check_all_accounts() -> list[dict]:
    """检查所有账号状态。"""
    accounts = db.list_accounts()
    return [get_account_status(a) for a in accounts]
