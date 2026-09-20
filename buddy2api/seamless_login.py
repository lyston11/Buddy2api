"""无感登录：OAuth state 轮询采集 WorkBuddy 凭据。

为什么需要它（2026-09-20）：WorkBuddy 桌面端新版把 auth 文件里的
accessToken/refreshToken 换成了 `$wbEncrypted` 加密信封（AES-256-GCM，
密钥 sha256(atRestSecretKey) 只存在客户端侧），网关再也读不出明文 token。
但官方插件 OAuth 接口本身仍直接下发明文 token —— 这条路径与信封加密无关，
机制复刻自 WorkDaddy 的「无感登录」（daemon.js 的 oauthPollOnce）：

  1. POST /v2/plugin/auth/state?platform=workbuddy  → state + 授权链接
  2. 用户在浏览器用目标账号完成授权
  3. GET  /v2/plugin/auth/token?state=...           → 明文 accessToken/refreshToken
  4. GET  /v2/plugin/login/account?state=...        → 账号信息（uid 等）

拿到的凭据按 uid 归入已有账号（更新并重新激活）或新建账号，写入走 db 的
加密路径，与其它凭据一视同仁。
"""

from __future__ import annotations

import json
import secrets
import time
import urllib.error
import urllib.request
from typing import Optional

import buddy2api.database as db

API_BASE = "https://www.workbuddy.cn/v2/plugin"
DEFAULT_PLATFORM = "workbuddy"
FLOW_TIMEOUT_SECONDS = 600      # state 官方有效期
RESULT_RETENTION_SECONDS = 300  # 完成后保留结果供前端取回
USER_AGENT = "buddy2api-seamless-login"

_flows: dict[str, dict] = {}


class SeamlessLoginError(RuntimeError):
    """发起或轮询无感登录失败。"""


def _http_json(url: str, method: str = "GET", body=None, headers: Optional[dict] = None, timeout: int = 30) -> dict:
    """最小 JSON 请求器。模块级函数，测试里整体替换即可不打网络。"""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:  # 上游 4xx/5xx 也要按 JSON 语义处理
        raw = exc.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        raise SeamlessLoginError(f"网络错误: {exc.reason}") from exc
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError as exc:
        raise SeamlessLoginError("上游返回不是合法 JSON") from exc
    if not isinstance(parsed, dict):
        raise SeamlessLoginError("上游返回结构异常")
    return parsed


def _purge(now: Optional[float] = None) -> None:
    now = now if now is not None else time.time()
    for key, flow in list(_flows.items()):
        if flow["expires_at"] <= now:
            _flows.pop(key, None)


def start(platform: str = DEFAULT_PLATFORM) -> dict:
    """申请 state 与授权链接。返回 {login_id, auth_url, expires_in, platform}。"""
    _purge()
    resp = _http_json(f"{API_BASE}/auth/state?platform={platform}", "POST", {})
    data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
    state = str(data.get("state") or "")
    if not state:
        raise SeamlessLoginError(f"auth/state 未返回 state（code={resp.get('code')}）")
    auth_url = (
        data.get("authUrl")
        or data.get("auth_url")
        or data.get("url")
        or f"{API_BASE.rsplit('/v2/plugin', 1)[0]}/login?platform={platform}&state={state}"
    )
    login_id = "sl_" + secrets.token_urlsafe(16)
    _flows[login_id] = {
        "platform": platform,
        "state": state,
        "created_at": time.time(),
        "expires_at": time.time() + FLOW_TIMEOUT_SECONDS,
        "done": False,
        "result": None,
        "error": None,
    }
    return {
        "login_id": login_id,
        "auth_url": str(auth_url),
        "expires_in": FLOW_TIMEOUT_SECONDS,
        "platform": platform,
    }


def _norm_ms(value) -> Optional[int]:
    """有效期归一：秒/毫秒/字符串 → 毫秒。"""
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    if not isinstance(value, (int, float)):
        return None
    if value <= 0:
        return None
    return int(value if value > 1e10 else value * 1000)


def _resolve_deadline(token_data: dict, ms_key: str, in_key: str, fallback_seconds: float) -> int:
    deadline = _norm_ms(token_data.get(ms_key)) or _norm_ms(token_data.get(ms_key.lower()))
    if deadline is None:
        seconds = token_data.get(in_key)
        try:
            seconds = float(seconds)
        except (TypeError, ValueError):
            seconds = 0.0
        deadline = int(time.time() * 1000) + int((seconds or fallback_seconds) * 1000)
    return deadline


