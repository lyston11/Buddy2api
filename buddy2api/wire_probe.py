"""wire_probe.py — 出站指纹探针：抓自己发出去的 ClientHello 和 HTTP 报文。

为什么需要它：指纹伪装最容易变成「照着文档猜」，猜完也没法验证改没改对。这里用
一个本地 TLS 服务器把网关**真实**发出的字节抓下来，做两件可验证的事：

1. **算 JA3**，和官方客户端运行时（内置 CLI 用的 Node v22.22.2 / OpenSSL 3.5.5）
   的基准值比 —— 只配 `upstream_transport.ssl_context()` 就能做到完全一致，
   见 `NODE_JA3_MD5`；
2. **打印原始请求行 + 头**，用来核对头名大小写、头集合与插入顺序（官方客户端是
   axios/fetch，头名全小写、没有 `accept-encoding`）。

自签证书直接用 `cryptography` 现场生成，不需要联网、不碰真上游。

用法（CLI 见 scripts/fingerprint_probe.py）：

    python -m buddy2api.wire_probe            # 人类可读
    python -m buddy2api.wire_probe --json     # 给测试/脚本用
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import socket
import ssl
import sys
import tempfile
from typing import Optional

import buddy2api.upstream_transport as upstream_transport


# ---------------------------------------------------------------- ClientHello

def _parse_client_hello(data: bytes) -> dict:
    """把 ClientHello 拆成 JA3 需要的字段（ciphers / extensions / curves / ecpf）。"""
    if len(data) < 6:
        raise ValueError("ClientHello 太短")
    record_version = int.from_bytes(data[1:3], "big")
    pos = 5  # 跳过 TLS record 头
    if data[pos] != 1:
        raise ValueError("不是 ClientHello 记录")
    pos += 4  # handshake type + length
    pos += 2  # legacy_version
    pos += 32  # random
    session_id_len = data[pos]
    pos += 1 + session_id_len
    ciphers_len = int.from_bytes(data[pos:pos + 2], "big")
    pos += 2
    ciphers = []
    end = pos + ciphers_len
    while pos < end:
        ciphers.append(int.from_bytes(data[pos:pos + 2], "big"))
        pos += 2
    compression_len = data[pos]
    pos += 1 + compression_len

    extensions: list[int] = []
    ext_data: dict[int, bytes] = {}
    if pos + 2 <= len(data):
        ext_total = int.from_bytes(data[pos:pos + 2], "big")
        pos += 2
        end = pos + ext_total
        while pos + 4 <= end:
            ext_type = int.from_bytes(data[pos:pos + 2], "big")
            ext_len = int.from_bytes(data[pos + 2:pos + 4], "big")
            ext_data[ext_type] = data[pos + 4:pos + 4 + ext_len]
            extensions.append(ext_type)
            pos += 4 + ext_len

    curves: list[int] = []
    if 10 in ext_data:
        blob = ext_data[10]
        count = int.from_bytes(blob[:2], "big")
        for index in range(count // 2):
            start = 2 + index * 2
            curves.append(int.from_bytes(blob[start:start + 2], "big"))
    ec_point_formats = list(ext_data.get(11, b""))

    ja3 = ",".join(
        [
            str(record_version),
            "-".join(str(c) for c in ciphers),
            "-".join(str(e) for e in extensions),
            "-".join(str(c) for c in curves),
            "-".join(str(f) for f in ec_point_formats),
        ]
    )
    return {
        "record_version": record_version,
        "ciphers": ciphers,
        "extensions": extensions,
        "curves": curves,
        "ec_point_formats": ec_point_formats,
        "alpn": ext_data.get(16, b"").decode("latin-1"),
        "ja3": ja3,
        "ja3_md5": hashlib.md5(ja3.encode("ascii")).hexdigest(),
    }


def ja3(client_hello: bytes) -> dict:
    """对外入口：ClientHello 字节 → JA3 与明细。"""
    return _parse_client_hello(client_hello)


# ---------------------------------------------------------------- 自签证书

def _self_signed_cert(directory: str) -> tuple[str, str]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = f"{directory}/probe-cert.pem"
    key_path = f"{directory}/probe-key.pem"
    with open(cert_path, "wb") as fh:
        fh.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as fh:
        fh.write(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            )
        )
    return cert_path, key_path


# ---------------------------------------------------------------- 抓报文

class _Capture:
    """一次抓取的产物：ClientHello 字节 + 拿到的那一条请求的原始报文。"""

    def __init__(self) -> None:
        self.client_hello = b""
        self.raw_request = b""


async def capture_once(
    headers: Optional[dict] = None,
    body: Optional[dict] = None,
    *,
    path: str = "/v2/chat/completions",
    account: Optional[dict] = None,
) -> dict:
    """用网关自己的 TLS 上下文/客户端发一次请求，抓 ClientHello + 请求头。"""
    capture = _Capture()
    with tempfile.TemporaryDirectory() as tmp:
        cert_path, key_path = _self_signed_cert(tmp)
        server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ctx.load_cert_chain(cert_path, key_path)

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]

        def serve() -> None:
            """本地 TLS 服务端：先 peek ClientHello，再正常握手、读请求、回 200。"""
            raw, _ = listener.accept()
            raw.settimeout(5)
            capture.client_hello = raw.recv(8192, socket.MSG_PEEK)
            tls = server_ctx.wrap_socket(raw, server_side=True)
            try:
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = tls.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                length = 0
                for line in data.split(b"\r\n"):
                    if line.lower().startswith(b"content-length:"):
                        length = int(line.split(b":", 1)[1].strip())
                while length > 0:
                    chunk = tls.recv(min(length, 4096))
                    if not chunk:
                        break
                    data += chunk
                    length -= len(chunk)
                capture.raw_request = data
                tls.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    b"Content-Length: 2\r\nConnection: close\r\n\r\n{}"
                )
            finally:
                try:
                    tls.close()
                except OSError:
                    pass
                listener.close()

        loop = asyncio.get_running_loop()
        server = loop.run_in_executor(None, serve)
        client = upstream_transport.build_client(
            5.0, verify=upstream_transport.build_ssl_context(verify=False)
        )
        try:
            await client.post(
                f"https://127.0.0.1:{port}{path}",
                headers=headers or {},
                json=body or {"model": "probe", "stream": False},
            )
        except Exception:  # 抓包失败也要把已抓到的内容返回
            pass
        finally:
            await client.aclose()
            await server

    return {
        "client_hello": capture.client_hello,
        "raw_request": capture.raw_request,
        "ja3": ja3(capture.client_hello) if capture.client_hello else None,
        "request_head": parse_request_head(capture.raw_request),
    }


def parse_request_head(raw: bytes) -> dict:
    """原始报文 → {请求行, [(头名, 值), ...]}（保持线上大小写与顺序）。"""
    if not raw:
        return {"request_line": "", "headers": []}
    text = raw.decode("latin-1")
    lines = text.split("\r\n")
    headers = []
    for line in lines[1:]:
        if not line:
            break
        if ":" not in line:
            continue
        name, value = line.split(":", 1)
        headers.append((name, value.strip()))
    return {"request_line": lines[0], "headers": headers}


def compare(baseline_ja3_md5: str, captured: dict) -> list[str]:
    """把抓到的指纹与官方客户端基准做对比，返回问题列表（空 = 通过）。"""
    problems = []
    if not captured.get("ja3"):
        return ["没抓到 ClientHello"]
    if captured["ja3"]["ja3_md5"] != baseline_ja3_md5:
        problems.append(
            f"JA3 不一致: 抓到的 {captured['ja3']['ja3_md5']} != 官方客户端 {baseline_ja3_md5}"
        )
    if captured["ja3"]["alpn"]:
        problems.append(f"握手包里带了 ALPN 扩展: {captured['ja3']['alpn']!r}（Node 不发）")
    names = [name for name, _ in captured["request_head"]["headers"]]
    if any(name.lower() == "accept-encoding" for name in names):
        problems.append("请求头里带了 accept-encoding（官方客户端不发）")
    return problems


async def main_async(as_json: bool = False) -> int:
    import buddy2api.fingerprint as fingerprint

    account = {
        "domain": "www.workbuddy.ai",
        "access_token": "probe-token",
        "uid": "00000000-0000-7000-8000-000000000000",
        "enterprise_id": "",
    }
    fingerprint.begin_request(None)
    headers = fingerprint.chat_headers(account)
    captured = await capture_once(headers=headers, body={"model": "probe"}, account=account)
    problems = compare(upstream_transport.NODE_JA3_MD5, captured)

    if as_json:
        print(
            json.dumps(
                {
                    "ja3": captured["ja3"],
                    "request_line": captured["request_head"]["request_line"],
                    "headers": captured["request_head"]["headers"],
                    "problems": problems,
                    "baseline_ja3_md5": upstream_transport.NODE_JA3_MD5,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if not problems else 1

    print(f"JA3 md5: {captured['ja3']['ja3_md5']} (官方客户端 {upstream_transport.NODE_JA3_MD5})")
    print(f"ALPN   : {captured['ja3']['alpn']!r}")
    print(f"{captured['request_head']['request_line']}")
    for name, value in captured["request_head"]["headers"]:
        print(f"  {name}: {value}")
    if problems:
        print("\n问题:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("\n与官方客户端一致。")
    return 0


def main() -> int:
    return asyncio.run(main_async("--json" in sys.argv[1:]))


if __name__ == "__main__":
    raise SystemExit(main())
