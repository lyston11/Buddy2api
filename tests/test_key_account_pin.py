"""API Key 绑定账号（api_keys.default_account）。

背景：一个网关里混装多个账号时，调度是「优先级 + 站点偏好 + 粘性」，客户端无法
指定用哪一个。给 api_keys 加 default_account（0 = 不绑定）后，一把 key 固定走一个
账号，调用方就能按 key 分开用不同账号的额度。

这里钉住三条语义：
  1. 绑定优先于所有调度规则（优先级、站点偏好、粘性都不参与）。
  2. 绑定的账号不可用时**宁可失败也不换号** —— 否则调用方以为没动额度，实际在吃
     别的账号。
  3. 不绑定时行为完全不变。
"""

import asyncio
import json

import pytest

import buddy2api.auth_manager as auth_manager
import buddy2api.credential_crypto as credential_crypto
import buddy2api.database as db


@pytest.fixture()
def clean_routing():
    auth_manager.set_pinned_account(0)
    auth_manager._sticky_account_id.clear()
    with auth_manager._failure_lock:
        auth_manager._account_failures.clear()
    yield
    auth_manager.set_pinned_account(0)
    auth_manager._sticky_account_id.clear()


def _add_account(name, uid, *, priority=0, status="active", provider="workbuddy",
                 domain="www.workbuddy.ai"):
    aid = db.add_account(
        {
            "name": name,
            "uid": uid,
            "provider": provider,
            "access_token": f"at-{uid}",
            "refresh_token": f"rt-{uid}",
            "domain": domain,
            "priority": priority,
        }
    )
    if status != "active":
        db.update_account(aid, {"status": status})
    return aid


# ============================================================
# 存储层
# ============================================================

def test_add_api_key_defaults_to_unpinned():
    key = "sk-cb-unpinned"
    kid = db.add_api_key(key, "auto")
    row = db.get_api_key_by_key(key)

    assert row["id"] == kid
    assert int(row["default_account"]) == 0


def test_add_and_update_api_key_store_default_account(clean_routing):
    _add_account("a", "u-a")
    _add_account("b", "u-b")
    key = "sk-cb-pinned"
    db.add_api_key(key, "pinned", default_account=2)

    assert int(db.get_api_key_by_key(key)["default_account"]) == 2

    db.update_api_key(db.get_api_key_by_key(key)["id"], {"default_account": 1})
    assert int(db.get_api_key_by_key(key)["default_account"]) == 1

    # 解绑：None / 0 都归一到 0
    db.update_api_key(db.get_api_key_by_key(key)["id"], {"default_account": None})
    assert int(db.get_api_key_by_key(key)["default_account"]) == 0


def test_update_api_key_clamps_negative_default_account(clean_routing):
    """兜底归一：任何调用方都写不进负数（负值会让绑定语义变成「未绑定」以外的含义）。"""
    _add_account("a", "u-a")
    key = "sk-cb-neg"
    db.add_api_key(key, "neg")
    kid = db.get_api_key_by_key(key)["id"]

    db.update_api_key(kid, {"default_account": -5})
    assert int(db.get_api_key_by_key(key)["default_account"]) == 0


# ============================================================
# 选号：绑定优先，且不静默换号
# ============================================================

def test_pin_beats_priority(clean_routing):
    low = _add_account("low", "u-low", priority=0)
    _add_account("high", "u-high", priority=10)

    auth_manager.set_pinned_account(low)

    assert auth_manager.pinned_account_id() == low
    assert auth_manager.pick_account()["id"] == low


def test_pin_never_falls_back_to_another_account(clean_routing):
    """绑定的账号不可用时宁可失败，也不偷偷换号。"""
    low = _add_account("low", "u-low", priority=0)
    _add_account("high", "u-high", priority=10)
    auth_manager.set_pinned_account(low)
    db.update_account(low, {"status": "expired"})

    assert auth_manager.pick_account() is None


def test_pin_respects_exclude_ids(clean_routing):
    low = _add_account("low", "u-low")
    auth_manager.set_pinned_account(low)

    assert auth_manager.pick_account(exclude_ids={low}) is None


def test_pin_ignores_account_of_other_provider(clean_routing):
    other = _add_account("other", "u-other", provider="qwenwork")
    auth_manager.set_pinned_account(other)

    assert auth_manager.pick_account(provider="workbuddy") is None
    assert auth_manager.pick_account(provider="qwenwork")["id"] == other


