"""线上报文指纹的回归测试：TLS 与请求头都要和官方客户端同形。

依据（2026-10-27 抓包 + 读官方客户端实现，见 docs/design/fingerprint.md）：

- 发 `/v2/chat/completions` 的是内置 CLI：axios + `https.Agent({keepAlive:true})`，
  即 Node/OpenSSL、HTTP/1.1、**没有 ALPN 扩展**；
- 头名全小写（axios 的 `AxiosHeaders` 与 fetch 的 `Headers` 都把小写化），
  `Host` / `Connection` / `Content-Length` 由 Node 自己补，且不带 `accept-encoding`。

这里用一个本地自签 TLS 服务端把网关**真实**发出的字节抓下来，逐项比对 —— 这样
「指纹改没改对」不再靠读代码判断。
"""

import asyncio

import buddy2api.fingerprint as fingerprint
import buddy2api.upstream_transport as upstream_transport
import buddy2api.wire_probe as wire_probe

ACCOUNT = {
    "domain": "www.workbuddy.ai",
    "access_token": "probe-token",
    "uid": "00000000-0000-7000-8000-000000000000",
    "enterprise_id": "",
}


def _capture(headers=None):
    fingerprint.begin_request(None)
    chat = headers or fingerprint.chat_headers(ACCOUNT)
    return asyncio.run(wire_probe.capture_once(headers=chat, body={"model": "probe"}))


def test_tls_hello_matches_official_node_runtime():
    """JA3 与官方客户端运行时（Node v22 / OpenSSL 3.5.5）完全一致。"""
    captured = _capture()
    assert captured["ja3"] is not None, "没抓到 ClientHello"
    assert captured["ja3"]["ja3_md5"] == upstream_transport.NODE_JA3_MD5
    # Node 的 https.Agent 不带 ALPNProtocols，握手包里就不该有 ALPN 扩展
    assert captured["ja3"]["alpn"] == ""


def test_wire_headers_look_like_the_real_client():
    captured = _capture()
    names = [name for name, _ in captured["request_head"]["headers"]]
    lowered = [name.lower() for name in names]

    # 官方客户端不发 accept-encoding（Python 的 httpx 默认会带）
    assert "accept-encoding" not in lowered
    # 我们显式给的头一律小写，只有 Node 自己补的三个自动头是首字母大写
    auto_headers = {
        name for name in names if name in {"Host", "Connection", "Content-Length"}
    }
    for name in names:
        if name in auto_headers:
            continue
        assert name == name.lower(), f"头名必须全小写: {name}"

    for required in (
        "accept",
        "content-type",
        "x-conversation-id",
        "x-conversation-request-id",
        "x-conversation-message-id",
        "x-request-id",
        "x-agent-intent",
        "x-agent-type",
        "x-ide-type",
        "x-private-data",
        "x-product",
        "authorization",
        "user-agent",
    ):
        assert required in lowered, required


def test_wire_headers_have_no_python_or_sdk_artifacts():
    captured = _capture()
    lowered = {name.lower(): value for name, value in captured["request_head"]["headers"]}
    # httpx / python-requests / axios / node-fetch 的自报 UA 都不能出现
    user_agent = lowered.get("user-agent", "")
    assert "python" not in user_agent.lower()
    assert "axios" not in user_agent.lower()
    assert "node-fetch" not in user_agent.lower()
    assert not any(name.startswith("x-stainless") for name in lowered)
