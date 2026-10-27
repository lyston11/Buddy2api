"""签到活动语义：`active=false` 是「上游没开这个活动」，不是「领取失败」。

回归（2026-09-16）：国际站账号（domain=www.workbuddy.ai）点「一键领取今日积分」时，
弹出「活动当前不可用」并计入 `failed`，看起来像服务故障。

实查上游原始响应（按下面 payload 复刻）：

- `POST /v2/billing/meter/checkin-activity-status`：
  国际站 www.workbuddy.ai 回 `active=false`（`start_time`/`end_time` 为空串、
  `checkin_dates=[]`、`season=1`）；国内站 www.codebuddy.cn 回 `active=true`
  且有真实活动周期与 `season=9`。
- `POST /v2/billing/meter/daily-checkin`（关键旁证）：两个站点都回
  `400 code=10001`，国际站文案是**「签到活动未开启或已过期」**，
  国内站同码文案是「今天已签到，请明天再来」。

即 `active=false` 是**上游的活动开关状态**，与账号、凭证、我们的请求头都无关
（已用三种 header 变体实测，结果一致）。所以正确语义是「上游未开启」，
而不是「我们做失败了」——不写进已逝的臆测（如「活动从未配置」）。

处理方式必须是**提示区分**，绝不能绕过上游的活动开关去强行 claim。
"""

import asyncio
from pathlib import Path

import pytest

import buddy2api.auth_manager as auth_manager
from conftest import patch_upstream_client
import buddy2api.control_plane as control_plane
import buddy2api.credential_crypto as credential_crypto
import buddy2api.database as db

INTL_DOMAIN = "www.workbuddy.ai"
CN_DOMAIN = "www.codebuddy.cn"

# 上游国际站真实响应（2026-09-16 实测复刻）
INTL_INACTIVE_PAYLOAD = {
    "code": 0,
    "msg": "OK",
    "data": {
        "active": False,
        "today_checked_in": False,
        "streak_days": 0,
        "daily_credit": 100,
        "today_credit": 100,
        "is_streak_day": False,
        "checkin_dates": [],
        "week_checkin_days": 0,
        "total_credits": 0,
        "start_time": "",
        "end_time": "",
        "theme_name": "Buddy 加油站",
        "season": 1,
        "activity_name": "本期：专家能量包",
        "claim_button_text": "立即领取",
    },
}

CN_ACTIVE_PAYLOAD = {
    "code": 0,
    "msg": "OK",
    "data": {
        "active": True,
        "today_checked_in": False,
        "streak_days": 1,
        "daily_credit": 100,
        "today_credit": 100,
        "checkin_dates": ["2026-09-16"],
        "week_checkin_days": 1,
        "total_credits": 100,
        "start_time": "2026-09-16 00:00:00",
        "end_time": "2026-09-29 23:59:59",
        "theme_name": "Buddy加油站",
        "season": 9,
        "activity_name": "高校新生攻略",
    },
}


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    path = tmp_path / "gateway.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setenv("CB_GATEWAY_MASTER_KEY", "pytest-master-key")
    credential_crypto.reset_cache()
    db.init_db()
    yield path
    credential_crypto.reset_cache()


def _make_account(uid: str = "u1", domain: str = INTL_DOMAIN) -> int:
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


class _FakeResponse:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, response):
        self._response = response
        self.requests: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        self.requests.append(url)
        return self._response


def _patch_headers(monkeypatch):
    async def fake_headers(account):
        return {"authorization": "Bearer test"}

    monkeypatch.setattr(auth_manager, "get_billing_headers", fake_headers)


def test_inactive_site_is_not_reported_as_failure(isolated_db, monkeypatch):
    """active=false 时 ok 保持 True、标 unavailable，且不发起 claim 请求。"""
    _patch_headers(monkeypatch)
    aid = _make_account(domain=INTL_DOMAIN)
    client = _FakeClient(_FakeResponse(200, INTL_INACTIVE_PAYLOAD))

    patch_upstream_client(monkeypatch, client)

    result = asyncio.run(auth_manager.claim_daily_checkin(db.get_account(aid)))

    assert result["ok"] is True, "活动未开启不该被判为失败"
    assert result["unavailable"] is True
    assert result["claimed"] is False
    assert result["message"] == "签到活动未开启或已过期"
    # 关键：不得去戳 daily-checkin，绝不绕过上游活动开关
    assert not any("daily-checkin" in url for url in client.requests), (
        f"active=false 时不应发起领取请求，实际请求了 {client.requests}"
    )


