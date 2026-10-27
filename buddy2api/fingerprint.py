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

## 2026-10-27：按官方客户端源码逐字节对齐（不再只是「那几个头在不在」）

对齐依据是 `app.asar` 里官方客户端的**真实实现**，不是猜测：

- `cli/dist/codebuddy-headless.js`：真正发 `/v2/chat/completions` 的是内置 CLI，
  走 **axios 1.16.1 + `https.Agent({keepAlive:true, keepAliveMsecs:2000})`**，
  即 **Node/OpenSSL 的 TLS 栈**（不是 Chromium，也不是 HTTP/2），TLS 层的对齐见
  `upstream_transport.ssl_context()`。
- `main/daemon-bootstrap.js` 的 `Jr()` / `Yr()` / `Zr()` 是桌面端主进程那条 fetch
  路径的头拼装（`WorkBuddy/<app> WorkBuddy/<app> CLI/<cli>` UA 就出自这里）。
- `main/module.app-server.js` 的 `AuthStateHeaderProvider` 提供
  `X-Custom-Channel-Id: unknown` 与 `X-Device-Id-List: {"qimei36":...}`；
  `main/code-cache.js` 里 `X-No-*` 标记的真实取值是 `"true"`（不是 `"1"`）。
- 会话 id 生成器 `nH = uuidv7`：`X-Conversation-ID`、`X-Request-ID`、
  `X-Conversation-Message-ID` 全是 **uuid 版本 7**（前 48 位是毫秒时间戳）。
  之前网关发的是 uuid5（会话 id，含 version=5 的确定位）和 uuid4（消息 id），
  版本位和「时间戳与请求时刻是否对得上」两条都能被一眼看穿，这里改成 uuid7。
- `X-Request-ID` 与 `X-Conversation-Message-ID` 同值（CLI 的
  `buildConversationHeaders`：`REQUEST_ID_HEADER = messageId`），而
  `X-Conversation-Request-ID` 是**一轮 prompt** 的 id，同一轮里的多次模型调用
  共用一个值（工具循环），不是每条 HTTP 请求换一个。

### 头名大小写与顺序

官方客户端的头名实际是**全小写**的：axios 的 `AxiosHeaders` 用 `normalizeHeader`
把 key 统一转小写，Electron 那条 fetch 路径的 `Headers.set()` 同样小写；只有 Node
自己补的 `Host` / `Connection` / `Content-Length` 是首字母大写且排在最后（实测见
`scripts/fingerprint_probe.py` 的基线）。所以本模块所有头名一律小写、按官方拼装
顺序插入 —— 之前那种 `X-IDE-Type` 的 Title-Case 是任何真实客户端都不会出现的形态。

## 两套 profile

- `app`（默认）：桌面端 WorkBuddy。UA `WorkBuddy/<app> WorkBuddy/<app> CLI/<cli>`，
  带 `x-requested-with` / `acp-connection-id` / `x-codebuddy-request` /
  `traceparent`+`b3` 家族（桌面端主进程的 fetch 路径就是这么发的）。
- `cli`：内置 CLI（axios）的形态。UA `CLI/<cli> CodeBuddy/<cli>`，没有
  `acp-connection-id` / `x-codebuddy-request` / `x-requested-with` / 追踪头
  （OTel 默认不开），`accept` 用 axios 的默认值。

渠道校验失败（11128）时 `proxy` 会调 `switch_channel()` 换另一套 profile 重试一次
（见 `_plan_channel_retry`）：11128 说的是「你这次的头/体不像官方客户端」，跟账号本身
无关，所以既不该给账号记失败，也不该耗掉换号额度。

## 环境变量

  CB_GATEWAY_CHANNEL=app|cli              默认身份（默认 app）
  CB_GATEWAY_APP_VERSION                  桌面端版本，覆盖自动探测
  CB_GATEWAY_CLI_VERSION                  CLI 版本，覆盖自动探测（旧名 CB_GATEWAY_IDE_VERSION）
  CB_GATEWAY_USER_AGENT                   整体覆盖 User-Agent（最高优先级）
  CB_GATEWAY_CONVERSATION_ID=stable|random  会话 id 是否按会话稳定（默认 stable）
  CB_GATEWAY_QIMEI36                      设备标识；默认按站点读官方客户端的缓存文件，
                                          off/none 可整个摘掉这个头
  CB_GATEWAY_STAINLESS_OS                 历史兼容开关，可忽略
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import plistlib
import secrets
import threading
import time
import uuid
from collections import OrderedDict
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
# 所以 x-ide-version / UA 必须按账号站点取，否则国内账号会自称一个国际版版本号。
_MAC_APP_BUNDLES = {
    sites.SITE_INTERNATIONAL: "/Applications/WorkBuddy AI.app",
    sites.SITE_DOMESTIC: "/Applications/WorkBuddy.app",
}

