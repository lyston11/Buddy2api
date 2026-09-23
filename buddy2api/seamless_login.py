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

**站点必须分开**：国内版与国际版的 OAuth 接口是两个 host，platform 标识也不同
（WorkDaddy profiles.js / daemon.js 实测：国内 `www.workbuddy.cn` /
`www.codebuddy.cn` 用 `workbuddy`，国际 `www.workbuddy.ai` / `www.codebuddy.ai`
用 `workbuddy-ai`）。拿国际版账号去国内 host 授权，拿到的是国内站的凭据 ——
要么建出一个错的账号，要么根本对不上。所以 start() 必须指定站点，轮询沿用
发起时选定的站点。
"""

from __future__ import annotations

import json
import secrets
import time
import urllib.error
import urllib.request
from typing import Optional
from urllib.parse import urlparse

import buddy2api.database as db
import buddy2api.sites as sites

FLOW_TIMEOUT_SECONDS = 600      # state 官方有效期
RESULT_RETENTION_SECONDS = 300  # 完成后保留结果供前端取回
USER_AGENT = "buddy2api-seamless-login"
API_PREFIX = "/v2/plugin"

# 站点 → (API host, platform 标识)。host 与 auth.domain 一致，platform 是官方
# 客户端标识：国际版必须用 `workbuddy-ai`，否则授权出来的凭据不属于目标站点。
SITE_ENDPOINTS = {
    sites.SITE_DOMESTIC: ("https://www.workbuddy.cn", "workbuddy"),
    sites.SITE_INTERNATIONAL: ("https://www.workbuddy.ai", "workbuddy-ai"),
}
DEFAULT_SITE = sites.SITE_DOMESTIC
DEFAULT_PLATFORM = SITE_ENDPOINTS[DEFAULT_SITE][1]

_flows: dict[str, dict] = {}


class SeamlessLoginError(RuntimeError):
    """发起或轮询无感登录失败。"""


def site_for_domain(domain) -> str:
    """账号域名 → 站点分组。没有域名的账号按默认站点（国内版）处理。"""
    text = str(domain or "").strip()
    if not text:
        return DEFAULT_SITE
    return sites.site_group(text)


def available_sites() -> list[dict]:
    """站点清单（含各自账号与状态），供前端选站点。

    为什么不只给数量：无感登录的授权链接是**通用**的 —— 最终授权成哪个账号由
    浏览器当前登录的身份决定，网关事先无从得知（uid 要等授权完成才由官方返回）。
    所以界面必须让用户看到「这个站点有哪些账号、哪个已经好了、哪个还要重新登录」，
    否则点开链接后无法判断这次授权会落到谁身上（2026-09-23 用户反馈）。
    """
    by_site: dict[str, list[dict]] = {}
    for account in db.list_accounts(provider="workbuddy"):
        group = site_for_domain(account.get("domain"))
        by_site.setdefault(group, []).append({
            "id": account.get("id"),
            "name": account.get("nickname") or account.get("name") or "",
            "status": account.get("status") or "",
            "uid_masked": _mask_uid(account.get("uid")),
        })
    out = []
    for group in sites.SITE_GROUPS:
        # 需要重新登录的排前面，前端一眼能看出还差谁
        rows = sorted(by_site.get(group, []), key=lambda a: (a["status"] == "active", a["id"] or 0))
        out.append({
            "site": group,
            "api_host": SITE_ENDPOINTS[group][0],
            "platform": SITE_ENDPOINTS[group][1],
            "account_count": len(rows),
            "pending_count": sum(1 for a in rows if a["status"] != "active"),
            "accounts": rows,
        })
    return out


def _mask_uid(uid) -> str:
    value = str(uid or "")
    if len(value) <= 12:
        return value
    return f"{value[:6]}…{value[-4:]}"



def _endpoint(site: str) -> tuple[str, str]:
    return SITE_ENDPOINTS.get(site) or SITE_ENDPOINTS[DEFAULT_SITE]


_AUTH_URL_HOSTS = tuple(
    host.split("://", 1)[1] for host, _platform in SITE_ENDPOINTS.values()
)


def _safe_auth_url(raw, host: str, platform: str, state: str) -> str:
    """只接受已知站点的 https 授权链接，否则用 host+state 自己拼一个。

    这个 URL 会直接进前端 `<a href>`,所以不能把上游返回的任意字符串原样透出
    （异常/被篡改的响应可能给出 `javascript:` 之类）。合法来源只有官方两个站点。
    """
    fallback = f"{host}/login?platform={platform}&state={state}"
    text = str(raw or "").strip()
    if not text.startswith("https://"):
        return fallback
    try:
        parsed = urlparse(text)
    except ValueError:
        return fallback
    if (parsed.hostname or "").lower() not in _AUTH_URL_HOSTS:
        return fallback
    return text


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


def start(site: str = DEFAULT_SITE, expect_uid: str = "") -> dict:
    """申请 state 与授权链接。返回 {login_id, auth_url, expires_in, site, platform, expect_uid}。

    `site` 决定打哪个 host、用哪个 platform 标识；轮询沿用发起时的选择，
    所以国际版账号不会被送到国内站授权。

    `expect_uid`：本次想恢复的账号 uid（界面上点某个账号的「重新登录」时会带上）。
    OAuth 链接本身无法指定身份 —— 授权成谁由浏览器当前登录态决定，所以这里只做
    **事后核对**：授权回来的 uid 与 expect_uid 不一致时，poll 会给出 `uid_matched=false`
    并说明实际落到哪个账号，避免用户以为"点的是 A，登的却是 B"。
    """
    _purge()
    host, platform = _endpoint(site)
    api_base = f"{host}{API_PREFIX}"
    resp = _http_json(f"{api_base}/auth/state?platform={platform}", "POST", {})
    data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
    state = str(data.get("state") or "")
    if not state:
        raise SeamlessLoginError(f"auth/state 未返回 state（code={resp.get('code')}）")
    # 上游给的链接不直接采信：只接受已知站点的 https 链接，否则回退到我们自己拼的。
    # 该 URL 会被前端渲染成可点击的 <a href>，来源不可信时可能是 javascript: 之类。
    auth_url = _safe_auth_url(
        data.get("authUrl") or data.get("auth_url") or data.get("url"),
        host,
        platform,
        state,
    )
    login_id = "sl_" + secrets.token_urlsafe(16)
    _flows[login_id] = {
        "site": site,
        "platform": platform,
        "api_base": api_base,
        "state": state,
        "expect_uid": str(expect_uid or ""),
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
        "site": site,
        "platform": platform,
        "expect_uid": str(expect_uid or ""),
    }


def account_name_for_uid(uid: str) -> str:
    """按 uid 找账号显示名（用于界面提示"这次要登的是谁"）。"""
    target = str(uid or "")
    if not target:
        return ""
    for account in db.list_accounts(provider="workbuddy"):
        if str(account.get("uid") or "") == target:
            return str(account.get("nickname") or account.get("name") or "")
    return ""


def site_for_uid(uid: str) -> str:
    """按 uid 找该账号所属站点（界面点账号的「重新登录」时用它决定 host）。"""
    target = str(uid or "")
    for account in db.list_accounts(provider="workbuddy"):
        if str(account.get("uid") or "") == target:
            return site_for_domain(account.get("domain"))
    return DEFAULT_SITE


def pending_accounts() -> list[dict]:
    """列出需要重新登录的账号（非 active），附各自站点。"""
    out = []
    for account in db.list_accounts(provider="workbuddy"):
        status = str(account.get("status") or "")
        if status == "active":
            continue
        group = site_for_domain(account.get("domain"))
        uid = str(account.get("uid") or "")
        out.append({
            "id": account.get("id"),
            "name": account.get("nickname") or account.get("name") or "",
            "status": status,
            "site": group,
            "api_host": SITE_ENDPOINTS[group][0],
            "platform": SITE_ENDPOINTS[group][1],
            # 只给掩码：明文 uid 没必要离开后端（前端只用它显示"无 uid"提示），
            # 真正的 uid 在 start_for_pending 里按账号 id 现取，用于授权后核对。
            "uid_masked": _mask_uid(uid),
            # 没有 uid 就无法在授权后核对身份，界面要如实说明（不能假装已核对）
            "verifiable": bool(uid),
        })
    return out


def _uid_for_account_id(account_id) -> str:
    """按账号 id 取 uid（发起授权时用，不外发）。"""
    for account in db.list_accounts(provider="workbuddy"):
        if account.get("id") == account_id:
            return str(account.get("uid") or "")
    return ""


def start_for_pending() -> dict:
    """一键给所有「需要登录」的账号各生成一个授权链接。

    界面一次点击就该拿到「谁要登录 + 去哪儿登」，不该让用户先理解"站点"这种实现
    细节 —— 站点从账号的 domain 推导即可（2026-09-23 用户要求）。
    每个账号一条 flow：OAuth 一次授权只能确定一个身份，无法批量。
    """
    pending = pending_accounts()
    flows = []
    for item in pending:
        try:
            started = start(site=item["site"], expect_uid=_uid_for_account_id(item["id"]))
        except SeamlessLoginError as exc:
            flows.append({**item, "error": str(exc)[:200]})
            continue
        flows.append({
            **item,
            "login_id": started["login_id"],
            "auth_url": started["auth_url"],
            "expires_in": started["expires_in"],
        })
    return {
        "pending": flows,
        "pending_count": len(flows),
        "all_active": len(pending) == 0,
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


def _resolve_deadline(token_data: dict, ms_key: str, in_key: str, fallback_seconds) -> Optional[int]:
    """解析有效期（毫秒）。拿不到且未给 fallback 时返回 None —— 调用方据此决定不写。"""
    deadline = _norm_ms(token_data.get(ms_key)) or _norm_ms(token_data.get(ms_key.lower()))
    if deadline is None:
        try:
            seconds = float(token_data.get(in_key))
        except (TypeError, ValueError):
            seconds = None
        if not seconds and fallback_seconds is None:
            return None
        deadline = int(time.time() * 1000) + int((seconds or fallback_seconds or 0) * 1000)
    return deadline


def _write_credentials(uid: str, token_data: dict, account: dict, site: str = DEFAULT_SITE) -> dict:
    """按 uid 归入已有账号（更新）或新建账号。返回 {account_id, created, name}。

    `site` 是发起授权时选定的站点：上游没回 `domain` 时用它对应的 host 兜底，
    避免把国际版账号的域名写成国内站（那会让后续请求打到错的上游）。
    """
    if not str(uid or "").strip():
        # 空 uid 会匹配到"同样没有 uid"的账号，把凭据写进别人身上；宁可报错
        raise SeamlessLoginError("授权响应缺少 uid，拒绝写入（无法确定归属账号）")
    access = str(token_data.get("accessToken") or token_data.get("access_token") or "")
    refresh = str(token_data.get("refreshToken") or token_data.get("refresh_token") or "")
    if not access:
        raise SeamlessLoginError("授权响应缺少 accessToken")
    fallback_host = _endpoint(site)[0].split("://", 1)[-1]
    domain = str(token_data.get("domain") or "") or fallback_host
    nickname = str(account.get("nickname") or "")
    parsed = {
        "name": nickname or str(account.get("phoneNumber") or "") or uid,
        "uid": uid,
        "nickname": nickname,
        "phone": str(account.get("phoneNumber") or ""),
        "account_type": str(account.get("type") or "personal"),
        "access_token": access,
        "refresh_token": refresh,
        "domain": domain,
        "enterprise_id": str(account.get("enterpriseId") or ""),
    }
    # 有效期拿不到就不写：写一个"现在"会让账号看上去立刻过期，比不更新更糟
    for ms_key, in_key, field in (
        ("expiresAt", "expiresIn", "expires_at"),
        ("refreshExpiresAt", "refreshExpiresIn", "refresh_expires_at"),
    ):
        deadline = _resolve_deadline(token_data, ms_key, in_key, None)
        if deadline:
            parsed[field] = deadline
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
    api_base = flow.get("api_base") or f"{_endpoint(flow.get('site') or DEFAULT_SITE)[0]}{API_PREFIX}"
    resp = _http_json(f"{api_base}/auth/token?state={flow['state']}")
    code = resp.get("code")
    data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
    access = str(data.get("accessToken") or data.get("access_token") or "")
    if code not in (0, 200) or not access:
        return {"status": "pending"}

    headers = {"Authorization": f"Bearer {access}"}
    if data.get("domain"):
        headers["X-Domain"] = str(data["domain"])
    account_resp = _http_json(f"{api_base}/login/account?state={flow['state']}", headers=headers)
    account = account_resp.get("data") if isinstance(account_resp.get("data"), dict) else {}
    uid = str(account.get("uid") or "")
    if not uid:
        flow["done"] = True
        flow["error"] = "官方接口未返回 uid，无法归类账号"
        return {"status": "error", "error": flow["error"]}

    try:
        written = _write_credentials(uid, data, account, flow.get("site") or DEFAULT_SITE)
    except Exception as exc:  # 写入失败要把原因带回前端，而不是让流程悬着
        flow["done"] = True
        flow["error"] = str(exc)[:240]
        return {"status": "error", "error": flow["error"]}

    flow["done"] = True
    expect = str(flow.get("expect_uid") or "")
    flow["result"] = {
        **written,
        "uid": uid,
        "nickname": str(account.get("nickname") or ""),
        # 事后核对：OAuth 链接无法指定身份，浏览器登录态决定授权成谁。
        # 与预期不符时前端要明确报警，避免用户以为"点的是 A，登的却是 B"。
        "expected_name": account_name_for_uid(expect) if expect else "",
        "uid_matched": (not expect) or (uid == expect),
    }
    return {"status": "done", **flow["result"]}


def reset() -> None:
    """清空流程状态（测试用）。"""
    _flows.clear()
