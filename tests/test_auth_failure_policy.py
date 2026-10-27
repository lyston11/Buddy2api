"""401/403 判定策略：单次失败不再直接置 expired，而是累积到阈值 + 真实复核。"""

import asyncio

import pytest

import buddy2api.auth_manager as auth_manager
from conftest import patch_upstream_client
import buddy2api.credential_crypto as credential_crypto
import buddy2api.database as db


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    path = tmp_path / "gateway.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setenv("CB_GATEWAY_MASTER_KEY", "pytest-master-key")
    credential_crypto.reset_cache()
    db.init_db()
    yield path
    credential_crypto.reset_cache()


def _make_account(uid: str = "u1", status: str = "active",
                  domain: str = "www.workbuddy.cn") -> int:
    aid = db.add_account(
        {
            "name": uid,
            "uid": uid,
            "provider": "workbuddy",
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "domain": domain,
            "status": status,
            "expires_at": 4_000_000_000_000,
        }
    )
    return aid


def _clear_state():
    auth_manager._account_failures.clear()
    auth_manager._auth_failures.clear()
    auth_manager._verify_inflight.clear()


def test_single_401_keeps_account_active(isolated_db):
    _clear_state()
    aid = _make_account()
    auth_manager.mark_account_failure(aid, 401)
    assert db.get_account(aid)["status"] == "active"
    assert auth_manager.auth_failure_count(aid) == 1
    assert auth_manager.account_is_cooling_down(aid) is True


def test_below_threshold_keeps_account_active(isolated_db):
    _clear_state()
    aid = _make_account()
    for _ in range(auth_manager.auth_failure_threshold() - 1):
        auth_manager.mark_account_failure(aid, 403)
    assert db.get_account(aid)["status"] == "active"


def test_threshold_expires_only_when_probe_confirms_invalid(isolated_db, monkeypatch):
    _clear_state()
    aid = _make_account()
    for _ in range(auth_manager.auth_failure_threshold()):
        auth_manager.mark_account_failure(aid, 401)

    async def fake_probe(account):
        return "invalid", "HTTP 401 from https://www.workbuddy.cn"

    monkeypatch.setattr(auth_manager, "probe_account_credentials", fake_probe)
    result = asyncio.run(auth_manager.verify_account_credentials(aid))
    assert result["action"] == "expired"
    assert db.get_account(aid)["status"] == "expired"


def test_threshold_keeps_active_when_probe_passes(isolated_db, monkeypatch):
    _clear_state()
    aid = _make_account()
    for _ in range(auth_manager.auth_failure_threshold()):
        auth_manager.mark_account_failure(aid, 401)

    async def fake_probe(account):
        return "ok", "HTTP 200"

    monkeypatch.setattr(auth_manager, "probe_account_credentials", fake_probe)
    result = asyncio.run(auth_manager.verify_account_credentials(aid))
    assert result["action"] == "kept_active"
    assert db.get_account(aid)["status"] == "active"
    assert auth_manager.auth_failure_count(aid) == 0


def test_inconclusive_probe_keeps_account_active(isolated_db, monkeypatch):
    _clear_state()
    aid = _make_account()
    for _ in range(auth_manager.auth_failure_threshold()):
        auth_manager.mark_account_failure(aid, 401)

    async def fake_probe(account):
        return "unknown", "HTTP 504"

    monkeypatch.setattr(auth_manager, "probe_account_credentials", fake_probe)
    result = asyncio.run(auth_manager.verify_account_credentials(aid))
    assert result["action"] == "inconclusive"
    assert db.get_account(aid)["status"] == "active"


def test_verification_skips_manually_disabled_account(isolated_db, monkeypatch):
    _clear_state()
    aid = _make_account(status="inactive")
    for _ in range(auth_manager.auth_failure_threshold()):
        auth_manager.mark_account_failure(aid, 401)

    async def fake_probe(account):
        return "invalid", "HTTP 401"

    monkeypatch.setattr(auth_manager, "probe_account_credentials", fake_probe)
    result = asyncio.run(auth_manager.verify_account_credentials(aid))
    assert result["action"] == "skipped"
    assert db.get_account(aid)["status"] == "inactive"