# 站点 → 官方客户端的设备标识缓存（`X-Device-Id-List: {"qimei36": ...}` 的来源）。
# 两个 app 是独立 product，qimei36 不能跨站复用：国际版客户端今天是 cache miss
# （日志里 `qimei36 from cache: miss`），所以国际账号本来就不该带这个头。
_QIMEI_CACHE_FILES = {
    sites.SITE_DOMESTIC: "~/.workbuddy/qimei-cache.json",
    sites.SITE_INTERNATIONAL: "~/.workbuddy-ai/qimei-cache.json",
}

# 渠道校验必需的三个会话头。头名按官方客户端形态写小写；HTTP 头大小写不敏感，
# `CONVERSATION_HEADERS` 保持原有名字给调用方/测试做大小写不敏感查找用。
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

# 单请求的上下文：渠道覆盖（11128 时切换）、会话 id、本轮 prompt 的 request id。
# 用 ContextVar 是为了让 auth_manager.build_headers 不必多带参数，同时请求之间互不串味。
_channel_override: ContextVar[Optional[str]] = ContextVar(
    "buddy2api_fingerprint_channel", default=None
)
_conversation_id: ContextVar[Optional[str]] = ContextVar(
    "buddy2api_fingerprint_conversation", default=None
)
_conversation_request_id: ContextVar[Optional[str]] = ContextVar(
    "buddy2api_fingerprint_conversation_request", default=None
)


# ============================================================
# uuid v7
# ============================================================

# 官方客户端用的是 uuidv7（`uuidv7` npm 包，`nH()`）。与 uuid4 的区别是对外的：
# 版本位是 7，且高 48 位是毫秒时间戳 —— 拿 uuid4 冒充会被「版本位」或「时间戳与请求
# 时刻对不上」直接识破，所以这里按 RFC 9562 自己实现一个单调版本。
_UUID7_LOCK = threading.Lock()
_uuid7_last_ms = -1
_uuid7_counter = 0


def uuid7() -> uuid.UUID:
    """RFC 9562 uuid v7（48 位毫秒时间戳 + 单调计数器 + 随机尾部）。"""
    global _uuid7_last_ms, _uuid7_counter
    with _UUID7_LOCK:
        now_ms = time.time_ns() // 1_000_000
        if now_ms > _uuid7_last_ms:
            _uuid7_last_ms = now_ms
            _uuid7_counter = secrets.randbits(12)
        else:
            now_ms = _uuid7_last_ms
            _uuid7_counter += 1
            if _uuid7_counter > 0xFFF:
                # 同一毫秒内计数器用尽：把时间戳推到下一毫秒，保持单调
                _uuid7_last_ms += 1
                now_ms = _uuid7_last_ms
                _uuid7_counter = 0
        rand_a = _uuid7_counter & 0xFFF
        rand_b = secrets.randbits(62)
    value = (now_ms & 0xFFFFFFFFFFFF) << 80
    value |= 0x7 << 76
    value |= rand_a << 64
    value |= 0b10 << 62
    value |= rand_b
    return uuid.UUID(int=value)


def uuid7_hex() -> str:
    """uuid7 去掉短横线（官方客户端的 message id 就是 `uuidv7().replace(/-/g,'')`）。"""
    return uuid7().hex


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
    """桌面端版本号（x-ide-version / UA 的第一段）。"""
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

    桌面端是 `WorkBuddy/<app> WorkBuddy/<app> CLI/<cli>`（官方 `Jr()` 里就是这么拼的，
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
    """按账号域给出站点 origin。

    保留给需要 Origin/Referer 的路径（chat 已不再发送：官方客户端的 chat 请求里
    没有这两个头，Node/Electron 主进程的 fetch 也不会自动加）。
    """
    return sites.site_url(domain) or _GLOBAL_ORIGIN


# ============================================================
# 设备标识（X-Device-Id-List / X-Custom-Channel-Id）
# ============================================================

@lru_cache(maxsize=8)
def _qimei_from_file(path: str, stamp: float) -> Optional[str]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    value = str(data.get("qimei36") or "").strip()
    return value or None


def qimei36(domain: Optional[str] = None) -> Optional[str]:
    """本机官方客户端缓存的 qimei36（设备标识）。

    取自客户端自己写的缓存文件；读不到就**不发**这个头 —— 真实客户端在 cache miss
    时同样不发（国际版今天的日志就是 `qimei36 from cache: miss`），发一个编造的值
    反而比不发送更像伪造。`CB_GATEWAY_QIMEI36` 可显式覆盖。
    """
    override = (os.environ.get("CB_GATEWAY_QIMEI36") or "").strip()
    if override:
        # off/none/空 用来摘掉这个头（排障时的开关），其余值当显式覆盖
        return None if override.lower() in _DISABLED_VALUES else override
    template = _QIMEI_CACHE_FILES.get(sites.site_group(domain))
    if not template:
        return None
    path = os.path.expanduser(template)
    try:
        stamp = os.stat(path).st_mtime
    except OSError:
        return None
    return _qimei_from_file(path, stamp)


