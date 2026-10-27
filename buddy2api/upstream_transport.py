"""上游出站兜底：直连建连失败时，换本机代理重发一次。

排障背景（2026-09-24）：本机到 www.workbuddy.ai（43.160.158.125:443）的 TCP 建连会
成串黑洞 —— 实测连续 60 次 SYN 全部超时、curl 直连返回 000，而同时段 DNS 正常
（p50 3ms）、国内站 p50 0.02s、github 正常。网关日志里的表现就是成串
`[upstream_disconnect] ConnectTimeout` 502（当日 313 条）。同一时刻系统代理
（Clash Verge 写在 Wi-Fi 上的 127.0.0.1:7897）那条出口是通的 —— 用户手动开
WorkBuddy 客户端走的正是它。

所以这里给聊天转发加一层兜底：**直连建连失败**时，换本机代理重发同一个请求。

为什么只兜「建连」：请求体是 bytes（httpx.ByteStream），可以安全重发；一旦上游已经
回包就不能重试 —— 流式正文可能已经下发给客户端，重发会重复执行工具调用。建连类失败
发生在拿到响应之前，所以是安全的。

代理地址候选（都不写死端口，端口常变）：
1. `CB_GATEWAY_UPSTREAM_PROXY`：显式配置；
2. `CODEBUDDY_SERVICE_PROXY_URL`：WorkBuddy 桌面端自己写的服务代理；
3. 系统代理（macOS `scutil --proxy`）。

三者都没有时不做兜底，行为与以前完全一致。

国际站默认一律走代理（2026-09-27）：本机直连出口是运营商 IP，而代理出口在海外。
多个国际版账号在直连出口下被上游按账号拒绝（403 + code 11140，官方客户端同样报错），
逐项排除请求体/模型/token/指纹头后，出站 IP 是唯一未被排除的变量。见 `transport_for`。
用 `CB_GATEWAY_INTL_PROXY=off` 可关掉，退回原来的「直连优先」。
"""

from __future__ import annotations

import os
import ssl
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Optional

import httpx

import buddy2api.sites as sites

EXPLICIT_PROXY_VAR = "CB_GATEWAY_UPSTREAM_PROXY"
CLIENT_PROXY_VAR = "CODEBUDDY_SERVICE_PROXY_URL"
# 国际站出站代理开关：设为 off/none/false/0 时退回原来的「直连优先」。
INTL_PROXY_DISABLED_VAR = "CB_GATEWAY_INTL_PROXY"
_DISABLED_VALUES = {"off", "none", "false", "0", ""}

# 建连类失败。注意 httpx 的 ConnectTimeout 继承 TimeoutException 而**不是** ConnectError，
# 两个都要接；ReadTimeout/WriteTimeout 不在此列（那是拿到连接之后的事）。
CONNECT_FAILURES = (httpx.ConnectError, httpx.ConnectTimeout)

# 官方客户端的 TLS 参数（2026-10-27 实测）。
#
# 发 `/v2/chat/completions` 的是内置 CLI：axios + `https.Agent({keepAlive:true,
# keepAliveMsecs:2000})`，即 **Node + OpenSSL**、只走 HTTP/1.1，没有 ALPN 扩展。
# 客户端自带的 Node 运行时是 v22.22.2 / OpenSSL 3.5.5（本机
# `~/.workbuddy-ai/binaries/node/versions/22.22.2-6/bin/node`），下面这串就是它的
# `tls.DEFAULT_CIPHERS` 原文；本项目 venv 的 Python 也是 OpenSSL 3.5.5。
#
# 实测（`src/fingerprint_probe.py`，抓 ClientHello 算 JA3）：只把 cipher 串换成这
# 串、并让 SSLContext 不发 ALPN，Python 的 JA3 与官方 Node 运行时的 JA3
# **完全一致**（md5 46648fde641eb8c4155d71526eb5ebae）；不换就是「Python/OpenSSL
# 默认 + ALPN http/1.1」的另一枚指纹（503dcc056cabbab1f56ca50b2c526c3e）。
NODE_DEFAULT_CIPHERS = (
    "TLS_AES_256_GCM_SHA384:TLS_CHACHA20_POLY1305_SHA256:TLS_AES_128_GCM_SHA256:"
    "ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES128-GCM-SHA256:"
    "ECDHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-AES256-GCM-SHA384:"
    "DHE-RSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-SHA256:DHE-RSA-AES128-SHA256:"
    "ECDHE-RSA-AES256-SHA384:DHE-RSA-AES256-SHA384:ECDHE-RSA-AES256-SHA256:"
    "DHE-RSA-AES256-SHA256:HIGH:!aNULL:!eNULL:!EXPORT:!DES:!RC4:!MD5:!PSK:!SRP:!CAMELLIA"
)