def test_mark_success_resets_auth_failure_counter(isolated_db):
    _clear_state()
    aid = _make_account()
    auth_manager.mark_account_failure(aid, 401)
    auth_manager.mark_account_failure(aid, 401)
    auth_manager.mark_account_success(aid)
    assert auth_manager.auth_failure_count(aid) == 0
    assert auth_manager.account_is_cooling_down(aid) is False


def test_threshold_schedules_verification_once(isolated_db, monkeypatch):
    _clear_state()
    aid = _make_account()
    calls: list[int] = []

    async def fake_verify(account_id):
        calls.append(account_id)
        return {"account_id": account_id, "action": "kept_active"}

    monkeypatch.setattr(auth_manager, "verify_account_credentials", fake_verify)

    async def run():
        for _ in range(auth_manager.auth_failure_threshold() + 2):
            auth_manager.mark_account_failure(aid, 401)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(run())
    assert calls == [aid]


class _FakeResponse:
    def __init__(self, status_code: int, payload=None, raises: bool = False):
        self.status_code = status_code
        self._payload = payload
        self._raises = raises

    def json(self):
        if self._raises:
            raise ValueError("not json")
        return self._payload


class _FakeClient:
    def __init__(self, response):
        self._response = response
        self.requests: list[tuple[str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        self.requests.append((url, headers or {}))
        return self._response


def _patch_client(monkeypatch, response) -> _FakeClient:
    client = _FakeClient(response)

    patch_upstream_client(monkeypatch, client)
    return client


def test_probe_uses_account_own_domain(isolated_db, monkeypatch):
    _clear_state()
    aid = _make_account()
    client = _patch_client(monkeypatch, _FakeResponse(200, {"code": 0, "data": {}}))
    result = asyncio.run(auth_manager.probe_account_credentials(db.get_account(aid)))
    assert result[0] == "ok"
    assert client.requests[0][0].startswith("https://www.workbuddy.cn/")


def test_backend_url_for_accepts_any_cn_domain(isolated_db):
    """客户端写进 auth 文件的国内域名不止一个，按 .cn 后缀整体判定。"""
    for domain in ("www.workbuddy.cn", "www.codebuddy.cn", "workbuddy.cn", "foo.bar.cn"):
        assert auth_manager.backend_url_for({"domain": domain}) == f"https://{domain}"
    # 大小写 / 带协议头 / 带尾斜杠也要能识别
    assert auth_manager.backend_url_for({"domain": "WWW.CodeBuddy.CN/"}) == "https://www.codebuddy.cn"
    assert auth_manager.backend_url_for({"domain": "https://www.workbuddy.cn"}) == "https://www.workbuddy.cn"


def test_backend_url_for_falls_back_for_non_cn(isolated_db):
    """国际版账号仍然回退全局 backend_url（自定义 relay 因此继续生效）。"""
    db.set_setting("backend_url", "https://www.workbuddy.ai")
    for account in (
        {"domain": "www.workbuddy.ai"},
        {"domain": "www.codebuddy.ai"},
        {},
        {"domain": ""},
        # 不能误判：这些都不是 .cn 后缀
        {"domain": "foocn.com"},
        {"domain": "www.workbuddy.cn.evil.com"},
    ):
        assert auth_manager.backend_url_for(account) == "https://www.workbuddy.ai", account


def test_backend_url_for_sends_intl_accounts_to_their_own_site_on_default(isolated_db):
    """backend_url 还是官方默认值时，国际版账号必须走自己的站点。

    copilot.tencent.com 只认内部 realm 签发的 token，国际版 token 打过去会被
    APISIX 直接 401（HTML，不是业务 JSON）。默认值下把国际版账号发到那里，等于
    整条国际线路不可用 —— 实测过。
    """
    db.set_setting("backend_url", "https://copilot.tencent.com")
    # 与国内站同一原则：发到账号自己的域名（大小写/尾斜杠会被归一）
    for domain, expected in (
        ("www.workbuddy.ai", "https://www.workbuddy.ai"),
        ("foo.workbuddy.ai", "https://foo.workbuddy.ai"),
        ("WWW.WorkBuddy.AI/", "https://www.workbuddy.ai"),
    ):
        assert auth_manager.backend_url_for({"domain": domain}) == expected
    # 官方内部入口的其它形态同样算默认值
    db.set_setting("backend_url", "https://staging-copilot.tencent.com")
    assert auth_manager.backend_url_for({"domain": "www.workbuddy.ai"}) == "https://www.workbuddy.ai"


def test_backend_url_for_keeps_custom_relay_for_intl_accounts(isolated_db):
    """自定义 relay 必须继续生效，不能被「国际版走自己的站点」绕过。"""
    db.set_setting("backend_url", "https://my-relay.example.com")
    for domain in ("www.workbuddy.ai", "www.codebuddy.ai", ""):
        assert auth_manager.backend_url_for({"domain": domain}) == "https://my-relay.example.com"
    # 自定义 relay 也不影响国内站账号：它们一律走自己的站点
    assert auth_manager.backend_url_for({"domain": "www.workbuddy.cn"}) == "https://www.workbuddy.cn"
    # 没有实测证据的 .ai 域名族（codebuddy.ai）不猜，仍走 relay
    assert auth_manager.backend_url_for({"domain": "foo.workbuddy.ai"}) == "https://my-relay.example.com"


def test_probe_routes_codebuddy_cn_account(isolated_db, monkeypatch):
    """domain=www.codebuddy.cn 的账号必须打到 codebuddy.cn，不能再回退到 .ai。"""
    _clear_state()
    aid = _make_account(domain="www.codebuddy.cn")
    client = _patch_client(monkeypatch, _FakeResponse(200, {"code": 0, "data": {}}))
    result = asyncio.run(auth_manager.probe_account_credentials(db.get_account(aid)))
    assert result[0] == "ok"
    assert client.requests[0][0].startswith("https://www.codebuddy.cn/")


def test_probe_reports_invalid_on_401(isolated_db, monkeypatch):
    _clear_state()
    aid = _make_account()
    _patch_client(monkeypatch, _FakeResponse(401, {}, raises=True))
    result = asyncio.run(auth_manager.probe_account_credentials(db.get_account(aid)))
    assert result[0] == "invalid"


def test_probe_reports_unknown_on_5xx(isolated_db, monkeypatch):
    _clear_state()
    aid = _make_account()
    _patch_client(monkeypatch, _FakeResponse(504, {}, raises=True))
    result = asyncio.run(auth_manager.probe_account_credentials(db.get_account(aid)))
    assert result[0] == "unknown"
    assert "504" in result[1]


def test_refresh_rejection_needs_threshold(isolated_db, monkeypatch):
    _clear_state()
    aid = _make_account()
    _patch_client(monkeypatch, _FakeResponse(200, {"code": 1001, "msg": "refresh rejected"}))
    account = db.get_account(aid)
    assert asyncio.run(auth_manager.refresh_token(account)) is False
    assert db.get_account(aid)["status"] == "active"
    assert asyncio.run(auth_manager.refresh_token(account)) is False
    assert db.get_account(aid)["status"] == "active"
    assert asyncio.run(auth_manager.refresh_token(account)) is False
    assert db.get_account(aid)["status"] == "expired"


def test_refresh_success_resets_counter(isolated_db, monkeypatch):
    _clear_state()
    aid = _make_account()
    auth_manager.mark_account_failure(aid, 401)
    _patch_client(
        monkeypatch,
        _FakeResponse(
            200,
            {
                "code": 0,
                "data": {
                    "accessToken": "new-access",
                    "refreshToken": "new-refresh",
                    "expiresAt": 4_100_000_000_000,
                    "refreshExpiresAt": 4_200_000_000_000,
                    "domain": "www.workbuddy.cn",
                },
            },
        ),
    )
    assert asyncio.run(auth_manager.refresh_token(db.get_account(aid))) is True
    assert auth_manager.auth_failure_count(aid) == 0
    assert db.get_account(aid)["access_token"] == "new-access"


def test_threshold_from_settings(isolated_db):
    _clear_state()
    db.set_setting("auth_failure_threshold", 5)
    assert auth_manager.auth_failure_threshold() == 5
    db.set_setting("auth_failure_threshold", 0)
    assert auth_manager.auth_failure_threshold() == 1
    db.set_setting("auth_failure_threshold", 999)
    assert auth_manager.auth_failure_threshold() == 20
    db.set_setting("auth_failure_threshold", "bad")
    assert auth_manager.auth_failure_threshold() == auth_manager.AUTH_FAILURE_THRESHOLD