def _json_compact(payload: dict) -> str:
    """与 Node 的 `JSON.stringify` 同形（无空格、保持插入顺序）。"""
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


# ============================================================
# 请求上下文（渠道 / 会话 id / 本轮 request id）
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

    多轮续聊时这段前缀不变（历史是往后追加的），所以同一段对话稳定映射到同一个
    X-Conversation-ID；不同对话哪怕换了模型也还是不同 id。密钥只用于把这批会话与
    别的 API Key 隔开，不出现在任何出站头里。
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


# 会话键 → 已发出的 X-Conversation-ID。存的是 uuid7（而不是按内容哈希出 uuid5）：
# 版本位要对，而且同一个会话在多次请求里必须复用同一个 id（真实客户端一个会话一个
# id），所以这里缓存首次生成的值。进程重启后同一段对话会换 id —— 真实客户端的会话
# 也随客户端重启换 id，属于同类行为。
_CONVERSATION_CACHE: "OrderedDict[str, str]" = OrderedDict()
_CONVERSATION_CACHE_MAX = 512


def _conversation_id_for(key: Optional[str]) -> str:
    if not key or not _stable_conversation_ids():
        return str(uuid7())
    cached = _CONVERSATION_CACHE.get(key)
    if cached:
        _CONVERSATION_CACHE.move_to_end(key)
        return cached
    value = str(uuid7())
    _CONVERSATION_CACHE[key] = value
    while len(_CONVERSATION_CACHE) > _CONVERSATION_CACHE_MAX:
        _CONVERSATION_CACHE.popitem(last=False)
    return value


def begin_request(key: Optional[str] = None) -> None:
    """每个客户端请求开始时调用：清渠道覆盖，定会话 id 与本轮 request id。

    一轮 prompt 里的多次模型调用（工具循环、停转重打）必须共用同一个
    X-Conversation-Request-ID —— 官方客户端就是这么发的，每条 HTTP 一个 uuid
    是「这个 id 不是客户端自己生成的」的明显特征。
    """
    reset_channel()
    _conversation_id.set(_conversation_id_for(key))
    _conversation_request_id.set(uuid7_hex())


def current_conversation_id() -> str:
    value = _conversation_id.get()
    return value or str(uuid7())


def current_conversation_request_id() -> str:
    value = _conversation_request_id.get()
    return value or uuid7_hex()


# ============================================================
# 请求头
# ============================================================

def _trace_id() -> str:
    return uuid.uuid4().hex


def _span_id() -> str:
    return secrets.token_hex(8)


def _identity_headers(account: dict) -> dict:
    """账号身份头：x-user-id / x-enterprise-id / x-tenant-id / x-domain / x-userinfo。

    缺字段时按官方客户端的约定发 `x-no-*: true`（`main/code-cache.js` 里
    `"X-No-User-Id": "true"` —— 之前网关发的是 `1`，形态不对）。
    `x-userinfo` 是 `runIdentityHeaders()` 的产物：base64(JSON{uin, owner_uin,
    id_source, token_source})，只有企业账号才带（个人号 uid 有了但既没企业 id、
    也没有 idSource/method，官方实现同样不发）。
    """
    headers: dict = {}
    uid = str(account.get("uid") or "").strip()
    enterprise_id = str(account.get("enterprise_id") or "").strip()
    domain = (account.get("domain") or "").strip()

    headers["x-user-id" if uid else "x-no-user-id"] = uid or "true"
    headers["x-enterprise-id" if enterprise_id else "x-no-enterprise-id"] = (
        enterprise_id or "true"
    )
    if enterprise_id:
        headers["x-tenant-id"] = enterprise_id
        claim = {"uin": uid}
        if enterprise_id:
            claim["owner_uin"] = enterprise_id
        headers["x-userinfo"] = base64.b64encode(
            _json_compact(claim).encode("utf-8")
        ).decode("ascii")
    headers["x-domain" if domain else "x-no-department-info"] = domain or "true"
    return headers


def _device_headers(account: dict) -> dict:
    """设备标识头（`AuthStateHeaderProvider` 的产物）。"""
    headers: dict = {"x-custom-channel-id": "unknown"}
    device_id = qimei36((account.get("domain") or "").strip() or None)
    if device_id:
        headers["x-device-id-list"] = _json_compact({"qimei36": device_id})
    return headers