def test_active_site_still_claims_normally(isolated_db, monkeypatch):
    """国内站 active=true 的路径不受影响，仍会真的发起领取。"""
    _patch_headers(monkeypatch)
    aid = _make_account(domain=CN_DOMAIN)
    client = _FakeClient(_FakeResponse(200, CN_ACTIVE_PAYLOAD))
    client.requests = []

    async def post(url, headers=None, json=None):
        client.requests.append(url)
        if "daily-checkin" in url:
            return _FakeResponse(
                200,
                {"code": 0, "msg": "OK", "data": {"credit": 100, "today_checked_in": True}},
            )
        return _FakeResponse(200, CN_ACTIVE_PAYLOAD)

    monkeypatch.setattr(client, "post", post)

    patch_upstream_client(monkeypatch, client)

    result = asyncio.run(auth_manager.claim_daily_checkin(db.get_account(aid)))

    assert any("daily-checkin" in url for url in client.requests)
    assert result["unavailable"] is False
    assert result["claimed"] is True


def test_status_all_excludes_inactive_from_available(isolated_db, monkeypatch):
    """无活动账号既不进 available 也不进 failed，单独计 unavailable。"""
    intl = _make_account(uid="intl-1", domain=INTL_DOMAIN)
    cn = _make_account(uid="cn-1", domain=CN_DOMAIN)

    async def fake_fetch(account, force=False):
        if account["domain"] == INTL_DOMAIN:
            row = auth_manager._checkin_result(
                account,
                ok=True,
                payload=INTL_INACTIVE_PAYLOAD["data"],
                message="签到活动未开启或已过期",
            )
            row["unavailable"] = True
            return row
        return auth_manager._checkin_result(
            account, ok=True, payload=CN_ACTIVE_PAYLOAD["data"], message="OK"
        )

    monkeypatch.setattr(auth_manager, "fetch_checkin_status", fake_fetch)

    summary = asyncio.run(control_plane.checkin_status_all(force=True))
    by_id = {row["account_id"]: row for row in summary["results"]}

    assert by_id[intl]["unavailable"] is True
    assert summary["unavailable"] == 1
    assert summary["failed"] == 0, "无活动不算失败"
    assert summary["available"] == 1, "只有国内站账号可领"
    assert by_id[cn]["unavailable"] is False


def test_checkin_all_failed_excludes_unavailable(isolated_db, monkeypatch):
    """一键领取汇总里，无活动账号不计入 failed，但单独可见。"""
    _make_account(uid="intl-1", domain=INTL_DOMAIN)

    async def fake_claim(account):
        row = auth_manager._checkin_result(
            account,
            ok=True,
            payload=INTL_INACTIVE_PAYLOAD["data"],
            message="签到活动未开启或已过期",
        )
        row["unavailable"] = True
        return row

    monkeypatch.setattr(auth_manager, "claim_daily_checkin", fake_claim)
    monkeypatch.setattr(control_plane, "checkin_gap_seconds", lambda: 0)

    summary = asyncio.run(control_plane.checkin_all())

    assert summary["failed"] == 0, "活动未开启不应显示为失败"
    assert summary["unavailable"] == 1
    assert summary["claimed"] == 0


def test_real_failure_still_counts_as_failed(isolated_db, monkeypatch):
    """真正的失败（如凭证失效）必须仍然计入 failed，别把语义改宽了。"""
    _make_account(uid="intl-1", domain=INTL_DOMAIN)

    async def fake_claim(account):
        return auth_manager._checkin_result(
            account,
            ok=False,
            message="token refresh failed or account credentials are invalid",
        )

    monkeypatch.setattr(auth_manager, "claim_daily_checkin", fake_claim)
    monkeypatch.setattr(control_plane, "checkin_gap_seconds", lambda: 0)

    summary = asyncio.run(control_plane.checkin_all())

    assert summary["failed"] == 1
    assert summary["unavailable"] == 0


# ---------------------------------------------------------------------------
# 管理页提示：单账号「领取」按钮与一键领取必须用同一套文案
# ---------------------------------------------------------------------------


def _index_html() -> str:
    return (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(encoding="utf-8")


def test_claim_one_toast_distinguishes_inactive_activity():
    """单账号「领取」的 toast 也要认 unavailable。

    回归：一键领取补了 unavailable 分支，但 `claimOne` 的 toast 仍是
    `r.claimed?'领取成功':(r.already_claimed?'今日已领':'领取失败')`，
    于是国际站账号点单个「领取」仍弹红字「领取失败」——同一件事两个入口两种结论。
    """
    html = _index_html()
    line = next(
        (ln for ln in html.splitlines() if ln.startswith("  async function claimOne(a)")),
        None,
    )
    assert line, "未找到 claimOne 定义"
    assert "r.unavailable||r.active===false" in line, (
        "claimOne 的 toast 必须区分「活动未开启」，否则会误报领取失败"
    )
    assert "'签到活动未开启或已过期'" in line


def test_claim_text_and_one_click_summary_share_the_wording():
    """claimText 与一键领取汇总的措辞保持一致，避免同一状态两种说法。"""
    html = _index_html()
    text_line = next(
        (ln for ln in html.splitlines() if ln.startswith("  function claimText(r)")),
        None,
    )
    assert text_line and "r.unavailable||r.active===false" in text_line
    assert "claim.summary.unavailable" in html, "一键领取弹窗应单列「活动未开启/已过期」"