# 官方 Node 运行时的 JA3 指纹（基准线，probe 与测试都对着它比）。
NODE_JA3_MD5 = "46648fde641eb8c4155d71526eb5ebae"


class _NoAlpnContext(ssl.SSLContext):
    """不发 ALPN 的 SSLContext。

    httpcore 在建连时一定会调 `ssl_context.set_alpn_protocols(["http/1.1"])`，
    而 Node 的 `https.Agent` 不带 ALPNProtocols —— 握手包里多一个 ALPN 扩展
    （JA3 里多一个 16）就是「这不是 Node 客户端」的直接证据。这里把该调用变
    no-op，保住 JA3。
    """

    def set_alpn_protocols(self, protocols) -> None:  # noqa: D401 - 故意空实现
        return None


def build_ssl_context(verify: bool = True) -> ssl.SSLContext:
    """造一个与官方客户端运行时同指纹的 TLS 上下文（cipher 串 + 无 ALPN）。

    `verify=False` 只给离线探针/测试用（对端是本地自签证书）。
    """
    context = _NoAlpnContext(ssl.PROTOCOL_TLS_CLIENT)
    if verify:
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        # load_default_certs 认 SSL_CERT_FILE / SSL_CERT_DIR，与 httpx 默认行为一致
        context.load_default_certs()
    else:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    context.set_ciphers(NODE_DEFAULT_CIPHERS)
    return context


@lru_cache(maxsize=1)
def ssl_context() -> ssl.SSLContext:
    """出站统一使用的 TLS 上下文（校验对端证书）。"""
    return build_ssl_context(True)

# 同一代理的兜底日志最多每分钟一行：故障窗口里每个请求都会触发，不节流会刷屏。
_REPORT_INTERVAL_SECONDS = 60.0
_last_report_at = 0.0
_fallback_count = 0

_system_proxy_cache: Optional[list[str]] = None


def fallback_count() -> int:
    """累计触发过多少次「直连失败 → 走代理」。"""
    return _fallback_count


def _scutil_value(text: str, key: str) -> str:
    prefix = f"{key} : "
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return ""


def _system_proxies() -> list[str]:
    """macOS 系统代理（Clash 这类工具会写在这里）。非 macOS 或读不到时返回空列表。"""
    global _system_proxy_cache
    if _system_proxy_cache is not None:
        return _system_proxy_cache
    found: list[str] = []
    try:
        completed = subprocess.run(
            ["scutil", "--proxy"], capture_output=True, text=True, timeout=5
        )
        text = completed.stdout
    except (OSError, subprocess.SubprocessError):
        text = ""
    for kind in ("HTTPS", "HTTP"):
        if f"{kind}Enable : 1" not in text:
            continue
        host = _scutil_value(text, f"{kind}Proxy")
        port = _scutil_value(text, f"{kind}Port")
        if host and port:
            found.append(f"http://{host}:{port}")
    _system_proxy_cache = list(dict.fromkeys(found))
    return _system_proxy_cache


def proxy_for_retry() -> Optional[str]:
    """直连失败时用来兜底的代理地址；没有可用候选时返回 None。"""
    for candidate in (
        os.environ.get(EXPLICIT_PROXY_VAR, "").strip(),
        os.environ.get(CLIENT_PROXY_VAR, "").strip(),
        *_system_proxies(),
    ):
        if candidate:
            return candidate
    return None


def intl_proxy_enabled() -> bool:
    """国际站是否强制走代理（默认是，可用 CB_GATEWAY_INTL_PROXY 关闭）。"""
    value = os.environ.get(INTL_PROXY_DISABLED_VAR)
    if value is None:
        return True
    return value.strip().lower() not in _DISABLED_VALUES


