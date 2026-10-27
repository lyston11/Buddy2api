"""上游「这个账号本身不能用」的判定：必须和「限流」彻底分开。

背景（2026-09-25 / 09-26 的真实事故，库里有完整日志）：

- 账号 sunnylw1109 吃 `429 code=14017`（试用未激活）后，网关把它当**限流**处理 ——
  只给「账号 × 模型」900 秒冷却。冷却一过它就因为「零负载」回到候选池最前面，
  于是每 15 分钟被派一次活，连打 40 次 14017，全部白跑。
- 2026-09-26 四个账号吃 `403 code=11140`（账号级风控封禁）后同理，被反复重试了
  925 次。

上游把真正的判定放在 `error.data.code` 里，HTTP status 只是粗包装（14017/14018 是
429，11140 是 403，11102 是 400），所以只看 status 一定分不开这两件事。
"""

import asyncio

import pytest

import buddy2api.auth_manager as auth_manager
import buddy2api.credential_crypto as credential_crypto
import buddy2api.database as db
import buddy2api.proxy as proxy


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    path = tmp_path / "gateway.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setenv("CB_GATEWAY_MASTER_KEY", "pytest-master-key")
    credential_crypto.reset_cache()
    db.init_db()
    auth_manager.clear_all_account_unavailable()
    yield path
    auth_manager.clear_all_account_unavailable()
    credential_crypto.reset_cache()


def _make_account(uid: str = "u1", domain: str = "www.workbuddy.ai") -> int:
    return db.add_account(
        {
            "name": uid,
            "uid": uid,
            "provider": "workbuddy",
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "domain": domain,
            "status": "active",
            "expires_at": 4_000_000_000_000,
        }
    )


# ---------------------------------------------------------------- 业务码提取

@pytest.mark.parametrize(
    "detail,expected",
    [
        ({"error": {"data": {"code": 14017, "msg": "trial"}}}, 14017),
        ({"error": {"data": {"code": 11140, "msg": "request illegal"}}}, 11140),
        ({"error": {"code": 11102, "msg": "model [x] service info not found"}}, 11102),
        ({"code": 14018}, 14018),
        ({"error": {"message": "boom"}}, 0),
        ("not a dict", 0),
        (None, 0),
    ],
)
def test_upstream_error_code_reads_error_data_code(detail, expected):
    assert proxy._upstream_error_code(detail) == expected


def test_account_unavailable_code_only_matches_known_codes():
    assert proxy._account_unavailable_code(429, {"error": {"data": {"code": 14017}}}) == 14017
    assert proxy._account_unavailable_code(403, {"error": {"data": {"code": 11140}}}) == 11140
    # 普通限流（6004）和模型不可用（11102）都不是账号级不可用
    assert proxy._account_unavailable_code(429, {"error": {"data": {"code": 6004}}}) == 0
    assert proxy._account_unavailable_code(400, {"error": {"data": {"code": 11102}}}) == 0


# ---------------------------------------------------------------- 冷却与选路

def test_unavailable_account_gets_long_cooldown_not_rate_limit(isolated_db):
    aid = _make_account()
    reason = auth_manager.mark_account_unavailable(aid, 14017)
    assert reason == "trial_not_activated"
    # 不是「账号 × 模型」的 900 秒限流冷却
    assert not auth_manager.account_model_rate_limited(aid, "deepseek-v4.1-flash")
    assert auth_manager.account_unavailable_reason(aid) == "trial_not_activated(code=14017)"

    snapshot = auth_manager.account_unavailable_snapshot()
    assert snapshot[aid]["code"] == 14017
    # 默认 6 小时，远长于限流的 15 分钟
    assert snapshot[aid]["seconds_left"] > 5 * 3600


def test_unavailable_account_is_skipped_by_router(isolated_db, monkeypatch):
    bad = _make_account("bad")
    good = _make_account("good")
    auth_manager.mark_account_unavailable(bad, 11140)

    monkeypatch.setattr(
        auth_manager, "model_site_preference", lambda: {"default": "auto", "models": {}}
    )
    monkeypatch.setattr(auth_manager, "_auto_site_preference", lambda *_a, **_k: None)
    picked = asyncio.run(
        auth_manager.pick_account_with_fallback(set(), model="deepseek-v4.1-flash")
    )
    assert picked is not None
    assert picked["id"] == good, "被封的账号不该再被选中"


def test_clear_unavailable_on_refresh_success(isolated_db):
    aid = _make_account()
    auth_manager.mark_account_unavailable(aid, 14017)
    assert auth_manager.account_unavailable_reason(aid)
    auth_manager.clear_account_unavailable(aid)
    assert auth_manager.account_unavailable_reason(aid) is None


def test_unknown_code_is_not_treated_as_unavailable(isolated_db):
    aid = _make_account()
    assert auth_manager.mark_account_unavailable(aid, 6004) is None
    assert auth_manager.account_unavailable_reason(aid) is None


# ---------------------------------------------------------------- 重试计划

def test_plan_retry_treats_account_unavailable_as_switchable():
    """账号级不可用：换号有效、用满账号池额度、不占链路额度。"""
    will_retry, link = proxy._plan_retry(
        429, attempt=0, max_attempts=8, link_failures=0, account_unavailable=True
    )
    assert will_retry is True and link == 0

    # 额度用尽后不再换号
    will_retry, _ = proxy._plan_retry(
        429, attempt=7, max_attempts=8, link_failures=0, account_unavailable=True
    )
    assert will_retry is False


def test_plan_retry_plain_429_still_rate_limit_flow():
    """普通 429 仍走原路径（链路额度不受影响）。"""
    will_retry, link = proxy._plan_retry(
        429, attempt=0, max_attempts=8, link_failures=0
    )
    assert will_retry is True and link == 0
