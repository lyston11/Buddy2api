"""fingerprint.py — WorkBuddy / CodeBuddy 出站请求头（官方客户端伪装）。

上游有**渠道校验**：不是「官方客户端发出的」请求会在后端被拒。2026-10-24 实测在
`POST /v2/chat/completions` 上摸清了它今天到底认什么（逐个删头做的对照实验）：

1. **首条消息必须是 system**。缺了返回 `400 {"code":11128,"msg":"first message is
   not system prompt"}`，displayTips 明说「这次调用没通过渠道校验，可能不是官方
   客户端发出的」。网关侧由 `proxy._ensure_leading_system_message` 补一条兜住。
2. **必须带三个会话头**：`X-Conversation-ID` / `X-Conversation-Request-ID` /
   `X-Conversation-Message-ID`。删掉任意一个（哪怕别的头全在）都会变成
   `400 {"code":11128,"msg":"request illegal"}`。
3. **User-Agent 决定落到哪条后端流水线**：`WorkBuddy/<app> ...` 走桌面端会话流水线
   （回包首行多一条 `: heartbeat` 注释、chunk id 带短横线），`CLI/<cli> CodeBuddy/<cli>`
   走 CLI 流水线。两条流水线今天都 200，用量（usage/credit）完全一致。
4. 其余头（X-IDE-*、X-Private-Data、X-Agent-Intent/Purpose/Type、traceparent/b3、
   acp-connection-id、X-Custom-Channel-Id、Origin/Referer、x-stainless-*）删掉都不影响
   判定 —— 但它们是官方客户端的真实形态，跟着一起发才不显眼，也是上游一收紧就还能过的
   余量。`x-stainless-*` 两个官方客户端都不发，已删掉（它来自别的 SDK 拷贝）。

所以本模块按「身份」分两套 profile：

- `app`（默认）：桌面端 WorkBuddy。UA `WorkBuddy/<app> WorkBuddy/<app> CLI/<cli>`，
  `X-IDE-Type/Name=WorkBuddy`、`X-IDE-Version=<桌面端版本>`，带 `X-Private-Data`、
  `X-Agent-Purpose: conversation`、`acp-connection-id`。版本号从本机安装的客户端里读
  （国际版 `/Applications/WorkBuddy AI.app`，国内版 `/Applications/WorkBuddy.app`），
  读不到才回退常量 —— 上游对版本号本身不做严格校验，但对「自称的身份」有渠道判定。
- `cli`：命令行 CodeBuddy。UA `CLI/<cli> CodeBuddy/<cli>`、`X-IDE-Type/Name=CLI`。

渠道校验失败（11128）时 `proxy` 会调 `switch_channel()` 换另一套 profile 重试一次
（见 `_plan_channel_retry`）：11128 说的是「你这次的头/体不像官方客户端」，跟账号本身
无关，所以既不该给账号记失败，也不该耗掉换号额度。

环境变量：

  CB_GATEWAY_CHANNEL=app|cli              默认身份（默认 app）
  CB_GATEWAY_APP_VERSION                  桌面端版本，覆盖自动探测
  CB_GATEWAY_CLI_VERSION                  CLI 版本，覆盖自动探测（旧名 CB_GATEWAY_IDE_VERSION）
  CB_GATEWAY_USER_AGENT                   整体覆盖 User-Agent（最高优先级）
  CB_GATEWAY_CONVERSATION_ID=stable|random  会话 id 是否按会话稳定（默认 stable）
  CB_GATEWAY_STAINLESS_OS                 仅 billing/refresh 之外的兼容开关，可忽略
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import plistlib
import secrets
import uuid
from contextvars import ContextVar
from functools import lru_cache
from typing import Optional

import buddy2api.sites as sites

# 桌面端 / CLI 版本：优先读本机装的官方客户端，读不到才用这两个回退值。
# 2026-10-24 的实测值（WorkBuddy AI 5.7.6 + 内置 CLI 2.156.0）。
DEFAULT_APP_VERSION = "5.7.6"
DEFAULT_CLI_VERSION = "2.156.0"

_GLOBAL_ORIGIN = "https://www.workbuddy.ai"

# 站点 → macOS 客户端安装路径。国内和国际是两个独立 app（版本号也不同），
# 所以 X-IDE-Version / UA 必须按账号站点取，否则国内账号会自称一个国际版版本号。
_MAC_APP_BUNDLES = {
    sites.SITE_INTERNATIONAL: "/Applications/WorkBuddy AI.app",
    sites.SITE_DOMESTIC: "/Applications/WorkBuddy.app",
}

# 渠道校验必需的三个会话头（见模块开头第 2 条）。
CONVERSATION_HEADERS = (
    "X-Conversation-ID",
    "X-Conversation-Request-ID",
    "X-Conversation-Message-ID",
)

CHANNEL_APP = "app"
CHANNEL_CLI = "cli"
CHANNELS = (CHANNEL_APP, CHANNEL_CLI)
_DEFAULT_CHANNEL = CHANNEL_APP

_DISABLED_VALUES = {"off", "none", "false", "0", ""}

# 会话 id 的命名空间：同一段对话（同一份 system + 第一条 user）在多轮里派生出同一个
# X-Conversation-ID，跟官方客户端「一个会话一个 id」的语义一致 —— 每次请求都换一个
# 全新会话 id 是官方客户端不会出现的形态。
_CONVERSATION_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://www.workbuddy.ai/conversation")

# 单请求的上下文：渠道覆盖（11128 时切换）与当前会话 id。用 ContextVar 是为了让
# auth_manager.build_headers 不必多带参数，同时请求之间互不串味。
_channel_override: ContextVar[Optional[str]] = ContextVar(
    "buddy2api_fingerprint_channel", default=None
)
_conversation_id: ContextVar[Optional[str]] = ContextVar(
    "buddy2api_fingerprint_conversation", default=None
)


# ============================================================
# 版本探测
# ============================================================

@lru_cache(maxsize=8)
def _bundle_app_version(app_dir: str) -> Optional[str]:
    try:
        with open(os.path.join(app_dir, "Contents", "Info.plist"), "rb") as fh:
            info = plistlib.load(fh)
    except (OSError, plistlib.InvalidFileException):
        return None
    value = str(info.get("CFBundleShortVersionString") or "").strip()
    return value or None


@lru_cache(maxsize=8)
def _bundle_cli_version(app_dir: str) -> Optional[str]:
    """内置 CLI 的版本：cli/package.json 的 publishConfig.customPackage.version。"""
    path = os.path.join(
        app_dir, "Contents", "Resources", "app.asar.unpacked", "cli", "package.json"
    )
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    custom = (data.get("publishConfig") or {}).get("customPackage") or {}
    value = str(custom.get("version") or "").strip()
    return value or None


def _bundle_for(domain: Optional[str]) -> Optional[str]:
    return _MAC_APP_BUNDLES.get(sites.site_group(domain))


def app_version(domain: Optional[str] = None) -> str:
    """桌面端版本号（X-IDE-Version / UA 的第一段）。"""
    override = (os.environ.get("CB_GATEWAY_APP_VERSION") or "").strip()
    if override:
        return override
    bundle = _bundle_for(domain)
    if bundle:
        detected = _bundle_app_version(bundle)
        if detected:
            return detected
    return DEFAULT_APP_VERSION


def cli_version(domain: Optional[str] = None) -> str:
    """内置 CLI 版本号。CB_GATEWAY_IDE_VERSION 是历史名，继续认。"""
    override = (
        os.environ.get("CB_GATEWAY_CLI_VERSION")
        or os.environ.get("CB_GATEWAY_IDE_VERSION")
        or ""
    ).strip()
    if override:
        return override
    bundle = _bundle_for(domain)
    if bundle:
        detected = _bundle_cli_version(bundle)
        if detected:
            return detected
    return DEFAULT_CLI_VERSION


def stainless_os() -> str:
    """官方 CLI 上报的操作系统（旧字段，保留给 billing/refresh 的兼容路径）。"""
    override = (os.environ.get("CB_GATEWAY_STAINLESS_OS") or "").strip()
    if override:
        return override
    system = platform.system()
    if system == "Windows":
        return "Windows"
    if system == "Darwin":
        return "macOS"
    return "Linux"


def user_agent(domain: Optional[str] = None, channel: Optional[str] = None) -> str:
    """官方客户端 User-Agent。可用 CB_GATEWAY_USER_AGENT 整体覆盖。

    桌面端是 `WorkBuddy/<app> WorkBuddy/<app> CLI/<cli>`（官方代码里就是这么拼的，
    两段 WorkBuddy 是原样重复）；CLI 是 `CLI/<cli> CodeBuddy/<cli>`。
    实测 UA 决定后端走哪条流水线，所以它是渠道判定里最重要的一个头。
    """
    override = (os.environ.get("CB_GATEWAY_USER_AGENT") or "").strip()
    if override:
        return override
    current = channel or channel_name()
    if current == CHANNEL_CLI:
        version = cli_version(domain)
        return f"CLI/{version} CodeBuddy/{version}"
    app = app_version(domain)
    return f"WorkBuddy/{app} WorkBuddy/{app} CLI/{cli_version(domain)}"


def origin_for(domain: str) -> str:
    """按账号域选择 Origin/Referer。

    必须与 `auth_manager.backend_url_for` 同源：请求发到哪个站，Origin/Referer 就
    自称哪个站。国内账号用自己的站点域名（www.workbuddy.cn / www.codebuddy.cn），
    其余回退国际站。判定逻辑见 sites.py，两边共用一份，避免再次分叉。
    """
    return sites.site_url(domain) or _GLOBAL_ORIGIN


# ============================================================
# 请求上下文（渠道 / 会话 id）
# ============================================================

def channel_name() -> str:
    """当前生效的身份：app（桌面端）或 cli（命令行）。"""
    override = _channel_override.get()
    if override in CHANNELS:
        return override
    value = (os.environ.get("CB_GATEWAY_CHANNEL") or "").strip().lower()
    return value if value in CHANNELS else _DEFAULT_CHANNEL


def switch_channel() -> str:
    """切到另一套 profile 并返回它（渠道校验失败时的重试依据）。"""
    target = CHANNEL_CLI if channel_name() == CHANNEL_APP else CHANNEL_APP
    _channel_override.set(target)
    return target


def reset_channel() -> None:
    _channel_override.set(None)


def _stable_conversation_ids() -> bool:
    value = os.environ.get("CB_GATEWAY_CONVERSATION_ID")
    if value is None:
        return True
    return value.strip().lower() not in _DISABLED_VALUES | {"random", "per-request"}


def conversation_key(messages, api_key_id=None) -> Optional[str]:
    """从请求体里算会话指纹：首条 system 文案 + 第一条 user 文案。

    多轮续聊时这段前缀不变（历史是往后追加的），所以同一段对话稳定派生出同一个
    X-Conversation-ID；不同对话哪怕换了模型也还是不同 id。
    """
    if not isinstance(messages, list):
        return None
    system_text = ""
    user_text = ""
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if not user_text and role in ("system", "developer"):
            system_text = str(message.get("content") or "")
        elif role == "user":
            user_text = str(message.get("content") or "")
            break
    if not user_text and not system_text:
        return None
    digest = hashlib.sha256(
        f"{api_key_id or ''}\x00{system_text}\x00{user_text}".encode("utf-8", "replace")
    ).hexdigest()
    return digest


def _conversation_id_for(key: Optional[str]) -> str:
    if not key or not _stable_conversation_ids():
        return str(uuid.uuid4())
    return str(uuid.uuid5(_CONVERSATION_NAMESPACE, key))


def begin_request(key: Optional[str] = None) -> None:
    """每个客户端请求开始时调用：清掉上一轮渠道覆盖、定下本次会话 id。"""
    reset_channel()
    _conversation_id.set(_conversation_id_for(key))


def current_conversation_id() -> str:
    value = _conversation_id.get()
    return value or str(uuid.uuid4())


# ============================================================
# 请求头
# ============================================================

def _trace_id() -> str:
    return uuid.uuid4().hex


def _span_id() -> str:
    return secrets.token_hex(8)


def _domain_headers(account: dict) -> dict:
    domain = (account.get("domain") or "").strip()
    if domain:
        return {"X-Domain": domain}
    # 与官方 CLI 一致：字段缺失时用 X-No-* 标记，而不是发空值
    return {"X-No-Department-Info": "1"}


def _account_header(account: dict, field: str, header: str, no_header: str) -> dict:
    value = str(account.get(field) or "").strip()
    if value:
        return {header: value}
    return {no_header: "1"}


def _trace_headers() -> dict:
    """官方客户端的追踪头：traceparent + B3（多个变体同时发）。"""
    trace = _trace_id()
    span = _span_id()
    parent = _span_id()
    return {
        "traceparent": f"00-{trace}-{span}-01",
        "b3": f"{trace}-{span}-1-{parent}",
        "X-B3-TraceId": trace,
        "X-B3-ParentSpanId": parent,
        "X-B3-SpanId": span,
        "X-B3-Sampled": "1",
        "X-Trace-ID": trace,
    }


def common_headers(account: dict, channel: Optional[str] = None) -> dict:
    """所有 API 共享的通用指纹头（含 B3 追踪头）。"""
    domain = (account.get("domain") or "").strip()
    origin = origin_for(domain)
    request_id = _trace_id()
    span_id = _span_id()
    return {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": origin,
        "Referer": origin + "/",
        "X-Product": "SaaS",
        "User-Agent": user_agent(domain, channel),
        "X-Request-ID": request_id,
        "X-B3-TraceId": request_id,
        "X-B3-SpanId": span_id,
        "X-B3-Sampled": "1",
        "b3": f"{request_id}-{span_id}-1-",
        **_domain_headers(account),
    }


def chat_headers(account: dict, channel: Optional[str] = None) -> dict:
    """Chat 请求头：通用指纹 + 账号头 + 身份头（IDE/会话/追踪）。

    安全红线：chat 请求绝不携带 X-Refresh-Token。

    `channel` 不传就取当前上下文（默认 app）；`proxy` 在渠道校验失败时用
    `switch_channel()` 切到另一套重试。
    """
    current = channel or channel_name()
    domain = (account.get("domain") or "").strip()
    headers = common_headers(account, channel=current)
    headers.update(_account_header(account, "access_token", "Authorization", "X-No-Authorization"))
    headers.update(_account_header(account, "uid", "X-User-Id", "X-No-User-Id"))
    headers.update(
        _account_header(account, "enterprise_id", "X-Enterprise-Id", "X-No-Enterprise-Id")
    )
    if headers.get("Authorization"):
        headers["Authorization"] = f"Bearer {headers['Authorization']}"
    enterprise_id = str(account.get("enterprise_id") or "").strip()
    if enterprise_id:
        headers["X-Tenant-Id"] = enterprise_id

    # 会话三头 + 请求 id：官方 CLI 里 X-Request-ID 与 X-Conversation-Message-ID 同值，
    # 都是「这一条消息」的 id；X-Conversation-Request-ID 是「这一轮 prompt」的 id。
    conversation_id = current_conversation_id()
    message_id = uuid.uuid4().hex
    headers.update(
        {
            "X-Conversation-ID": conversation_id,
            "X-Conversation-Request-ID": message_id,
            "X-Conversation-Message-ID": message_id,
            "X-Request-ID": message_id,
            "X-Agent-Intent": "craft",
            "X-Agent-Type": "main",
            "X-IDE-Type": "WorkBuddy" if current == CHANNEL_APP else "CLI",
            "X-IDE-Name": "WorkBuddy" if current == CHANNEL_APP else "CLI",
            "X-IDE-Version": (
                app_version(domain) if current == CHANNEL_APP else cli_version(domain)
            ),
            "x-codebuddy-request": "1",
            "X-Private-Data": "true",
        }
    )
    if current == CHANNEL_APP:
        # 桌面端自己打的这两项：X-Agent-Purpose 把它标成「会话请求」，
        # acp-connection-id 是本地 ACP 连接的标识（客户端随机生成）。
        headers.update(
            {
                "X-Agent-Purpose": "conversation",
                "acp-connection-id": str(uuid.uuid4()),
                "X-Custom-Channel-Id": "unknown",
            }
        )
    headers.update(_trace_headers())
    return headers


def billing_headers(account: dict) -> dict:
    """Billing 接口（余额/积分）请求头指纹。"""
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Product": "SaaS",
        "User-Agent": user_agent((account.get("domain") or "").strip()),
        **_domain_headers(account),
    }
    headers.update(_account_header(account, "access_token", "Authorization", "X-No-Authorization"))
    if headers.get("Authorization"):
        headers["Authorization"] = f"Bearer {headers['Authorization']}"
    headers.update(_account_header(account, "uid", "X-User-Id", "X-No-User-Id"))
    headers.update(
        _account_header(account, "enterprise_id", "X-Enterprise-Id", "X-No-Enterprise-Id")
    )
    enterprise_id = str(account.get("enterprise_id") or "").strip()
    if enterprise_id:
        headers["X-Tenant-Id"] = enterprise_id
    return headers


def refresh_headers(account: dict) -> dict:
    """Token 刷新接口请求头。X-Refresh-Token 只允许出现在这里。"""
    headers = common_headers(account)
    headers["Accept"] = "application/json"
    headers["Cache-Control"] = "no-cache"
    headers["Pragma"] = "no-cache"
    access_token = str(account.get("access_token") or "").strip()
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    headers["X-Refresh-Token"] = str(account.get("refresh_token") or "")
    headers["X-Auth-Refresh-Source"] = "plugin"
    headers.update(_account_header(account, "uid", "X-User-Id", "X-No-User-Id"))
    headers.update(
        _account_header(account, "enterprise_id", "X-Enterprise-Id", "X-No-Enterprise-Id")
    )
    return headers
