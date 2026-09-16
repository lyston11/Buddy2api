"""API Key 绑定账号（default_account）。

背景（2026-09-16）：
  一个网关里有 4 个账号，调度是「优先级 + 粘性」，客户端无法指定用哪一个。
  给 api_keys 加 default_account（0 = 不绑定）后，一把 key 固定走一个账号，
  DSH 里就可以用不同的 key / 模型条目分别选号。
"""

import asyncio

import pytest

import auth_manager
import credential_crypto
import database as db


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
def clean_routing():
    auth_manager.set_pinned_account(0)
    auth_manager._sticky_account_id.clear()
    with auth_manager._failure_lock:
        auth_manager._account_failures.clear()
    yield
    auth_manager.set_pinned_account(0)
    auth_manager._sticky_account_id.clear()


def _add_account(name, uid, *, priority=0, status="active", provider="workbuddy"):
    aid = db.add_account(
        {
            "name": name,
            "uid": uid,
            "provider": provider,
            "access_token": f"at-{uid}",
            "refresh_token": f"rt-{uid}",
            "domain": "www.codebuddy.cn",
            "priority": priority,
        }
    )
    if status != "active":
        db.update_account(aid, {"status": status})
    return aid


def test_add_api_key_defaults_to_unpinned(isolated_db):
    key = "sk-cb-unpinned"
    kid = db.add_api_key(key, "auto")
    row = db.get_api_key_by_key(key)

    assert row["id"] == kid
    assert int(row["default_account"]) == 0


def test_add_and_update_api_key_store_default_account(isolated_db, clean_routing):
    _add_account("a", "u-a")
    _add_account("b", "u-b")
    key = "sk-cb-pinned"
    db.add_api_key(key, "pinned", default_account=2)

    assert int(db.get_api_key_by_key(key)["default_account"]) == 2

    db.update_api_key(db.get_api_key_by_key(key)["id"], {"default_account": 1})
    assert int(db.get_api_key_by_key(key)["default_account"]) == 1

    db.update_api_key(db.get_api_key_by_key(key)["id"], {"default_account": None})
    assert int(db.get_api_key_by_key(key)["default_account"]) == 0


def test_pin_beats_priority(isolated_db, clean_routing):
    low = _add_account("low", "u-low", priority=0)
    _add_account("high", "u-high", priority=10)

    auth_manager.set_pinned_account(low)

    assert auth_manager.pinned_account_id() == low
    assert auth_manager.pick_account()["id"] == low


def test_pin_never_falls_back_to_another_account(isolated_db, clean_routing):
    """绑定的账号不可用时宁可失败，也不偷偷换号。"""
    low = _add_account("low", "u-low", priority=0)
    _add_account("high", "u-high", priority=10)
    auth_manager.set_pinned_account(low)
    db.update_account(low, {"status": "expired"})

    assert auth_manager.pick_account() is None


def test_pin_respects_exclude_ids(isolated_db, clean_routing):
    low = _add_account("low", "u-low")
    auth_manager.set_pinned_account(low)

    assert auth_manager.pick_account(exclude_ids={low}) is None


def test_pin_ignores_account_of_other_provider(isolated_db, clean_routing):
    other = _add_account("other", "u-other", provider="qwenwork")
    auth_manager.set_pinned_account(other)

    assert auth_manager.pick_account(provider="workbuddy") is None
    assert auth_manager.pick_account(provider="qwenwork")["id"] == other


def test_unpinned_still_uses_priority(isolated_db, clean_routing):
    _add_account("low", "u-low", priority=0)
    high = _add_account("high", "u-high", priority=10)

    auth_manager.set_pinned_account(0)
    assert auth_manager.pick_account()["id"] == high

    auth_manager.set_pinned_account(None)
    assert auth_manager.pick_account()["id"] == high


def test_fallback_only_refreshes_pinned_account(isolated_db, clean_routing, monkeypatch):
    pinned = _add_account("pinned", "u-pinned", status="expired")
    other = _add_account("other", "u-other", status="expired")

    refreshed: list[int] = []

    async def fake_refresh(account):
        refreshed.append(account["id"])
        db.update_account(account["id"], {"status": "active"})
        return True

    monkeypatch.setattr(auth_manager, "refresh_token", fake_refresh)

    async def run():
        auth_manager.set_pinned_account(pinned)
        return await auth_manager.pick_account_with_fallback()

    account = asyncio.run(run())

    assert refreshed == [pinned]
    assert account["id"] == pinned
    assert other not in refreshed