def test_pin_beats_site_preference(clean_routing):
    """站点偏好只影响未绑定时的优先级；绑定必须赢过它。

    回归点：站点偏好过滤若排在绑定之前，绑了国内账号的 key 在偏好国际站时会选不到号。
    """
    intl = _add_account("intl", "u-intl", domain="www.workbuddy.ai")
    cn = _add_account("cn", "u-cn", domain="www.workbuddy.cn")
    db.set_setting(
        "model_site_preference",
        '{"default": "international", "models": {}}',
    )

    auth_manager.set_pinned_account(cn)
    assert auth_manager.pick_account(model="deepseek-v4.1-flash")["id"] == cn

    auth_manager.set_pinned_account(intl)
    assert auth_manager.pick_account(model="deepseek-v4.1-flash")["id"] == intl


def test_pin_ignores_sticky_account(clean_routing):
    """粘性只服务于未绑定场景；绑定不能被粘性槽顶掉。"""
    a = _add_account("a", "u-a")
    b = _add_account("b", "u-b")

    auth_manager._set_sticky_account(b, "workbuddy", "deepseek-v4.1-flash")
    auth_manager.set_pinned_account(a)

    assert auth_manager.pick_account(model="deepseek-v4.1-flash")["id"] == a


def test_unpinned_still_uses_priority(clean_routing):
    _add_account("low", "u-low", priority=0)
    high = _add_account("high", "u-high", priority=10)

    auth_manager.set_pinned_account(0)
    assert auth_manager.pick_account()["id"] == high

    # None / 非法值同样视为未绑定
    auth_manager.set_pinned_account(None)
    assert auth_manager.pick_account()["id"] == high
    auth_manager.set_pinned_account("garbage")
    assert auth_manager.pinned_account_id() == 0
    assert auth_manager.pick_account()["id"] == high


def test_fallback_only_refreshes_pinned_account(clean_routing, monkeypatch):
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


def test_fallback_gives_up_when_pinned_refresh_fails(clean_routing, monkeypatch):
    """绑定的账号刷新失败就返回 None，绝不去刷别的账号。"""
    pinned = _add_account("pinned", "u-pinned", status="expired")
    _add_account("other", "u-other", status="expired")

    async def fake_refresh(account):
        return False

    monkeypatch.setattr(auth_manager, "refresh_token", fake_refresh)

    async def run():
        auth_manager.set_pinned_account(pinned)
        return await auth_manager.pick_account_with_fallback()

    assert asyncio.run(run()) is None


# ============================================================
# 请求入口：contextvar 注入 + 管理 API 校验
# ============================================================

def test_apply_key_account_pin_sets_contextvar(clean_routing):
    """请求入口把 Key 的 default_account 写进 contextvar，选号才能看到。"""
    import buddy2api.server as server

    server._apply_key_account_pin({"default_account": 7})
    assert auth_manager.pinned_account_id() == 7

    server._apply_key_account_pin(None)
    assert auth_manager.pinned_account_id() == 0

    server._apply_key_account_pin({})
    assert auth_manager.pinned_account_id() == 0


def test_validate_key_account_rejects_unknown_and_wrong_channel(clean_routing):
    import buddy2api.server as server
    from fastapi import HTTPException

    aid = _add_account("cn", "u-cn", provider="workbuddy")

    assert server._validate_key_account(0, "workbuddy") == 0
    assert server._validate_key_account(None, "workbuddy") == 0
    assert server._validate_key_account(aid, "workbuddy") == aid

    with pytest.raises(HTTPException) as error:
        server._validate_key_account(9999, "workbuddy")
    assert error.value.status_code == 400

    with pytest.raises(HTTPException) as error:
        server._validate_key_account(aid, "qwenwork")
    assert error.value.status_code == 400

    with pytest.raises(HTTPException) as error:
        server._validate_key_account("abc", "workbuddy")
    assert error.value.status_code == 400


