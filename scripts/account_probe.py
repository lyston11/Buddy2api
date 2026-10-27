#!/usr/bin/env python
"""账号健康巡检：一次跑完所有账号，直接告诉你哪个号被上游拒了、为什么。

为什么需要它：账号被封是**服务端行为**，网关这边只能在被拒之后才知道。今天排查
「两个号被封」的整个过程（逐个账号发一条最小请求 + 看业务码）本来是手工做的，
固化下来之后 5 秒就能自查，不必等到请求失败才发现。

    python scripts/account_probe.py                 # 全部账号
    python scripts/account_probe.py --id 9 --id 13  # 只看这两个
    python scripts/account_probe.py --json          # 给脚本/监控用
    python scripts/account_probe.py --mark          # 顺手把不可用的号打上长冷却

判定依据是上游业务码而不是 HTTP status（14017/14018 都是 429、11140 是 403，
只看 status 会把「号废了」和「限流了」混成一件，见 auth_manager 的注释）：

    200           正常
    14017         试用未激活，需要重新登录激活
    14018         积分耗尽
    11140/11141/11142  账号级风控/授权封禁（官方客户端也归为「需重新登录」）
    401           凭据失效（refresh_token 可能已被服务端销毁）
    其它 4xx/5xx  记录下来人工判断
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import buddy2api.auth_manager as auth_manager  # noqa: E402
import buddy2api.database as db  # noqa: E402
import buddy2api.fingerprint as fingerprint  # noqa: E402
import buddy2api.upstream_transport as upstream_transport  # noqa: E402

PROBE_MODEL = "deepseek-v4.1-flash"

REASON_TEXT = {
    0: "正常",
    14017: "试用未激活（需重新登录激活）",
    14018: "积分耗尽",
    11140: "账号级风控封禁（request illegal）",
    11141: "账号级授权封禁",
    11142: "账号级授权封禁",
    11102: "该账号站点没有这个模型",
}


def _extract_code(raw: bytes) -> int:
    """从上游错误体里取业务码（error.data.code 优先）。"""
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return 0
    return _code_from_node(payload)


def _code_from_node(node) -> int:
    if not isinstance(node, dict):
        return 0
    error = node.get("error")
    if isinstance(error, dict):
        data = error.get("data")
        if isinstance(data, dict) and isinstance(data.get("code"), int):
            return data["code"]
        if isinstance(error.get("code"), int):
            return error["code"]
    if isinstance(node.get("code"), int):
        return node["code"]
    return 0


async def probe_account(account: dict) -> dict:
    """对一个账号发一条最小的真实流式请求，返回探测结果。"""
    aid = int(account["id"])
    domain = str(account.get("domain") or "").strip()
    token = str(account.get("access_token") or "").strip()
    result = {
        "id": aid,
        "name": account.get("name") or "",
        "domain": domain,
        "status": account.get("status") or "",
        "http": 0,
        "code": 0,
        "detail": "",
    }
    if not token:
        result.update(http=0, code=-1, detail="库里没有可解密的 access_token")
        return result

    body = {
        "model": PROBE_MODEL,
        "stream": True,
        "max_tokens": 4,
        "messages": [
            {"role": "system", "content": "pong"},
            {"role": "user", "content": "pong"},
        ],
    }
    fingerprint.begin_request(None)
    try:
        async with upstream_transport.client_scope(account, 30.0) as client:
            async with client.stream(
                "POST",
                f"https://{domain}/v2/chat/completions",
                headers=fingerprint.chat_headers(account),
                json=body,
            ) as response:
                head = b""
                async for chunk in response.aiter_bytes():
                    head += chunk
                    if len(head) > 2000:
                        break
                result["http"] = response.status_code
                if response.status_code != 200:
                    result["code"] = _extract_code(head)
                    result["detail"] = head[:200].decode("utf-8", "replace").replace("\n", " ")
    except Exception as exc:  # 网络错误也要如实记下来
        result.update(http=0, code=-2, detail=f"{type(exc).__name__}: {exc}"[:160])
    return result


def _verdict(result: dict) -> str:
    if result["http"] == 200:
        return "OK"
    if result["http"] == 401:
        # 网关层 401（APISIX/openresty 直接拒），不是业务码
        return "凭据被拒（401，可能 refresh_token 已被服务端销毁）"
    if result["code"] in REASON_TEXT:
        return REASON_TEXT[result["code"]]
    if result["code"] == -1:
        return "无凭据"
    if result["code"] == -2:
        return "网络不可达"
    return f"上游 HTTP {result['http']}"


async def run(args) -> int:
    db.init_db()
    accounts = db.list_accounts(provider="workbuddy")
    if args.id:
        wanted = {int(value) for value in args.id}
        accounts = [a for a in accounts if int(a["id"]) in wanted]
    if not accounts:
        print("没有匹配的账号")
        return 1

    results = []
    for account in accounts:
        result = await probe_account(account)
        results.append(result)
        verdict = _verdict(result)
        flag = "OK " if result["http"] == 200 else "!! "
        print(
            f"{flag}#{result['id']:<3}{result['name'][:26]:<28}{result['domain']:<20}"
            f"{result['status']:<9}{verdict}"
        )
        if args.mark and result["http"] != 200 and result["code"] in auth_manager.ACCOUNT_UNAVAILABLE_CODES:
            auth_manager.mark_account_unavailable(result["id"], result["code"])

    broken = [r for r in results if r["http"] != 200]
    if args.json:
        print(json.dumps({"results": results, "broken": len(broken)}, ensure_ascii=False, indent=1))
    print(f"\n{len(results) - len(broken)}/{len(results)} 正常")
    if broken:
        print("被上游拒绝的账号:")
        for result in broken:
            print(f"  #{result['id']} {result['name']} → {_verdict(result)}")
    return 0 if not broken else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="账号健康巡检（逐个真发一条最小请求）")
    parser.add_argument("--id", action="append", type=int, help="只查这些账号 id（可重复）")
    parser.add_argument("--json", action="store_true", help="额外输出 JSON")
    parser.add_argument(
        "--mark", action="store_true", help="把不可用的账号直接打上长冷却（不写数据库状态）"
    )
    return asyncio.run(run(parser.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
