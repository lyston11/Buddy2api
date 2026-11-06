"""账号积分口径：必须与官方桌面端算出来的数字一致。

## 事故

管理页「官方余额」显示的积分数比官方界面多，国内账号尤其明显
（190****9272 显示 600，官方只有 100；19730034833 显示 2180，官方 2176.49）。
用户的原话是「我的几个国内账号根本都没有显示的那么多积分」。

## 两个独立的原因

1. **总余额读错了字段**。旧实现读 `POST /v2/billing/meter/get-user-resource` 的
   `data.Response.Data.TotalDosage`。那是服务端的另一种累加，**官方客户端自己都不用**
   （见下：它按分组拉明细自己求和）。实测 190****9272 的 `TotalDosage=600`
   而官方界面 `TotalCredits=100`。

2. **体验版切片包按整包上限累加**。国内个人体验版（`CapacityType=4`）是**按月切片**
   的额度：真实可用量在 `SlicePeriodUsageDetails[0].SlicePeriodCapacityRemainPrecise`，
   而 `CycleCapacityRemainPrecise` 报的是整包上限 500。旧代码把 500 当余额累加，
   于是国内账号凭空多出 500 分 —— 这正是 19730034833 那 2180 vs 2176.49 里的大部分差额。

## 官方口径（app.asar → BackendProvider.getPersonalUsage）

```
api1  POST /billing/meter/get-user-resource-summary   → 总余额（服务端已去重，无业务参数）
api2  POST /billing/meter/get-user-resource-paid-packages
api3  POST /billing/meter/get-user-resource-free-packages
```

三个路由都**不带** `/v2`（v2 路径直接 404，实测）。请求体固定带
`Status: [0, 3]`（有效 + 已用完）、按 `PackageCodes` 分组、每页 200 条翻页；
api3 额外带当天的 `SlicePeriodStartTime` / `SlicePeriodEndTime`，否则切片包拿不到明细。
总余额 = api1 的 `Packages[].CycleRemainCapacity` 求和（即 `sumSummaryCapacity`）。

## 本文件里的样本

都是 2026-11-06 从真实账号抓下来的脱敏响应（只保留余额相关字段，去掉
AccountId / Uin / AppId / DealName 等身份信息）。期望值就是同一次抓取里
官方 api1 自报的 `CycleRemainCapacity` 之和 / `TotalCredits`。
"""

import asyncio

import pytest

import buddy2api.auth_manager as auth_manager
import buddy2api.credential_crypto as credential_crypto
import buddy2api.database as db
from conftest import patch_upstream_client

CN_DOMAIN = "www.workbuddy.cn"
INTL_DOMAIN = "www.workbuddy.ai"

# ---------------------------------------------------------------- 真实样本