def _write_credentials(uid: str, token_data: dict, account: dict) -> dict:
    """按 uid 归入已有账号（更新）或新建账号。返回 {account_id, created, name}。"""
    access = str(token_data.get("accessToken") or token_data.get("access_token") or "")
    refresh = str(token_data.get("refreshToken") or token_data.get("refresh_token") or "")
    if not access:
        raise SeamlessLoginError("授权响应缺少 accessToken")
    domain = str(token_data.get("domain") or "") or "www.workbuddy.cn"
    nickname = str(account.get("nickname") or "")
    parsed = {
        "name": nickname or str(account.get("phoneNumber") or "") or uid,
        "uid": uid,
        "nickname": nickname,
        "phone": str(account.get("phoneNumber") or ""),
        "account_type": str(account.get("type") or "personal"),
        "access_token": access,
        "refresh_token": refresh,
        "expires_at": _resolve_deadline(token_data, "expiresAt", "expiresIn", 0),
        "refresh_expires_at": _resolve_deadline(token_data, "refreshExpiresAt", "refreshExpiresIn", 0),
        "domain": domain,
        "enterprise_id": str(account.get("enterpriseId") or ""),
    }
    session_state = token_data.get("sessionState") or token_data.get("session_state")
    if isinstance(session_state, str) and session_state:
        parsed["session_state"] = session_state

    for row in db.list_accounts(provider="workbuddy"):
        if str(row.get("uid") or "") == uid:
            patch = {k: v for k, v in parsed.items() if k != "uid"}
            db.update_account(int(row["id"]), patch)
            return {"account_id": int(row["id"]), "created": False, "name": parsed["name"]}

    parsed["provider"] = "workbuddy"
    new_id = db.add_account(parsed)
    return {"account_id": int(new_id), "created": True, "name": parsed["name"]}


def poll(login_id: str) -> dict:
    """轮询一次授权结果。

    返回 {status, ...}：
      pending  等待用户授权
      done     已写入（含 account_id / created / uid / nickname）
      expired  超过 10 分钟未完成
      unknown  login_id 不存在或结果已回收
      error    上游或写入失败
    """
    _purge()
    flow = _flows.get(login_id)
    if flow is None:
        return {"status": "unknown", "error": "登录请求不存在或已过期，请重新发起"}
    if flow["done"]:
        return {"status": "error", "error": flow["error"]} if flow["error"] else {"status": "done", **flow["result"]}
    if flow.get("polling"):
        # 前端每秒轮询，两个并发请求可能都读到「未入库」而各建一个账号；
        # 单进程内用标志位串行化，重复请求按待定返回。
        return {"status": "pending"}
    flow["polling"] = True
    try:
        return _poll_locked(flow)
    finally:
        flow["polling"] = False


def _poll_locked(flow: dict) -> dict:
    resp = _http_json(f"{API_BASE}/auth/token?state={flow['state']}")
    code = resp.get("code")
    data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
    access = str(data.get("accessToken") or data.get("access_token") or "")
    if code not in (0, 200) or not access:
        return {"status": "pending"}

    headers = {"Authorization": f"Bearer {access}"}
    if data.get("domain"):
        headers["X-Domain"] = str(data["domain"])
    account_resp = _http_json(f"{API_BASE}/login/account?state={flow['state']}", headers=headers)
    account = account_resp.get("data") if isinstance(account_resp.get("data"), dict) else {}
    uid = str(account.get("uid") or "")
    if not uid:
        flow["done"] = True
        flow["error"] = "官方接口未返回 uid，无法归类账号"
        return {"status": "error", "error": flow["error"]}

    try:
        written = _write_credentials(uid, data, account)
    except Exception as exc:  # 写入失败要把原因带回前端，而不是让流程悬着
        flow["done"] = True
        flow["error"] = str(exc)[:240]
        return {"status": "error", "error": flow["error"]}

    flow["done"] = True
    flow["result"] = {
        **written,
        "uid": uid,
        "nickname": str(account.get("nickname") or ""),
    }
    return {"status": "done", **flow["result"]}


def reset() -> None:
    """清空流程状态（测试用）。"""
    _flows.clear()
