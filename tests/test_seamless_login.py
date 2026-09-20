"""无感登录（OAuth state 轮询）单元测试。

背景：桌面端新版 auth 文件是 `$wbEncrypted` 信封，网关读不出明文 token；
官方插件 OAuth 接口仍直接下发明文 token，机制复刻 WorkDaddy daemon.js。
这里把 `seamless_login._http_json` 整体替换成脚本化假响应，绝不打网络。
"""

import time

import pytest

import buddy2api.database as db
import buddy2api.seamless_login as sl


@pytest.fixture(autouse=True)
def _clean_flows():
    sl.reset()
    yield
    sl.reset()


def _fake_http(responses: list[dict]):
    """按调用顺序返回预设响应，并记录 URL 便于断言。"""
    calls = []

    def _call(url, method="GET", body=None, headers=None, timeout=30):
        calls.append({"url": url, "method": method, "body": body, "headers": headers or {}})
        if not responses:
            raise AssertionError(f"unexpected extra request: {url}")
        return responses.pop(0)

    _call.calls = calls
    return _call


def _token_response(uid="uid-x", nickname="tester", access="at-plain", refresh="rt-plain"):
    return {
        "code": 0,
        "data": {
            "accessToken": access,
            "refreshToken": refresh,
            "expiresAt": 1_800_000_000_000,
            "refreshExpiresAt": 1_800_500_000_000,
            "domain": "www.workbuddy.cn",
            "sessionState": "ss-1",
        },
    }, {"code": 0, "data": {"uid": uid, "nickname": nickname, "phoneNumber": "17816074985"}}


def test_start_returns_auth_url_and_stores_flow(monkeypatch):
    fake = _fake_http([{"code": 0, "data": {"state": "st-123", "authUrl": "https://www.workbuddy.cn/login?state=st-123"}}])
    monkeypatch.setattr(sl, "_http_json", fake)

    out = sl.start()

    assert out["login_id"].startswith("sl_")
    assert out["auth_url"].endswith("state=st-123")
    assert out["expires_in"] == sl.FLOW_TIMEOUT_SECONDS
    url = fake.calls[0]["url"]
    assert "/v2/plugin/auth/state?platform=workbuddy" in url and fake.calls[0]["method"] == "POST"


def test_start_falls_back_to_derived_auth_url(monkeypatch):
    monkeypatch.setattr(sl, "_http_json", _fake_http([{"code": 0, "data": {"state": "st-9"}}]))
    out = sl.start()
    assert "state=st-9" in out["auth_url"] and out["auth_url"].startswith("https://www.workbuddy.cn/")


def test_start_without_state_raises(monkeypatch):
    monkeypatch.setattr(sl, "_http_json", _fake_http([{"code": 0, "data": {}}]))
    with pytest.raises(sl.SeamlessLoginError):
        sl.start()


def test_poll_pending_until_user_authorizes(monkeypatch):
    token_ok, account_ok = _token_response()
    fake = _fake_http([
        {"code": 0, "data": {"state": "st-1"}},
        {"code": 1001, "msg": "waiting"},          # 未授权
        token_ok,                                   # 已授权
        account_ok,
    ])
    monkeypatch.setattr(sl, "_http_json", fake)
    login_id = sl.start()["login_id"]

    assert sl.poll(login_id)["status"] == "pending"
    done = sl.poll(login_id)
    assert done["status"] == "done"
    assert done["uid"] == "uid-x"
    assert done["created"] is True, "库里没有该 uid 时应新建账号"


def test_poll_updates_existing_account_by_uid(monkeypatch):
    aid = db.add_account({"name": "old-name", "uid": "uid-x", "access_token": "stale-token"})
    token_ok, account_ok = _token_response(uid="uid-x", nickname="new-nick")
    fake = _fake_http([{"code": 0, "data": {"state": "st-2"}}, token_ok, account_ok])
    monkeypatch.setattr(sl, "_http_json", fake)

    done = sl.poll(sl.start()["login_id"])

    assert done["status"] == "done" and done["created"] is False
    assert done["account_id"] == aid
    fresh = db.get_account(aid)
    assert fresh["access_token"] == "at-plain", "明文字段解密后应与 OAuth 下发的一致"
    assert fresh["refresh_token"] == "rt-plain"
    assert fresh["session_state"] == "ss-1"
    assert len(db.list_accounts(provider="workbuddy")) == 1, "uid 命中已有账号时不得重复建号"


def test_poll_authorization_header_and_account_endpoint(monkeypatch):
    token_ok, account_ok = _token_response()
    fake = _fake_http([{"code": 0, "data": {"state": "st-3"}}, token_ok, account_ok])
    monkeypatch.setattr(sl, "_http_json", fake)
    sl.poll(sl.start()["login_id"])

    account_call = fake.calls[-1]
    assert "/login/account?state=st-3" in account_call["url"]
    assert account_call["headers"]["Authorization"] == "Bearer at-plain"
    assert account_call["headers"]["X-Domain"] == "www.workbuddy.cn"


def test_poll_without_uid_reports_error(monkeypatch):
    token_ok, _ = _token_response()
    fake = _fake_http([
        {"code": 0, "data": {"state": "st-4"}},
        token_ok,
        {"code": 0, "data": {"nickname": "no-uid"}},
    ])
    monkeypatch.setattr(sl, "_http_json", fake)

    out = sl.poll(sl.start()["login_id"])
    assert out["status"] == "error" and "uid" in out["error"]


def test_poll_unknown_login_id():
    assert sl.poll("sl_nope")["status"] == "unknown"


def test_poll_expired_flow(monkeypatch):
    monkeypatch.setattr(sl, "_http_json", _fake_http([{"code": 0, "data": {"state": "st-5"}}]))
    login_id = sl.start()["login_id"]
    sl._flows[login_id]["expires_at"] = time.time() - 1

    assert sl.poll(login_id)["status"] == "unknown", "过期流程按已回收处理"


def test_poll_second_call_returns_cached_result(monkeypatch):
    token_ok, account_ok = _token_response()
    fake = _fake_http([{"code": 0, "data": {"state": "st-6"}}, token_ok, account_ok])
    monkeypatch.setattr(sl, "_http_json", fake)
    login_id = sl.start()["login_id"]

    first = sl.poll(login_id)
    second = sl.poll(login_id)  # 结果已缓存，不应再打上游（fake 里没有多余响应）

    assert first["status"] == second["status"] == "done"
    assert second["account_id"] == first["account_id"]


def test_network_failure_surfaces_as_seamless_error(monkeypatch):
    def _boom(*a, **kw):
        raise sl.SeamlessLoginError("网络错误: timed out")

    monkeypatch.setattr(sl, "_http_json", _boom)
    with pytest.raises(sl.SeamlessLoginError):
        sl.start()


def test_concurrent_poll_does_not_double_create_account(monkeypatch):
    """前端每秒轮询：并发进入时不得各建一个账号（标志位串行化）。"""
    token_ok, account_ok = _token_response(uid="uid-race")
    fake = _fake_http([{"code": 0, "data": {"state": "st-7"}}, token_ok, account_ok])
    monkeypatch.setattr(sl, "_http_json", fake)
    login_id = sl.start()["login_id"]

    # 模拟第一个请求已进入上游阶段（polling 标志已置位）时第二个请求到达
    sl._flows[login_id]["polling"] = True
    assert sl.poll(login_id) == {"status": "pending"}
    sl._flows[login_id]["polling"] = False

    assert sl.poll(login_id)["status"] == "done"
    rows = [a for a in db.list_accounts(provider="workbuddy") if a.get("uid") == "uid-race"]
    assert len(rows) == 1