def test_admin_create_key_validates_and_returns_default_account(clean_routing, monkeypatch):
    import asyncio

    import buddy2api.server as server

    aid = _add_account("cn", "u-cn")
    monkeypatch.setattr(server, "_check_admin", lambda *_a, **_k: None)

    class FakeRequest:
        def __init__(self, payload):
            self._payload = payload

        async def stream(self):
            yield json.dumps(self._payload).encode("utf-8")

    result = asyncio.run(
        server.admin_create_key(
            FakeRequest({"name": "pinned", "default_channel": "workbuddy",
                         "default_account": aid}),
            "Bearer admin",
        )
    )
    assert result["default_account"] == aid
    row = db.get_api_key_by_key(result["key"])
    assert int(row["default_account"]) == aid


def test_admin_update_key_validates_against_effective_channel(clean_routing, monkeypatch):
    """只改 default_account 时，校验要用该 Key 现有的通道，不能当成 workbuddy 放行。"""
    import asyncio

    import buddy2api.server as server
    from fastapi import HTTPException

    qwen = _add_account("qw", "u-qw", provider="qwenwork")
    monkeypatch.setattr(server, "_check_admin", lambda *_a, **_k: None)

    kid = db.add_api_key("sk-cb-ch", "ch", default_channel="qwenwork")
    key_id = db.get_api_key_by_key("sk-cb-ch")["id"]
    assert key_id == kid

    class FakeRequest:
        def __init__(self, payload):
            self._payload = payload

        async def stream(self):
            yield json.dumps(self._payload).encode("utf-8")

    asyncio.run(server.admin_update_key(key_id, FakeRequest({"default_account": qwen}), "Bearer admin"))
    assert int(db.get_api_key_by_key("sk-cb-ch")["default_account"]) == qwen

    # 绑一个 workbuddy 账号到 qwenwork 通道的 Key 上 → 400
    wb = _add_account("wb", "u-wb", provider="workbuddy")
    with pytest.raises(HTTPException) as error:
        asyncio.run(server.admin_update_key(key_id, FakeRequest({"default_account": wb}), "Bearer admin"))
    assert error.value.status_code == 400


# ============================================================
# API Key 站点限定（api_keys.default_site）
# ============================================================

def test_add_api_key_defaults_to_any_site():
    db.add_api_key("sk-cb-site-default", "any")
    assert db.get_api_key_by_key("sk-cb-site-default")["default_site"] == ""


def test_site_pin_restricts_candidates_without_disabling_them(clean_routing):
    """限定站点是硬过滤：只剩该站点的账号，另一边完全不参与。"""
    intl = _add_account("intl", "u-intl", domain="www.workbuddy.ai", priority=0)
    _add_account("cn", "u-cn", domain="www.workbuddy.cn", priority=10)

    auth_manager.set_pinned_site("international")
    assert auth_manager.pinned_site() == "international"
    assert auth_manager.pick_account()["id"] == intl

    # 国内站优先级别更高，但限定国际站时不能被它抢走
    auth_manager.set_pinned_site("domestic")
    picked = auth_manager.pick_account()
    assert picked["id"] != intl
    assert picked["domain"] == "www.workbuddy.cn"


def test_site_pin_fails_instead_of_crossing_sites(clean_routing):
    """该站点没有可用账号时报无可用账号，绝不静默跨站点。"""
    _add_account("intl", "u-intl", domain="www.workbuddy.ai")

    auth_manager.set_pinned_site("domestic")
    assert auth_manager.pick_account() is None


def test_site_pin_overrides_site_preference(clean_routing):
    """站点偏好只说「优先哪边」，Key 限定说的是「只用哪边」，后者赢。"""
    intl = _add_account("intl", "u-intl", domain="www.workbuddy.ai")
    cn = _add_account("cn", "u-cn", domain="www.workbuddy.cn")
    db.set_setting("model_site_preference", '{"default": "international", "models": {}}')

    auth_manager.set_pinned_site("domestic")
    assert auth_manager.pick_account(model="glm-5.3")["id"] == cn

    auth_manager.set_pinned_site("")
    assert auth_manager.pick_account(model="glm-5.3")["id"] == intl


def test_site_pin_applies_to_pinned_account(clean_routing):
    """绑定账号与站点限定冲突时返回 None（配置矛盾不能靠静默跨站解决）。"""
    cn = _add_account("cn", "u-cn", domain="www.workbuddy.cn")
    auth_manager.set_pinned_account(cn)

    auth_manager.set_pinned_site("international")
    assert auth_manager.pick_account() is None

    auth_manager.set_pinned_site("domestic")
    assert auth_manager.pick_account()["id"] == cn


