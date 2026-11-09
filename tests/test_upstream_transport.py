"""上游出站兜底：直连建连失败时换本机代理重发。

回归（2026-09-24）：本机到 www.workbuddy.ai（43.160.158.125:443）的 TCP 建连会成串
黑洞 —— 实测连续 60 次 SYN 全部超时，网关当日记录 313 条
`[upstream_disconnect] ConnectTimeout` 502。同时段系统代理那条出口是通的。

本测试钉死三条边界：
1. 只有建连类失败才兜底（ConnectTimeout 也要接住 —— 它继承 TimeoutException 而非
   ConnectError，漏接就白改）；
2. 拿到响应之后的问题（读超时等）原样抛给调用方，绝不重发 —— 流式正文可能已下发，
   重发会重复执行工具调用；
3. 没有代理候选时行为与以前完全一致（transport 为 None，直连），不能悄悄改行为。
"""

import asyncio

import httpx
import pytest

import buddy2api.upstream_transport as upstream_transport


def _request() -> httpx.Request:
    return httpx.Request("POST", "https://www.workbuddy.ai/v2/chat/completions", json={"model": "m"})


class RecordingTransport(httpx.AsyncBaseTransport):
    """记录被调用的次数与请求；按需抛错或返回响应。"""

    def __init__(self, error=None, status_code=200):
        self.error = error
        self.status_code = status_code
        self.calls = 0
        self.closed = False

    async def handle_async_request(self, request):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return httpx.Response(self.status_code, content=b"ok", request=request)

    async def aclose(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    monkeypatch.setattr(upstream_transport, "_fallback_count", 0)
    monkeypatch.setattr(upstream_transport, "_last_report_at", 0.0)
    monkeypatch.setattr(upstream_transport, "_system_proxy_cache", None)


def _send(transport, request=None):
    async def run():
        response = await transport.handle_async_request(request or _request())
        return response.status_code

    return asyncio.run(run())


def test_direct_success_never_touches_proxy(monkeypatch):
    direct = RecordingTransport()
    proxy = RecordingTransport()
    transport = upstream_transport.FallbackTransport(direct, "http://127.0.0.1:7897")
    transport._proxy_transport = proxy

    assert _send(transport) == 200
    assert (direct.calls, proxy.calls) == (1, 0)
    assert upstream_transport.fallback_count() == 0


def test_connect_timeout_falls_back_to_proxy(monkeypatch):
    """ConnectTimeout 必须被接住（它继承 TimeoutException，不是 ConnectError）。"""
    direct = RecordingTransport(error=httpx.ConnectTimeout("timed out"))
    proxy = RecordingTransport()
    transport = upstream_transport.FallbackTransport(direct, "http://127.0.0.1:7897")
    transport._proxy_transport = proxy

    assert _send(transport) == 200
    assert (direct.calls, proxy.calls) == (1, 1)
    assert upstream_transport.fallback_count() == 1


def test_connect_error_falls_back_to_proxy():
    direct = RecordingTransport(error=httpx.ConnectError("connection refused"))
    proxy = RecordingTransport()
    transport = upstream_transport.FallbackTransport(direct, "http://127.0.0.1:7897")
    transport._proxy_transport = proxy

    assert _send(transport) == 200
    assert (direct.calls, proxy.calls) == (1, 1)
    assert upstream_transport.fallback_count() == 1


def test_proxy_failure_propagates_as_is():
    """兜底也失败时抛的是兜底那次的原错，不能被包装。"""
    direct = RecordingTransport(error=httpx.ConnectError("direct down"))
    proxy = RecordingTransport(error=httpx.ConnectTimeout("proxy timed out"))
    transport = upstream_transport.FallbackTransport(direct, "http://127.0.0.1:7897")
    transport._proxy_transport = proxy

    with pytest.raises(httpx.ConnectTimeout):
        _send(transport)
    assert (direct.calls, proxy.calls) == (1, 1)


def test_read_timeout_after_response_is_not_retried():
    """响应已到之后的读超时不得重发（重复执行工具调用的风险）。"""
    direct = RecordingTransport(error=httpx.ReadTimeout("read timed out"))
    proxy = RecordingTransport()
    transport = upstream_transport.FallbackTransport(direct, "http://127.0.0.1:7897")
    transport._proxy_transport = proxy

    with pytest.raises(httpx.ReadTimeout):
        _send(transport)
    assert (direct.calls, proxy.calls) == (1, 0)
    assert upstream_transport.fallback_count() == 0


def test_aclose_closes_both_transports():
    direct = RecordingTransport()
    proxy = RecordingTransport()
    transport = upstream_transport.FallbackTransport(direct, "http://127.0.0.1:7897")
    transport._proxy_transport = proxy

    asyncio.run(transport.aclose())

    assert direct.closed is True
    assert proxy.closed is True


def test_aclose_without_proxy_use_only_closes_direct():
    direct = RecordingTransport()
    transport = upstream_transport.FallbackTransport(direct, "http://127.0.0.1:7897")

    asyncio.run(transport.aclose())

    assert direct.closed is True
    assert transport._proxy_transport is None


def test_fallback_transport_is_none_without_candidate(monkeypatch):
    """没有代理候选时不改行为：返回 None，调用方照旧直连。"""
    monkeypatch.delenv(upstream_transport.EXPLICIT_PROXY_VAR, raising=False)
    monkeypatch.delenv(upstream_transport.CLIENT_PROXY_VAR, raising=False)
    monkeypatch.setattr(upstream_transport, "_system_proxy_cache", [])

    assert upstream_transport.proxy_for_retry() is None
    assert upstream_transport.fallback_transport() is None


def test_proxy_candidates_prefer_explicit_then_client_then_system(monkeypatch):
    monkeypatch.setenv(upstream_transport.EXPLICIT_PROXY_VAR, "http://explicit:1")
    monkeypatch.setenv(upstream_transport.CLIENT_PROXY_VAR, "http://client:2")
    monkeypatch.setattr(upstream_transport, "_system_proxy_cache", ["http://system:3"])

    assert upstream_transport.proxy_for_retry() == "http://explicit:1"

    monkeypatch.delenv(upstream_transport.EXPLICIT_PROXY_VAR)
    assert upstream_transport.proxy_for_retry() == "http://client:2"

    monkeypatch.delenv(upstream_transport.CLIENT_PROXY_VAR)
    assert upstream_transport.proxy_for_retry() == "http://system:3"


def test_system_proxies_parsed_from_scutil(monkeypatch):
    output = (
        "<dictionary> {\n"
        "  HTTPEnable : 1\n"
        "  HTTPPort : 7897\n"
        "  HTTPProxy : 127.0.0.1\n"
        "  HTTPSEnable : 1\n"
        "  HTTPSPort : 7897\n"
        "  HTTPSProxy : 127.0.0.1\n"
        "  SOCKSEnable : 0\n"
        "}\n"
    )
    monkeypatch.setattr(
        upstream_transport.subprocess, "run",
        lambda *a, **k: type("R", (), {"stdout": output})(),
    )

    assert upstream_transport._system_proxies() == ["http://127.0.0.1:7897"]


def test_disabled_system_proxy_yields_nothing(monkeypatch):
    output = "  HTTPEnable : 0\n  HTTPProxy : 127.0.0.1\n  HTTPPort : 7897\n"
    monkeypatch.setattr(
        upstream_transport.subprocess, "run",
        lambda *a, **k: type("R", (), {"stdout": output})(),
    )

    assert upstream_transport._system_proxies() == []


def test_unavailable_scutil_does_not_raise(monkeypatch):
    def boom(*a, **k):
        raise OSError("scutil not found")

    monkeypatch.setattr(upstream_transport.subprocess, "run", boom)

    assert upstream_transport._system_proxies() == []


def test_fallback_log_is_throttled(monkeypatch):
    """故障窗口里每个请求都会兜底，日志必须节流，否则刷屏。"""
    printed: list[str] = []
    monkeypatch.setattr(upstream_transport.sys, "stderr", type("S", (), {"write": printed.append})())
    monkeypatch.setattr(upstream_transport, "_last_report_at", 0.0)

    for _ in range(5):
        upstream_transport.report_fallback("http://127.0.0.1:7897", httpx.ConnectTimeout("x"))

    assert upstream_transport.fallback_count() == 5
    assert sum("直连建连失败" in chunk for chunk in printed) == 1


# ============================================================
# 国际站默认走代理（transport_for）
# ============================================================

INTL = {"id": 1, "domain": "www.workbuddy.ai"}
DOMESTIC = {"id": 3, "domain": "www.workbuddy.cn"}


@pytest.fixture
def _proxy_available(monkeypatch):
    monkeypatch.setenv(upstream_transport.EXPLICIT_PROXY_VAR, "http://127.0.0.1:7897")
    monkeypatch.delenv(upstream_transport.INTL_PROXY_DISABLED_VAR, raising=False)


def test_intl_account_always_goes_through_proxy(_proxy_available):
    """国际站不得先直连：直连那一次就是被上游拒绝的那一次。"""
    transport = upstream_transport.transport_for(INTL)

    assert isinstance(transport, upstream_transport.AlwaysProxyTransport)
    assert transport._proxy == "http://127.0.0.1:7897"


def test_domestic_account_never_uses_proxy(_proxy_available):
    """国内站只看直连，连建连失败也不换代理。

    回归（2026-11-09）：以前国内号走 `FallbackTransport`，建连失败会静默从系统代理
    重发。本机那个代理的出口是**美国 Oracle 机房 IP**（实测 129.146.198.122 / US），
    而国内账号平时是杭州电信住宅 IP（125.122.2.115）。一个「定位在国内、请求来自
    境外 VPS」的切跳正是上游判 `11140 unusual activity` 的典型画像，而日志里国内
    账号确实出现过 `upstream_disconnect` —— 这条路径会被走到。

    代价：断网期间国内请求直接失败（客户端可重试）。远小于把账号搭进去。
    """
    assert upstream_transport.transport_for(DOMESTIC) is None


def test_intl_proxy_can_be_disabled(_proxy_available, monkeypatch):
    monkeypatch.setenv(upstream_transport.INTL_PROXY_DISABLED_VAR, "off")

    assert isinstance(
        upstream_transport.transport_for(INTL), upstream_transport.FallbackTransport
    )


def test_intl_proxy_enabled_default_and_parsing(monkeypatch):
    monkeypatch.delenv(upstream_transport.INTL_PROXY_DISABLED_VAR, raising=False)
    assert upstream_transport.intl_proxy_enabled() is True

    for value in ("off", "none", "false", "0", ""):
        monkeypatch.setenv(upstream_transport.INTL_PROXY_DISABLED_VAR, value)
        assert upstream_transport.intl_proxy_enabled() is False

    monkeypatch.setenv(upstream_transport.INTL_PROXY_DISABLED_VAR, "on")
    assert upstream_transport.intl_proxy_enabled() is True


def test_transport_for_is_none_without_candidate(monkeypatch):
    """没有代理候选时不能悄悄改行为：国际站也照旧直连。"""
    monkeypatch.delenv(upstream_transport.EXPLICIT_PROXY_VAR, raising=False)
    monkeypatch.delenv(upstream_transport.CLIENT_PROXY_VAR, raising=False)
    monkeypatch.setattr(upstream_transport, "_system_proxy_cache", [])

    assert upstream_transport.transport_for(INTL) is None


def test_always_proxy_transport_never_touches_direct():
    """AlwaysProxyTransport 只有代理那一条路，没有直连分支。"""
    proxy = RecordingTransport()
    transport = upstream_transport.AlwaysProxyTransport("http://127.0.0.1:7897")
    transport._transport = proxy

    assert _send(transport) == 200
    assert proxy.calls == 1
