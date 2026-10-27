"""上游渠道校验（400 code 11128）的处理契约。

背景（2026-10-24 实测）：上游在 `/v2/chat/completions` 上做「渠道校验」，判的是
**我们这一侧**的请求形态，不是账号：

  - 首条消息不是 system  → `{"code":11128,"msg":"first message is not system prompt"}`
  - 缺三个会话头之一     → `{"code":11128,"msg":"request illegal"}`

displayTips 原话是「这次调用没通过渠道校验，可能不是官方客户端发出的」。

它跟「账号坏了」「模型不归这个站点」都不是一回事：所有账号共用同一套指纹，换号
照样被拒；能换的只有身份（桌面端 app ↔ 命令行 cli）。所以网关的契约是：

  1. 换一套 profile，在**同一个账号**上重试一次；
  2. 不给账号记失败（不进冷却、不算鉴权失败、不改状态）；
  3. 两次都被拒就把失败如实报给客户端，日志类别记成 channel_verification。
"""

import asyncio

import pytest

import buddy2api.auth_manager as auth_manager
import buddy2api.database as db
import buddy2api.fingerprint as fingerprint
import buddy2api.proxy as proxy


def _make_account(uid: str, domain: str = "www.workbuddy.ai") -> int:
    return db.add_account(
        {
            "name": uid,
            "uid": uid,
            "provider": "workbuddy",
            "access_token": f"token-{uid}",
            "refresh_token": f"refresh-{uid}",
            "domain": domain,
            "status": "active",
            "expires_at": 4_000_000_000_000,
        }
    )


@pytest.fixture(autouse=True)
def _reset_fingerprint_context():
    fingerprint.begin_request(None)
    yield
    fingerprint.begin_request(None)


@pytest.mark.parametrize(
    "detail",
    [
        {"code": 11128, "msg": "first message is not system prompt"},
        {"code": 11128, "msg": "request illegal"},
        {"msg": "This call failed channel verification"},
        {"error": {"code": 11128, "message": "request illegal"}},
    ],
)
def test_channel_verification_error_recognizes_upstream_rejections(detail):
    assert proxy._channel_verification_error(400, detail) is True


@pytest.mark.parametrize(
    "status,detail",
    [
        (401, {"code": 11128, "msg": "request illegal"}),
        (403, {"code": 11128, "msg": "request illegal"}),
        (500, {"code": 11128}),
        (400, {"code": 11102, "msg": "model [x] service info not found"}),
        (400, {"code": 11155, "msg": "reasoning_content is required"}),
        (400, b"not a dict"),
        (400, None),
    ],
)
def test_channel_verification_error_ignores_other_failures(status, detail):
    assert proxy._channel_verification_error(status, detail) is False


def test_channel_retry_budget_is_one_profile_switch():
    assert proxy._plan_channel_retry(0) is True
    assert proxy._plan_channel_retry(1) is False


def test_channel_rejection_retries_same_account_with_other_profile(monkeypatch):
    """11128 换的是身份不是账号：同一个账号用另一套指纹重试，随后成功。"""
    account = _make_account("solo")

    seen: list[str] = []

    async def fake_headers(acct):
        headers = fingerprint.chat_headers(acct)
        return {"authorization": "Bearer x", "x-ide-type": headers["x-ide-type"]}

    async def fake_collect(url, headers, body, acct, api_key_info, model_name, t0, attempts=1):
        seen.append(headers["x-ide-type"])
        if len(seen) == 1:
            return ("error", (400, {"code": 11128, "msg": "request illegal"}))
        return ("json", {"id": "ok", "choices": [], "usage": {"total_tokens": 0}})

    async def no_delay(_attempt):
        return None

    monkeypatch.setattr(auth_manager, "get_valid_headers", fake_headers)
    monkeypatch.setattr(proxy, "_collect_stream", fake_collect)
    monkeypatch.setattr(proxy, "_retry_delay", no_delay)

    result = asyncio.run(proxy._json_chat_with_stall_retry({"model": "m"}, None, "m"))

    assert result[0] == "json"
    # 第一次是默认身份（桌面端），重试换成 CLI —— 账号始终是同一个
    assert seen == ["WorkBuddy", "CLI"]
    # 渠道问题不该让账号背锅
    assert auth_manager.account_is_cooling_down(account) is False
    assert auth_manager.auth_failure_count(account) == 0
    assert db.get_account(account)["status"] == "active"


def test_channel_rejection_twice_reports_without_blaming_account(monkeypatch):
    """两套身份都被渠道校验拒：如实报错，账号状态不受影响，日志记 channel_verification。"""
    account = _make_account("solo")

    seen: list[str] = []

    async def fake_headers(acct):
        headers = fingerprint.chat_headers(acct)
        return {"authorization": "Bearer x", "x-ide-type": headers["x-ide-type"]}

    async def fake_collect(url, headers, body, acct, api_key_info, model_name, t0, attempts=1):
        seen.append(headers["x-ide-type"])
        return ("error", (400, {"code": 11128, "msg": "request illegal"}))

    async def no_delay(_attempt):
        return None

    monkeypatch.setattr(auth_manager, "get_valid_headers", fake_headers)
    monkeypatch.setattr(proxy, "_collect_stream", fake_collect)
    monkeypatch.setattr(proxy, "_retry_delay", no_delay)

    result = asyncio.run(proxy._json_chat_with_stall_retry({"model": "m"}, None, "m"))

    assert result[0] == "error"
    assert result[1][0] == 400
    assert seen == ["WorkBuddy", "CLI"]
    assert auth_manager.account_is_cooling_down(account) is False
    assert auth_manager.auth_failure_count(account) == 0
    assert db.get_account(account)["status"] == "active"

    conn = db.get_conn()
    row = conn.execute(
        "SELECT finish_reason FROM logs WHERE account_id = ? ORDER BY id DESC LIMIT 1",
        (account,),
    ).fetchone()
    conn.close()
    assert row[0] == proxy.FAILURE_CHANNEL


def test_channel_retry_locks_the_same_account(monkeypatch):
    """换身份重试不能顺带换账号：11128 是渠道问题，换号解决不了。"""
    first = _make_account("first")
    second = _make_account("second")

    queue = [db.get_account(first), db.get_account(second), db.get_account(first)]

    async def fake_pick(tried_ids, model=None):
        while queue:
            candidate = queue.pop(0)
            if candidate["id"] not in tried_ids:
                return candidate
        return None

    async def fake_headers(acct):
        headers = fingerprint.chat_headers(acct)
        return {"authorization": "Bearer x", "x-ide-type": headers["x-ide-type"]}

    seen: list[int] = []

    async def fake_collect(url, headers, body, acct, api_key_info, model_name, t0, attempts=1):
        seen.append(acct["id"])
        if len(seen) == 1:
            return ("error", (400, {"code": 11128, "msg": "request illegal"}))
        return ("json", {"id": "ok", "choices": [], "usage": {"total_tokens": 0}})

    async def no_delay(_attempt):
        return None

    monkeypatch.setattr(auth_manager, "pick_account_with_fallback", fake_pick)
    monkeypatch.setattr(auth_manager, "get_valid_headers", fake_headers)
    monkeypatch.setattr(proxy, "_collect_stream", fake_collect)
    monkeypatch.setattr(proxy, "_retry_delay", no_delay)

    result = asyncio.run(proxy._json_chat_with_stall_retry({"model": "m"}, None, "m"))

    assert result[0] == "json"
    assert seen == [first, first]
