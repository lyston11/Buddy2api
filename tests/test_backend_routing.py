"""账号后端路由 + 凭据自救。

回归背景（2026-09-16）：
  网关原先对所有账号只用全局 backend_url（copilot.tencent.com）。
  www.workbuddy.ai（国际版）账号的 token 由各自 realm 签发，
  copilot.tencent.com 的 APISIX 对它一律 401 ⇒ mark_account_failure 把账号永久置 expired，
  表现是「已登录、额度正常，但在网关里永远不被选用」。
  实测：同一 token 打 https://www.workbuddy.ai/v2/chat/completions 返回 200。
"""

import json

import pytest

import auth_manager
import credential_crypto
import database as db

UPSTREAM = "https://upstream.test"


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    path = tmp_path / "gateway.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setenv("CB_GATEWAY_MASTER_KEY", "pytest-master-key")
    credential_crypto.reset_cache()
    db.init_db()
    yield path
    credential_crypto.reset_cache()


@pytest.fixture()
def stub_backend(monkeypatch):
    monkeypatch.setattr(auth_manager, "backend_url", lambda: UPSTREAM)


@pytest.mark.parametrize(
    "domain",
    [
        "copilot.tencent.com",
        "staging-copilot.tencent.com",
        "www.codebuddy.cn",
        "staging.codebuddy.cn",
        "www.workbuddy.cn",
        "staging.workbuddy.cn",
        "",
    ],
)
def test_internal_domains_use_global_backend(isolated_db, stub_backend, domain):
    assert auth_manager.account_backend_url({"domain": domain}) == UPSTREAM


@pytest.mark.parametrize(
    "domain", ["www.workbuddy.ai", "www.codebuddy.ai", "staging.workbuddy.ai"]
)
def test_external_domains_use_own_host(isolated_db, stub_backend, domain):
    assert auth_manager.account_backend_url({"domain": domain}) == f"https://{domain}"


def test_missing_account_falls_back_to_global(isolated_db, stub_backend):
    assert auth_manager.account_backend_url(None) == UPSTREAM
    assert auth_manager.account_backend_url({}) == UPSTREAM


def test_domain_routing_can_be_disabled(isolated_db, stub_backend, monkeypatch):
    monkeypatch.setenv("CB_GATEWAY_DOMAIN_BACKEND", "0")
    assert auth_manager.account_backend_url({"domain": "www.workbuddy.ai"}) == UPSTREAM


def _write_auth_file(path, access_token):
    path.write_text(
        json.dumps(
            {
                "account": {"uid": "u-ai", "nickname": "ai-user", "type": "personal"},
                "auth": {
                    "accessToken": access_token,
                    "refreshToken": "rt-1",
                    "expiresAt": 4102444800000,
                    "refreshExpiresAt": 4102444800000,
                    "domain": "www.workbuddy.ai",
                },
            }
        ),
        encoding="utf-8",
    )


def _add_ai_account(auth_file, access_token="token-old"):
    _write_auth_file(auth_file, access_token)
    return db.add_account(
        {
            "name": "ai-user",
            "uid": "u-ai",
            "provider": "workbuddy",
            "access_token": access_token,
            "domain": "www.workbuddy.ai",
            "extra": {"auth_path": str(auth_file)},
        }
    )


def test_mark_expired_adopts_rotated_token(isolated_db, tmp_path):
    """客户端已轮换 token 时，不该把账号永久打死。"""
    auth_file = tmp_path / "wb-ai.info"
    aid = _add_ai_account(auth_file)
    _write_auth_file(auth_file, "token-new")

    auth_manager.mark_account_expired(aid)

    account = db.get_account(aid)
    assert account["status"] == "active"
    assert account["access_token"] == "token-new"


def test_mark_expired_sticks_when_auth_file_unchanged(isolated_db, tmp_path):
    """auth 文件没变 ⇒ 凭据确实不可用，才置 expired。"""
    auth_file = tmp_path / "wb-ai.info"
    aid = _add_ai_account(auth_file)

    auth_manager.mark_account_expired(aid)

    assert db.get_account(aid)["status"] == "expired"


def test_mark_expired_without_auth_path_still_expires(isolated_db):
    aid = db.add_account(
        {
            "name": "manual",
            "uid": "u-manual",
            "provider": "workbuddy",
            "access_token": "token-manual",
            "domain": "www.workbuddy.ai",
        }
    )

    auth_manager.mark_account_expired(aid)

    assert db.get_account(aid)["status"] == "expired"


def test_mark_account_failure_401_uses_self_heal(isolated_db, tmp_path):
    auth_file = tmp_path / "wb-ai.info"
    aid = _add_ai_account(auth_file)
    _write_auth_file(auth_file, "token-new")

    auth_manager.mark_account_failure(aid, 401)

    assert db.get_account(aid)["status"] == "active"