# 190****9272（国内站、体验版）。官方界面 = 100。
#
# 关键点：TotalDosage 是 600（裂变包 100 + 体验版整包 500），但 api1 汇总里
# 体验版那行 CycleRemainCapacity=0（已被用光，切片窗口内没有余额），
# 所以官方余额 = 100。旧实现按 TotalDosage 报 600。
ACCT_190_PACKAGES = [
    {
        "CapacityType": 1,
        "CycleStartTime": "2026-10-16 11:10:54",
        "CycleEndTime": "2026-11-16 11:10:53",
        "DeductionStartTime": 1792120254000,
        "DeductionEndTime": 1794798653000,
        "ExpiredTime": "2026-10-30 16:31:12",
        "PackageCode": "TCACA_code_030_BjSt89qTvr",
        "PackageName": "CodeBuddy个人版拉新权益包",
        "Status": 3,
        "ResourceId": "codebuddy-k86012f2d9eovl4",
        "CycleCapacitySizePrecise": "1500",
        "CycleCapacityRemainPrecise": "0",
        "CycleCapacityUsedPrecise": "1500",
    },
    {
        "CapacityType": 1,
        "CycleStartTime": "2026-10-17 12:56:00",
        "CycleEndTime": "2026-11-17 12:55:59",
        "DeductionStartTime": 1792212960000,
        "DeductionEndTime": 1794891359000,
        "ExpiredTime": "2026-11-02 12:00:09",
        "PackageCode": "TCACA_code_007_nzdH5h4Nl0",
        "PackageName": "CodeBuddy个人版国内运营裂变包",
        "Status": 3,
        "ResourceId": "codebuddy-k8700t5eusebel4",
        "CycleCapacitySizePrecise": "100",
        "CycleCapacityRemainPrecise": "0",
        "CycleCapacityUsedPrecise": "100",
    },
    {
        "CapacityType": 1,
        "CycleStartTime": "2026-11-06 13:48:16",
        "CycleEndTime": "2026-12-06 13:48:15",
        "DeductionStartTime": 1793944096000,
        "DeductionEndTime": 1796536095000,
        "PackageCode": "TCACA_code_007_nzdH5h4Nl0",
        "PackageName": "CodeBuddy个人版国内运营裂变包",
        "Status": 0,
        "ResourceId": "codebuddy-k8r00t7e2f6gul4",
        "CycleCapacitySizePrecise": "100",
        "CycleCapacityRemainPrecise": "100",
        "CycleCapacityUsedPrecise": "0",
    },
]
ACCT_190_SUMMARY = {
    "Packages": [
        {"PackageCode": "TCACA_code_007_nzdH5h4Nl0", "CycleTotalCapacity": "2000",
         "CycleRemainCapacity": "100", "CycleUsedCapacity": "1900", "TotalCount": 20},
        {"PackageCode": "TCACA_code_008_cfWoLwvjU4", "CycleTotalCapacity": "500",
         "CycleRemainCapacity": "0", "CycleUsedCapacity": "500", "TotalCount": 1},
        {"PackageCode": "TCACA_code_030_BjSt89qTvr", "CycleTotalCapacity": "1500",
         "CycleRemainCapacity": "0", "CycleUsedCapacity": "1500", "TotalCount": 1},
    ],
    "TotalCredits": "100",
    "SubscriptionPackageCode": "",
    "SubscriptionPackageName": "体验版",
    "IsPaidUser": False,
}
ACCT_190_OFFICIAL_LEFT = 100.0
# 上游 get-user-resource 的 TotalDosage（旧实现读的就是它）
ACCT_190_TOTAL_DOSAGE = 600

# 体验版切片包（CapacityType=4）：整包上限 500，但当期切片只剩 0。
SLICE_PACKAGE = {
    "CapacityType": 4,
    "CapacityUnit": "credits",
    "CycleStartTime": "2026-11-01 00:00:00",
    "CycleEndTime": "2026-11-30 23:59:59",
    "DeductionStartTime": 1792120252000,
    "DeductionEndTime": 2052529851000,
    "ExpiredTime": "",
    "PackageCode": "TCACA_code_008_cfWoLwvjU4",
    "PackageName": "CodeBuddy个人体验版",
    "Status": 0,
    "ResourceId": "codebuddy-k86012f2d8ht7l4",
    "CycleCapacitySizePrecise": "500",
    "CycleCapacityRemainPrecise": "0",
    "CycleCapacityUsedPrecise": "500",
}
# 同一条包，后端这次下发了当期切片明细：真实可用 123.5（不是 500）
SLICE_PACKAGE_WITH_DETAIL = {
    **SLICE_PACKAGE,
    "ResourceId": "codebuddy-slice-with-detail",
    "SlicePeriodUsageDetails": [
        {"SlicePeriodCapacitySizePrecise": "500", "SlicePeriodCapacityRemainPrecise": "123.5"}
    ],
}


# ---------------------------------------------------------------- 夹具

@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "gateway.db")
    monkeypatch.setenv("CB_GATEWAY_MASTER_KEY", "pytest-master-key")
    credential_crypto.reset_cache()
    db.init_db()
    yield tmp_path
    credential_crypto.reset_cache()


def _make_account(uid: str = "u1", domain: str = CN_DOMAIN) -> int:
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


