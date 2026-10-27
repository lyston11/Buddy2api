#!/usr/bin/env python
"""出海前的指纹自检：抓一次真实出站报文，和官方客户端基准比。

    python scripts/fingerprint_probe.py              # 桌面端 profile（默认）
    python scripts/fingerprint_probe.py --channel cli
    python scripts/fingerprint_probe.py --json       # 给脚本/CI 用，一致时退出码 0

检查内容（都不联网、不碰真上游，本地自签 TLS 服务端抓自己发出的字节）：

  1. TLS ClientHello 的 JA3 是否与官方客户端运行时（内置 CLI 的 Node v22 /
     OpenSSL 3.5.5）一致，握手包里是否混进了 ALPN 扩展；
  2. 请求头是不是官方客户端那种形态：头名全小写、没有 `accept-encoding`、
     没有 python/httpx/axios 自报的 UA、三个会话头齐全。

基准值来自 2026-10-27 的实测，写在 `buddy2api/upstream_transport.py`
（`NODE_DEFAULT_CIPHERS` / `NODE_JA3_MD5`），推导与残留差异见
`docs/design/fingerprint.md`。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

# 作为脚本直接跑时 sys.path[0] 是 scripts/，项目根要自己补上（与 pytest.ini 的
# pythonpath=. 同一个理由）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import buddy2api.fingerprint as fingerprint
import buddy2api.upstream_transport as upstream_transport
import buddy2api.wire_probe as wire_probe

ACCOUNT = {
    "domain": "www.workbuddy.ai",
    "access_token": "probe-token",
    "uid": "00000000-0000-7000-8000-000000000000",
    "enterprise_id": "",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="出站指纹自检（TLS + 请求头）")
    parser.add_argument(
        "--channel",
        choices=(fingerprint.CHANNEL_APP, fingerprint.CHANNEL_CLI),
        default=fingerprint.CHANNEL_APP,
        help="要自检的身份 profile（默认 app）",
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument(
        "--qimei",
        action="store_true",
        help="只查两个站点的 qimei36（设备标识）解析结果，不发请求",
    )
    parser.add_argument("--domain", default=ACCOUNT["domain"], help="账号域名（决定版本号）")
    args = parser.parse_args(argv)

    if args.qimei:
        return probe_qimei()

    account = dict(ACCOUNT, domain=args.domain)
    fingerprint.begin_request(None)
    headers = fingerprint.chat_headers(account, channel=args.channel)
    captured = asyncio.run(wire_probe.capture_once(headers=headers, body={"model": "probe"}))
    problems = wire_probe.compare(upstream_transport.NODE_JA3_MD5, captured)

    if args.json:
        import json

        print(
            json.dumps(
                {
                    "channel": args.channel,
                    "baseline_ja3_md5": upstream_transport.NODE_JA3_MD5,
                    "ja3": captured["ja3"],
                    "request_line": captured["request_head"]["request_line"],
                    "headers": captured["request_head"]["headers"],
                    "problems": problems,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if not problems else 1

    ja3 = captured["ja3"] or {}
    print(f"身份 profile : {args.channel}")
    print(f"JA3 md5      : {ja3.get('ja3_md5')}")
    print(f"官方客户端   : {upstream_transport.NODE_JA3_MD5}")
    print(f"ALPN         : {ja3.get('alpn')!r}（Node 不发）")
    print()
    print(captured["request_head"]["request_line"])
    for name, value in captured["request_head"]["headers"]:
        print(f"  {name}: {value}")
    print()
    if problems:
        for problem in problems:
            print(f"[不一致] {problem}")
        return 1
    print("[ok] 与官方客户端一致")
    return 0


def probe_qimei() -> int:
    """两个站点各自的 qimei36 解析结果（缓存文件 / 落盘缓存 / 官方 SDK 现算）。"""
    ok = True
    for group, domain in (
        ("international", "www.workbuddy.ai"),
        ("domestic", "www.workbuddy.cn"),
    ):
        cached_file = fingerprint._QIMEI_CACHE_FILES.get(group, "")
        used_file = os.path.exists(os.path.expanduser(cached_file)) if cached_file else False
        value = fingerprint.qimei36(domain)
        if value is None:
            # 现算走后台线程，这里同步跑一次给 CLI 用
            value = fingerprint.detect_qimei_via_sdk(group)
            source = "sdk"
        else:
            source = "client-cache" if used_file else "gateway-cache"
        print(f"{group:14s} {domain:20s} {value or '(不可用)'}  [{source}]")
        ok = ok and bool(value)
    if not ok:
        print("\n拿不到 qimei36 时网关不会发 x-device-id-list（与官方客户端行为一致）")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