def report_fallback(proxy: str, exc: BaseException) -> None:
    """记录一次兜底（节流到每分钟一行）。"""
    global _last_report_at, _fallback_count
    _fallback_count += 1
    now = time.monotonic()
    if now - _last_report_at < _REPORT_INTERVAL_SECONDS:
        return
    _last_report_at = now
    print(
        f"[upstream] 直连建连失败（{type(exc).__name__}: {exc}），改用代理重试: {proxy}",
        file=sys.stderr,
    )


class FallbackTransport(httpx.AsyncBaseTransport):
    """直连优先；建连失败时改用代理把同一个请求重发一次。

    放在传输层而不是客户端层，是为了让「新建一个 AsyncClient」的写法保持不变 ——
    聊天转发的流式与非流式两条路径都只是换了个 transport。
    """

    def __init__(self, direct: httpx.AsyncBaseTransport, proxy: str):
        self._direct = direct
        self._proxy = proxy
        self._proxy_transport: Optional[httpx.AsyncBaseTransport] = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        try:
            return await self._direct.handle_async_request(request)
        except CONNECT_FAILURES as exc:
            report_fallback(self._proxy, exc)
        if self._proxy_transport is None:
            self._proxy_transport = httpx.AsyncHTTPTransport(
                proxy=self._proxy, verify=ssl_context()
            )
        return await self._proxy_transport.handle_async_request(request)

    async def aclose(self) -> None:
        await self._direct.aclose()
        if self._proxy_transport is not None:
            await self._proxy_transport.aclose()


def direct_transport() -> httpx.AsyncHTTPTransport:
    """直连传输层（TLS 指纹对齐官方客户端）。"""
    return httpx.AsyncHTTPTransport(verify=ssl_context())


def fallback_transport() -> Optional[FallbackTransport]:
    """有代理候选时返回兜底传输层；没有候选返回 None（调用方保持原样直连）。"""
    proxy = proxy_for_retry()
    if proxy is None:
        return None
    return FallbackTransport(direct_transport(), proxy)