def test_site_pin_filters_fallback_refresh(clean_routing, monkeypatch):
    """过期账号刷新回退也必须遵守站点限定，否则刷新值会绕过候选过滤。"""
    intl = _add_account("intl", "u-intl", domain="www.workbuddy.ai", status="expired")
    cn = _add_account("cn", "u-cn", domain="www.workbuddy.cn", status="expired")

    refreshed: list[int] = []

    async def fake_refresh(account):
        refreshed.append(account["id"])
        db.update_account(account["id"], {"status": "active"})
        return True

    monkeypatch.setattr(auth_manager, "refresh_token", fake_refresh)

    async def run():
        auth_manager.set_pinned_site("domestic")
        return await auth_manager.pick_account_with_fallback()

    account = asyncio.run(run())

    assert refreshed == [cn]
    assert account["id"] == cn
    assert intl not in refreshed


def test_apply_key_account_pin_sets_site_contextvar(clean_routing):
    import buddy2api.server as server

    server._apply_key_account_pin({"default_site": "domestic"})
    assert auth_manager.pinned_site() == "domestic"

    server._apply_key_account_pin({"default_site": "garbage"})
    assert auth_manager.pinned_site() == ""

    server._apply_key_account_pin(None)
    assert auth_manager.pinned_site() == ""


def test_validate_key_site_rejects_unknown_values():
    import buddy2api.server as server
    from fastapi import HTTPException

    assert server._validate_key_site(None) == ""
    assert server._validate_key_site("") == ""
    assert server._validate_key_site("international") == "international"
    assert server._validate_key_site("Domestic") == "domestic"

    with pytest.raises(HTTPException) as error:
        server._validate_key_site("cn")
    assert error.value.status_code == 400


def test_admin_create_key_stores_default_site(clean_routing, monkeypatch):
    import asyncio

    import buddy2api.server as server

    monkeypatch.setattr(server, "_check_admin", lambda *_a, **_k: None)

    class FakeRequest:
        def __init__(self, payload):
            self._payload = payload

        async def stream(self):
            yield json.dumps(self._payload).encode("utf-8")

    result = asyncio.run(
        server.admin_create_key(
            FakeRequest({"name": "cn-only", "default_channel": "workbuddy",
                         "default_site": "domestic"}),
            "Bearer admin",
        )
    )
    assert result["default_site"] == "domestic"
    assert db.get_api_key_by_key(result["key"])["default_site"] == "domestic"


def test_admin_rejects_site_and_account_mismatch(clean_routing, monkeypatch):
    """站点限定与账号绑定必须自洽，否则请求永远选不到号。"""
    import asyncio

    import buddy2api.server as server
    from fastapi import HTTPException

    intl = _add_account("intl", "u-intl", domain="www.workbuddy.ai")
    cn = _add_account("cn", "u-cn", domain="www.workbuddy.cn")
    monkeypatch.setattr(server, "_check_admin", lambda *_a, **_k: None)

    class FakeRequest:
        def __init__(self, payload):
            self._payload = payload

        async def stream(self):
            yield json.dumps(self._payload).encode("utf-8")

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            server.admin_create_key(
                FakeRequest({"name": "bad", "default_channel": "workbuddy",
                             "default_site": "domestic", "default_account": intl}),
                "Bearer admin",
            )
        )
    assert error.value.status_code == 400

    kid = db.add_api_key("sk-cb-mix", "mix", default_account=cn, default_site="domestic")
    with pytest.raises(HTTPException) as error:
        asyncio.run(
            server.admin_update_key(kid, FakeRequest({"default_site": "international"}), "Bearer admin")
        )
    assert error.value.status_code == 400

    # 解除绑定后可以自由改站点
    asyncio.run(
        server.admin_update_key(
            kid, FakeRequest({"default_account": 0, "default_site": "international"}), "Bearer admin",
        )
    )
    assert db.get_api_key_by_key("sk-cb-mix")["default_site"] == "international"


def test_database_normalizes_unknown_default_site(clean_routing):
    """写入侧兜底：任何调用方都写不进未知站点值。"""
    kid = db.add_api_key("sk-cb-bogus", "bogus", default_site="cn")
    assert db.get_api_key_by_key("sk-cb-bogus")["default_site"] == ""

    db.update_api_key(kid, {"default_site": "moon"})
    assert db.get_api_key_by_key("sk-cb-bogus")["default_site"] == ""