class _FakeBillingClient:
    """按路由回放响应，并记录每次请求的 (path, body)，供断言请求体口径。"""

    def __init__(self, summary: dict, packages: list[dict], *, summary_status: int = 200):
        self.summary = summary
        self.packages = packages
        self.summary_status = summary_status
        self.calls: list[tuple[str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        self.calls.append((url, dict(json or {})))
        if "get-user-resource-summary" in url:
            return _FakeResponse(self.summary_status, {"code": 0, "msg": "OK", "data": self.summary})
        if "get-user-resource-paid-packages" in url or "get-user-resource-free-packages" in url:
            return _FakeResponse(
                200,
                {"code": 0, "msg": "OK", "data": {"Accounts": self.packages, "TotalCount": len(self.packages)}},
            )
        return _FakeResponse(404, {"code": 404, "msg": "not found"})


def _patch(monkeypatch, client) -> None:
    async def fake_headers(account):
        return {"authorization": "Bearer test"}

    monkeypatch.setattr(auth_manager, "get_billing_headers", fake_headers)
    patch_upstream_client(monkeypatch, client)


def _fetch(aid: int) -> dict:
    return asyncio.run(
        auth_manager.fetch_account_resources(db.get_account(aid), force=True, allow_stale=False)
    )


# ---------------------------------------------------------------- 总余额口径

def test_total_matches_official_summary_not_total_dosage(isolated_db, monkeypatch):
    """总余额必须等于 api1 汇总（官方界面），不能是 get-user-resource 的 TotalDosage。

    回归：190****9272 旧实现显示 600，官方界面 100。
    """
    aid = _make_account(uid="190****9272", domain=CN_DOMAIN)
    client = _FakeBillingClient(ACCT_190_SUMMARY, ACCT_190_PACKAGES)
    _patch(monkeypatch, client)

    result = _fetch(aid)

    assert result["ok"] is True
    assert result["remaining"] == pytest.approx(ACCT_190_OFFICIAL_LEFT)
    assert result["available_total"] == pytest.approx(ACCT_190_OFFICIAL_LEFT)
    # total_dosage 这个字段名留着做兼容，但值必须是官方口径，不能再回退到上游 TotalDosage
    assert result["total_dosage"] == pytest.approx(ACCT_190_OFFICIAL_LEFT)
    assert result["total_dosage"] != pytest.approx(ACCT_190_TOTAL_DOSAGE), (
        "绝不能回到读 TotalDosage 的老口径（那正是显示值偏高的原因）"
    )


def test_slice_package_uses_period_detail_not_full_package_cap(isolated_db, monkeypatch):
    """体验版切片包按当期切片余量算，不是按整包上限 500。

    回归：19730034833 旧实现 2180，官方 2176.49 —— 差额来自把 500 当成可用余额。
    """
    aid = _make_account(uid="slice", domain=CN_DOMAIN)
    summary = {
        "Packages": [
            {"PackageCode": "TCACA_code_008_cfWoLwvjU4", "CycleRemainCapacity": "123.5"},
        ],
    }
    client = _FakeBillingClient(summary, [SLICE_PACKAGE_WITH_DETAIL])
    _patch(monkeypatch, client)

    result = _fetch(aid)

    assert result["remaining"] == pytest.approx(123.5)
    slice_row = next(p for p in result["packages"] if p["package_code"] == "TCACA_code_008_cfWoLwvjU4")
    assert slice_row["remaining_precise"] == pytest.approx(123.5)
    assert slice_row["slice_remaining"] == pytest.approx(123.5)


def test_slice_package_without_detail_falls_back_to_cycle_remain(isolated_db, monkeypatch):
    """后端对国内 freeMon 可能不下发切片明细：此时回落周期余量，不能当 500 用。

    官方 `resolveResourceCapacity` 的注释写着这是必须的回落（#105166），
    否则体验版整行会显示 0；但不能回落到整包 CapacityRemain。
    """
    aid = _make_account(uid="slice-fallback", domain=CN_DOMAIN)
    client = _FakeBillingClient({"Packages": []}, [SLICE_PACKAGE])
    _patch(monkeypatch, client)

    result = _fetch(aid)

    slice_row = next(p for p in result["packages"] if p["package_code"] == "TCACA_code_008_cfWoLwvjU4")
    assert slice_row["remaining_precise"] == pytest.approx(0.0), "当期余量是 0 就是 0"
    assert slice_row["size"] == pytest.approx(500.0)


def test_summary_failure_refuses_to_report_a_total_dosage_number(isolated_db, monkeypatch):
    """api1 挂了就报失败，绝不退回读 TotalDosage（那会是一个偏大的假余额）。

    官方客户端在这种情况下也是硬失败（`refusing to report incomplete credits`）：
    宁可让管理页显示「失败」或旧缓存，也不要给一个看起来正常、实际偏高的积分。
    """
    aid = _make_account(uid="no-summary", domain=CN_DOMAIN)
    client = _FakeBillingClient({}, ACCT_190_PACKAGES, summary_status=500)
    _patch(monkeypatch, client)

    result = _fetch(aid)

    assert result["ok"] is False
    # 失败结果里绝不能出现一个看着正常的 600（旧 TotalDosage 口径）
    assert result.get("remaining") is None
    assert result.get("total_dosage", 0) == 0
    # 失败结果不落缓存，下一次刷新会重试而不是回放一个不可信的数
    assert db.get_account_resource_cache(aid) is None


def test_remaining_falls_back_to_official_package_math_when_summary_is_empty():
    """api1 缺席时回退到「按官方算法算出的有效包之和」，而不是 TotalDosage。"""
    active = [
        {"remaining_precise": 100.0},
        {"remaining_precise": 23.5},
    ]
    assert auth_manager._wb_official_remaining({}, active) == pytest.approx(123.5)
    # 有 api1 时一律以服务端汇总为准（它已做过订阅去重与切片处理）
    summary = {"Packages": [{"CycleRemainCapacity": "100"}, {"CycleRemainCapacity": "23.5"}]}
    assert auth_manager._wb_official_remaining(summary, active) == pytest.approx(123.5)


# ---------------------------------------------------------------- 请求口径

def test_billing_uses_official_routes_and_bodies(isolated_db, monkeypatch):
    """请求必须打到官方三个路由（不带 /v2），带 Status=[0,3] 与切片窗口。"""
    aid = _make_account(uid="routes", domain=CN_DOMAIN)
    client = _FakeBillingClient(ACCT_190_SUMMARY, ACCT_190_PACKAGES)
    _patch(monkeypatch, client)

    _fetch(aid)

    paths = [url.split("//", 1)[1].split("/", 1)[1] for url, _ in client.calls]
    assert "billing/meter/get-user-resource-summary" in paths
    assert "billing/meter/get-user-resource-paid-packages" in paths
    assert "billing/meter/get-user-resource-free-packages" in paths
    assert not any("/v2/billing/" in p for p in paths), "官方三接口的路由不带 /v2（v2 会 404）"
    assert "v2/billing/meter/get-user-resource" not in paths, "不应再请求老的 get-user-resource 接口"

    free_bodies = [body for url, body in client.calls if "free-packages" in url]
    assert free_bodies, "必须拉免费包，否则体验版切片明细拿不到"
    for body in free_bodies:
        assert body["Status"] == [0, 3], "Status 必须是 [0, 3]（有效 + 已用完），与官方一致"
        assert body["PageSize"] == 200
        assert body["SlicePeriodStartTime"] and body["SlicePeriodEndTime"], "切片窗口必须带"
        assert len(body["PackageCodes"]) >= 11, "免费包商品码集必须完整"

    paid_bodies = [body for url, body in client.calls if "paid-packages" in url]
    for body in paid_bodies:
        assert body["Status"] == [0, 3]
        assert body["NeedRenewInfo"] is True


# ---------------------------------------------------------------- 到期口径

def test_expiring_totals_use_official_expiry(isolated_db, monkeypatch):
    """「即将到期」只统计真实有效包，已过 ExpiredTime 的包不参与。"""
    aid = _make_account(uid="expiring", domain=CN_DOMAIN)
    client = _FakeBillingClient(
        {"Packages": [{"PackageCode": "TCACA_code_007_nzdH5h4Nl0", "CycleRemainCapacity": "100"}]},
        ACCT_190_PACKAGES,
    )
    _patch(monkeypatch, client)

    result = _fetch(aid)

    active = [p for p in result["packages"] if not p["expired"]]
    assert len(active) == 1, "三条包里只有 2026-11-06 那条还在有效期内"
    assert result["expiring_30d_total"] == pytest.approx(100.0)
    assert result["next_expire_amount"] == pytest.approx(100.0)
    assert result["next_expire_days"] == 29


def test_expired_status3_package_is_not_counted_as_available(isolated_db, monkeypatch):
    """Status=3 且 ExpiredTime 已过的包，剩余必须是 0，不能混进有效余额（回归：多算 500）。"""
    aid = _make_account(uid="status3", domain=CN_DOMAIN)
    client = _FakeBillingClient({"Packages": []}, ACCT_190_PACKAGES)
    _patch(monkeypatch, client)

    result = _fetch(aid)

    expired_rows = [p for p in result["packages"] if p["expired"]]
    assert expired_rows, "样本里应当有已过期的包"
    for row in expired_rows:
        assert row["remaining_precise"] == pytest.approx(0.0)


# ---------------------------------------------------------------- 缓存

def test_cache_is_served_without_new_request_and_keeps_new_shape(isolated_db, monkeypatch):
    """缓存回放要保留新口径的值，且不再发请求。"""
    aid = _make_account(uid="cached", domain=CN_DOMAIN)
    client = _FakeBillingClient(ACCT_190_SUMMARY, ACCT_190_PACKAGES)
    _patch(monkeypatch, client)
    _fetch(aid)
    calls_after_refresh = len(client.calls)

    cached = asyncio.run(
        auth_manager.fetch_account_resources(db.get_account(aid), force=False, max_age_seconds=300)
    )

    assert len(client.calls) == calls_after_refresh, "命中缓存时不该再打上游"
    assert cached["stale"] is False
    assert cached["remaining"] == pytest.approx(ACCT_190_OFFICIAL_LEFT)
    assert cached["age_seconds"] >= 0


def test_old_schema_cache_is_ignored(isolated_db, monkeypatch):
    """旧口径的额度缓存不能被当新口径回放。

    版本升级前的缓存里 `total_dosage` 是上游 TotalDosage（偏大），`available_total`
    则是把体验版按整包上限累加的结果。直接回放等于把这次的偏差又带回来，
    所以缓存必须带 schema 版本，不匹配就重新拉。降级（失败回旧缓存）同理。
    """
    aid = _make_account(uid="legacy-cache", domain=CN_DOMAIN)
    db.upsert_account_resource_cache(
        aid,
        {
            "ok": True,
            "total_dosage": ACCT_190_TOTAL_DOSAGE,          # 旧口径：600
            "available_total": ACCT_190_TOTAL_DOSAGE + 500,  # 旧口径：还把切片包整包算进去
            "packages": [],
            "updated_at": int(__import__("time").time()),
        },
    )

    client = _FakeBillingClient(ACCT_190_SUMMARY, ACCT_190_PACKAGES)
    _patch(monkeypatch, client)

    result = _fetch(aid)

    assert client.calls, "旧 schema 缓存必须被忽略，重新拉上游"
    assert result["remaining"] == pytest.approx(ACCT_190_OFFICIAL_LEFT)
    assert result["schema"] == auth_manager._WB_RESOURCE_SCHEMA


def test_old_schema_cache_is_not_used_as_stale_fallback(isolated_db, monkeypatch):
    """拉取失败时也不能拿旧口径缓存当降级快照（宁可显示失败）。"""
    aid = _make_account(uid="legacy-fallback", domain=CN_DOMAIN)
    db.upsert_account_resource_cache(
        aid,
        {"ok": True, "total_dosage": ACCT_190_TOTAL_DOSAGE, "available_total": 1100, "packages": []},
    )

    client = _FakeBillingClient({}, [], summary_status=500)
    _patch(monkeypatch, client)

    result = _fetch(aid)

    assert result["ok"] is False
    assert result.get("remaining") is None
    assert result.get("total_dosage", 0) == 0