def _trace_headers() -> dict:
    """桌面端主进程的追踪头：traceparent + B3（多个变体同时发）。"""
    trace = _trace_id()
    span = _span_id()
    parent = _span_id()
    return {
        "traceparent": f"00-{trace}-{span}-01",
        "b3": f"{trace}-{span}-1-{parent}",
        "x-b3-traceid": trace,
        "x-b3-parentspanid": parent,
        "x-b3-spanid": span,
        "x-b3-sampled": "1",
        "x-trace-id": trace,
    }


def _authorization(account: dict) -> dict:
    token = str(account.get("access_token") or "").strip()
    if token:
        return {"authorization": f"Bearer {token}"}
    return {"x-no-authorization": "true"}


def common_headers(account: dict, channel: Optional[str] = None) -> dict:
    """所有 API 共享的通用指纹头（身份 + 设备 + 产品 + UA）。

    头名一律小写、按官方拼装顺序插入：真实客户端（axios / fetch）不会发出
    `X-IDE-Type` 这种 Title-Case 形态。
    """
    current = channel or channel_name()
    domain = (account.get("domain") or "").strip()
    headers = {"accept": "application/json", "content-type": "application/json"}
    headers.update(_identity_headers(account))
    headers.update(
        {
            "x-ide-type": "WorkBuddy" if current == CHANNEL_APP else "CLI",
            "x-ide-name": "WorkBuddy" if current == CHANNEL_APP else "CLI",
            "x-ide-version": (
                app_version(domain) if current == CHANNEL_APP else cli_version(domain)
            ),
            "x-private-data": "true",
            "x-product": "SaaS",
        }
    )
    headers.update(_device_headers(account))
    headers.update(_authorization(account))
    headers["user-agent"] = user_agent(domain, current)
    return headers


def chat_headers(account: dict, channel: Optional[str] = None) -> dict:
    """Chat 请求头：会话/身份/IDE/产品/追踪，按官方客户端的插入顺序。

    安全红线：chat 请求绝不携带 x-refresh-token。

    `channel` 不传就取当前上下文（默认 app）；`proxy` 在渠道校验失败时用
    `switch_channel()` 切到另一套重试。
    """
    current = channel or channel_name()
    domain = (account.get("domain") or "").strip()
    # 官方客户端的 message id：uuidv7 去短横线；x-request-id 与它同值。
    message_id = uuid7_hex()

    if current == CHANNEL_CLI:
        headers: dict = {
            # axios 的默认 accept（桌面端那条 fetch 路径只写 application/json）
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
        }
        headers.update(_identity_headers(account))
        headers.update(
            {
                "x-conversation-id": current_conversation_id(),
                "x-conversation-request-id": current_conversation_request_id(),
                "x-conversation-message-id": message_id,
                "x-request-id": message_id,
                "x-agent-intent": "craft",
                "x-agent-type": "main",
                "x-ide-type": "CLI",
                "x-ide-name": "CLI",
                "x-ide-version": cli_version(domain),
                "x-private-data": "true",
                "x-product": "SaaS",
            }
        )
        headers.update(_device_headers(account))
        headers.update(_authorization(account))
        headers["user-agent"] = user_agent(domain, current)
        return headers

    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "x-requested-with": "XMLHttpRequest",
        "x-conversation-id": current_conversation_id(),
        "x-conversation-request-id": current_conversation_request_id(),
        # 本地 ACP 连接 id（桌面端每个会话一个），客户端随机生成
        "acp-connection-id": str(uuid.uuid4()),
        "x-conversation-message-id": message_id,
        "x-request-id": message_id,
        "x-agent-intent": "craft",
        "x-agent-type": "main",
        # 桌面端会话固定标成 conversation 用途
        "x-agent-purpose": "conversation",
        "x-codebuddy-request": "1",
        "x-ide-type": "WorkBuddy",
        "x-ide-name": "WorkBuddy",
        "x-ide-version": app_version(domain),
        "x-private-data": "true",
        "x-product": "SaaS",
    }
    headers.update(_identity_headers(account))
    headers.update(_device_headers(account))
    headers.update(_authorization(account))
    headers["user-agent"] = user_agent(domain, current)
    headers.update(_trace_headers())
    return headers


def billing_headers(account: dict) -> dict:
    """Billing 接口（余额/积分）请求头指纹。"""
    headers = common_headers(account)
    headers["accept"] = "application/json"
    return headers


def refresh_headers(account: dict) -> dict:
    """Token 刷新接口请求头。x-refresh-token 只允许出现在这里。"""
    headers = common_headers(account)
    headers["accept"] = "application/json"
    headers["cache-control"] = "no-cache"
    headers["pragma"] = "no-cache"
    headers["x-refresh-token"] = str(account.get("refresh_token") or "")
    headers["x-auth-refresh-source"] = "plugin"
    return headers