class AlwaysProxyTransport(httpx.AsyncBaseTransport):
    """一律走代理，不做直连。

    给国际站用：本机直连出口是运营商 IP，代理出口在海外。多个国际版账号在直连出口下
    被上游按账号拒绝（403 + code 11140，官方客户端同样报错），而请求体、模型、token、
    指纹头逐项排除后都不成立 —— 出站 IP 是唯一未被排除的变量。

    与 FallbackTransport 的区别是**不先直连**：直连那一次本身就是被拒绝的那一次，
    先试一遍只是白白多一次失败往返。
    """

    def __init__(self, proxy: str):
        self._proxy = proxy
        self._transport: httpx.AsyncBaseTransport = httpx.AsyncHTTPTransport(
            proxy=proxy, verify=ssl_context()
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self._transport.handle_async_request(request)

    async def aclose(self) -> None:
        await self._transport.aclose()


def transport_for(account: Optional[dict] = None) -> Optional[httpx.AsyncBaseTransport]:
    """按账号站点选出站传输层。

    - 国际版账号：有代理候选就一律走代理（见 AlwaysProxyTransport）；
    - 其余（国内站、自定义 relay）：保持直连优先 + 建连失败兜底；
    - 没有代理候选：返回 None，调用方照旧直连（行为与以前完全一致）。
    """
    proxy = proxy_for_retry()
    if proxy is None:
        return None
    if intl_proxy_enabled() and sites.is_intl_domain((account or {}).get("domain")):
        return AlwaysProxyTransport(proxy)
    return FallbackTransport(direct_transport(), proxy)


# ============================================================
# 出站客户端：复用连接（对齐官方客户端的 keep-alive agent）
# ============================================================

class _NoCookies(httpx.Cookies):
    """不做 cookie 存储的 jar（永远是空的）。

    官方客户端是 axios / Node fetch，没有 cookie jar —— 真实客户端从不带 Cookie 头。
    网关这边客户端是共享的：如果沿用 httpx 的 jar，A 账号响应里的 Set-Cookie 会跟着
    B 账号发出去（既是串号风险，也是「这不像官方客户端」的特征）。
    """

    def extract_cookies(self, response) -> None:  # noqa: D401 - 故意空实现
        return None

    def set_cookie_header(self, request) -> None:
        return None


class _SharedClient(httpx.AsyncClient):
    """共享出站客户端，形态对齐官方客户端（axios / Node fetch）发出的报文。

    两处刻意的偏差，都在线上报文里看得见：

    - httpx 默认会带 `Accept-Encoding` / `Accept` / `User-Agent`，而官方客户端一个都
      不发（Node 的 http 模块不加 accept-encoding，UA 由调用方给）。这里把默认值摘掉，
      头全由 `fingerprint` 显式给；
    - httpx 把客户端默认头排在请求头**前面**，官方客户端（Node）是请求头在前、
      `Host`/`Connection`/`Content-Length` 这些自动头在后。这里把合并顺序反过来，
      只剩 `Host` 的位置差异（h11 强制 Host 打第一行，见 docs/design/fingerprint.md）。
    """

    def __init__(self, **kwargs):
        kwargs.setdefault("headers", {})
        super().__init__(**kwargs)
        self._cookies = _NoCookies()
        for name in ("accept", "accept-encoding", "user-agent"):
            self.headers.pop(name, None)
        # 连接头留着（Node 自己也发），但要排在请求头之后
        connection = self.headers.pop("connection", None)
        self.headers.pop("connection", None)
        if connection is not None:
            self.headers["Connection"] = connection

    def _merge_headers(self, headers=None):
        merged = httpx.Headers(headers)
        merged.update(self.headers)
        return merged


# 官方客户端是一个 `https.Agent({keepAlive:true, keepAliveMsecs:2000})`：连接复用、
# 空闲约 2s 回收。以前网关每条上游请求新建一个 AsyncClient，等于每个请求都做一次
# 完整 TCP+TLS 握手（真实的官方客户端不会这样，流量特征上也很显眼）。
_CLIENT_LIMITS = httpx.Limits(
    max_connections=64, max_keepalive_connections=16, keepalive_expiry=4.0
)
_CLIENTS: "dict[tuple, httpx.AsyncClient]" = {}


def _client_key(account: Optional[dict], timeout) -> tuple:
    domain = (account or {}).get("domain")
    return (
        sites.site_group(domain),
        sites.is_intl_domain(domain),
        proxy_for_retry(),
        intl_proxy_enabled(),
        str(timeout),
    )


def build_client(
    timeout=httpx.Timeout(300.0),
    *,
    transport: Optional[httpx.AsyncBaseTransport] = None,
    verify=None,
) -> httpx.AsyncClient:
    """按生产口径造一个出站客户端（共享客户端与离线探针共用同一份配置）。"""
    kwargs = {"timeout": timeout, "headers": {}, "limits": _CLIENT_LIMITS}
    if transport is not None:
        kwargs["transport"] = transport
    else:
        kwargs["verify"] = ssl_context() if verify is None else verify
    return _SharedClient(**kwargs)


def shared_client(account: Optional[dict] = None, timeout=httpx.Timeout(300.0)) -> httpx.AsyncClient:
    """按（站点、代理、超时）复用同一个出站客户端。

    - 头一律由调用方给全：`headers={}` 关掉 httpx 的默认头（`accept-encoding` /
      `user-agent` / `connection` 这些真实客户端都不发）；
    - 没有代理候选时把 `verify` 传进去，让 httpx 自建直连传输层时也用对齐过的
      TLS 上下文（要保留 `trust_env` 认环境代理的旧行为）。
    """
    key = _client_key(account, timeout)
    cached = _CLIENTS.get(key)
    if cached is not None:
        return cached
    client = build_client(timeout, transport=transport_for(account))
    _CLIENTS[key] = client
    return client


@asynccontextmanager
async def client_scope(account: Optional[dict] = None, timeout=httpx.Timeout(300.0)):
    """`async with` 形式拿到共享客户端（退出时不关，连接留给下一个请求）。"""
    yield shared_client(account, timeout)


async def aclose_clients() -> None:
    """关闭所有共享客户端（测试/退出用）。"""
    clients = list(_CLIENTS.values())
    _CLIENTS.clear()
    for client in clients:
        try:
            await client.aclose()
        except Exception:  # pragma: no cover - 关闭失败不影响退出
            pass
