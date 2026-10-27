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
    parser.add_argument("--domain", default=ACCOUNT["domain"], help="账号域名（决定版本号）")
    args = parser.parse_args(argv)

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


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
