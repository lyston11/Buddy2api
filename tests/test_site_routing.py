"""混装国内/国际账号时的站点判定与模型路由。

背景（实测，2026-09）：国内站与国际站暴露的模型集几乎不重叠 ——
国际站 18 个（default-model / gpt-5.6-sol / gemini-3.5-flash / …），
国内站 29 个（auto / deepseek-v4.1-flash / glm-4.6 / kimi-k2.5 / …），
交集只有 hy3、hy4-preview、glm-5.3 三个。

而模型目录是按「通道」存的，混装账号时目录必然是并集，单看目录无法判断某个账号
能不能服务某个模型。于是此前会这样失败：

  1. 选号完全不看模型，粘性又只按 provider 分一个槽 —— 一旦粘住某个账号，所有
     请求都压在它身上（「只会请求一个账号」）。
  2. 请求只属于另一个站的模型时，上游回 HTTP 400（code 11102：
     `model [...] service info not found` 或 `model [...] is only available for
     authorized users`），而 400 不在换号重试的集合里，客户端直接看到报错。

本文件把修复后的契约钉住：站点判定只有一处、选号按模型过滤、粘性按模型分槽且会
在明显跑偏时让位、模型不可用的 400 视为可换号错误。
"""

import asyncio
import time

import pytest

import buddy2api.auth_manager as auth_manager
import buddy2api.catalog as catalog
import buddy2api.database as db
import buddy2api.fingerprint as fingerprint
import buddy2api.proxy as proxy
import buddy2api.site_preference as site_preference
import buddy2api.sites as sites


@pytest.fixture(autouse=True)
def _clear_route_state():
    """路由与能力缓存都是模块级状态，测试之间必须清干净。"""
    def _reset():
        auth_manager._sticky_account_id.clear()
        auth_manager._account_failures.clear()
        auth_manager._model_rate_limits.clear()
        auth_manager._auth_failures.clear()
        auth_manager._verify_inflight.clear()
        auth_manager._account_models.clear()
        auth_manager._account_denied.clear()
        auth_manager.forget_cost_profile()

    _reset()
    yield
    _reset()


def _make_account(uid: str, domain: str, provider: str = "workbuddy", status: str = "active") -> int:
    return db.add_account(
        {
            "name": uid,
            "uid": uid,
            "provider": provider,
            "access_token": f"token-{uid}",
            "refresh_token": f"refresh-{uid}",
            "domain": domain,
            "status": status,
            "expires_at": 4_000_000_000_000,
        }
    )


def _set_requests(aid: int, total: int) -> None:
    """直接改 accounts.total_requests（终身计数，不在 update_account 白名单里）。

    注意：这**不再是选路的负载信号**（选路看 db.recent_account_loads 的窗口），
    只有专门验证「终身计数不影响路由」的测试才用它。要制造负载请用 _serve。
    """
    conn = db.get_conn()
    conn.execute("UPDATE accounts SET total_requests = ? WHERE id = ?", (total, aid))
    conn.commit()
    conn.close()


def _serve(
    aid: int,
    n: int = 1,
    *,
    model: str = "m",
    credit: float = 0.0,
    age_seconds: int = 0,
) -> None:
    """记录「这个账号服务了 n 次请求」—— 这才是选路的负载信号。

    选路用 db.recent_account_loads()（最近 ROUTE_WINDOW_SECONDS 内的成功请求数），
    数据来源就是 logs 表，所以测试必须真的写日志行，不能只改计数器。
    age_seconds 用来把记录推到窗口之外（验证窗口会过期）。
    """
    created = int(time.time()) - age_seconds
    conn = db.get_conn()
    for _ in range(n):
        conn.execute(
            "INSERT INTO logs (account_id, account_name, model, status_code, credit,"
            " created_at, provider) VALUES (?,?,?,200,?,?,'workbuddy')",
            (aid, f"acc-{aid}", model, credit, created),
        )
    conn.commit()
    conn.close()


# ============================================================
# 站点判定：只有一处定义
# ============================================================

def test_origin_matches_upstream_site_for_cn_accounts():
    """国内账号：请求发到哪个站，Origin/Referer 就必须自称哪个站。

    回归点：以前 fingerprint.origin_for 按域名里有没有 "workbuddy" 判定，
    于是 www.workbuddy.cn 的请求发往国内站、请求头却自称 www.workbuddy.ai。
    """
    for domain in ("www.workbuddy.cn", "www.codebuddy.cn"):
        assert auth_manager.backend_url_for({"domain": domain}) == f"https://{domain}"
        assert fingerprint.origin_for(domain) == f"https://{domain}"


def test_origin_for_non_cn_accounts_keeps_global_default():
    """域未知的非国内账号仍回退全局 backend_url（自定义 relay 语义不变）。

    `*.workbuddy.ai` 现在会在 backend_url 是官方默认值时改走自己的域（见
    test_auth_failure_policy），这里钉住的是**其余**非国内域不被牵连，
    以及 Origin 一律是国际站产品域。
    """
    for domain in ("www.codebuddy.ai", ""):
        assert auth_manager.backend_url_for({"domain": domain}) == auth_manager.backend_url()
    for domain in ("www.workbuddy.ai", "www.codebuddy.ai", ""):
        assert fingerprint.origin_for(domain) == "https://www.workbuddy.ai"


@pytest.mark.parametrize(
    "domain",
    ["www.workbuddy.cn", "www.codebuddy.cn", "workbuddy.cn", "foo.bar.cn", "WWW.CodeBuddy.CN/"],
)
def test_cn_domain_detection_accepts_real_domains(domain):
    assert sites.is_cn_domain(domain) is True
    assert sites.site_url(domain) == f"https://{sites.normalize_domain(domain)}"


@pytest.mark.parametrize(
    "domain",
    ["foocn.com", "www.workbuddy.cn.evil.com", "cn", "www.workbuddy.ai", "", None],
)
def test_cn_domain_detection_rejects_lookalikes(domain):
    assert sites.is_cn_domain(domain) is False
    assert sites.site_url(domain) is None


def test_cn_domain_detection_ignores_port():
    """带端口也要能识别，否则会静默回退到全局上游。"""
    assert sites.is_cn_domain("www.workbuddy.cn:443") is True
    assert sites.site_url("www.workbuddy.cn:443") == "https://www.workbuddy.cn:443"


# ============================================================
# 选号按模型过滤
# ============================================================

def _mixed_pool():
    """两个国际账号 + 两个国内账号，模型能力互不重叠（真实情况就是如此）。"""
    intl_a = _make_account("intl-a", "www.workbuddy.ai")
    intl_b = _make_account("intl-b", "www.workbuddy.ai")
    cn_a = _make_account("cn-a", "www.workbuddy.cn")
    cn_b = _make_account("cn-b", "www.codebuddy.cn")
    auth_manager.record_account_models(intl_a, ["gpt-5.6-sol", "hy3"])
    auth_manager.record_account_models(intl_b, ["gpt-5.6-sol", "hy3"])
    auth_manager.record_account_models(cn_a, ["auto", "deepseek-v4.1-flash", "hy3"])
    auth_manager.record_account_models(cn_b, ["auto", "deepseek-v4.1-flash", "hy3"])
    return intl_a, intl_b, cn_a, cn_b


def test_pick_account_only_returns_accounts_that_can_serve_the_model():
    intl_a, intl_b, cn_a, cn_b = _mixed_pool()

    for _ in range(5):
        assert auth_manager.pick_account(model="gpt-5.6-sol")["id"] in {intl_a, intl_b}
    for _ in range(5):
        assert auth_manager.pick_account(model="auto")["id"] in {cn_a, cn_b}
    # 两边都有的模型则都可以用
    for _ in range(5):
        assert auth_manager.pick_account(model="hy3")["id"] in {intl_a, intl_b, cn_a, cn_b}


def test_pick_account_without_model_considers_everyone():
    """没带模型信息时保持旧行为，不因能力过滤把账号排除掉。"""
    intl_a, intl_b, cn_a, cn_b = _mixed_pool()
    assert auth_manager.pick_account()["id"] in {intl_a, intl_b, cn_a, cn_b}


def test_unknown_capability_is_optimistic():
    """能力未知时先让它试（由上游 400 自愈），不要一上来就排除。"""
    aid = _make_account("fresh", "www.workbuddy.cn")
    assert auth_manager.account_supports_model(aid, "gpt-5.6-sol") is True


def test_denied_model_is_skipped_even_without_supplier_list():
    a = _make_account("a", "www.workbuddy.ai")
    b = _make_account("b", "www.workbuddy.ai")
    auth_manager.mark_model_denied(a, "m")
    assert auth_manager.account_supports_model(a, "m") is False
    assert auth_manager.pick_account(model="m")["id"] == b


def test_recording_supplier_models_prunes_stale_denials():
    aid = _make_account("a", "www.workbuddy.ai")
    auth_manager.mark_model_denied(aid, "gone")
    auth_manager.record_account_models(aid, ["kept"])
    # 新列表里没有的模型不可能再被选中，记录顺手清掉
    assert auth_manager.account_supports_model(aid, "gone") is False
    assert auth_manager.account_supports_model(aid, "kept") is True


def test_forget_account_clears_route_state():
    """账号删除后，能力/粘性/失败计数都不该留下残留。"""
    a = _make_account("a", "www.workbuddy.ai")
    b = _make_account("b", "www.workbuddy.ai")
    auth_manager.record_account_models(a, ["m"])
    auth_manager.mark_model_denied(a, "m")
    auth_manager._set_sticky_account(a, "workbuddy", "m")
    auth_manager.mark_account_failure(a, 401)

    db.delete_account(a)
    auth_manager.forget_account(a)

    assert a not in auth_manager._account_models
    assert a not in auth_manager._account_denied
    assert a not in auth_manager._sticky_account_id.values()
    assert auth_manager.auth_failure_count(a) == 0
    assert auth_manager.account_is_cooling_down(a) is False
    # 粘性槽已清空，选号重新开始 → 落到剩下的账号
    assert auth_manager.pick_account(model="m")["id"] == b


def test_capability_filter_falls_back_when_capable_accounts_exhausted():
    """能力过滤只调整优先级，不能让请求彻底无账号可用。

    上游的供应商列表双向不准（实测：国内站的列表里没有 kimi-k3，但它照样 200；
    反过来 glm-4.6 在列表里、所有账号却都回 service info not found）。如果过滤把候选
    清空就返回 None，就会出现「明明有账号，却报 No available accounts」。正确行为是：
    能服务的优先，都试过了就退回其余账号再试。
    """
    intl = _make_account("intl", "www.workbuddy.ai")
    cn = _make_account("cn", "www.workbuddy.cn")
    auth_manager.record_account_models(intl, ["m"])
    auth_manager.record_account_models(cn, ["other"])

    assert auth_manager.pick_account(model="m")["id"] == intl
    # 国际账号已被试过 → 退回国内账号，而不是返回 None
    assert auth_manager.pick_account(exclude_ids={intl}, model="m")["id"] == cn


# ============================================================
# 粘性：按模型分槽，且会为负载让位
# ============================================================

def test_sticky_slots_are_per_model():
    """不同模型各留各的粘性槽 —— 共用一个槽正是「只压一个账号」的来源。"""
    intl_a, intl_b, cn_a, cn_b = _mixed_pool()
    intl = auth_manager.pick_account(model="gpt-5.6-sol")["id"]
    cn = auth_manager.pick_account(model="auto")["id"]
    assert intl in {intl_a, intl_b}
    assert cn in {cn_a, cn_b}
    # 互相顶不掉：再问一次仍然各回各的
    assert auth_manager.pick_account(model="gpt-5.6-sol")["id"] == intl
    assert auth_manager.pick_account(model="auto")["id"] == cn


def test_sticky_holds_while_balanced():
    a = _make_account("a", "www.workbuddy.ai")
    b = _make_account("b", "www.workbuddy.ai")
    auth_manager.record_account_models(a, ["m"])
    auth_manager.record_account_models(b, ["m"])
    first = auth_manager.pick_account(model="m")["id"]
    _serve(first)
    # 只领先 1 个请求，还在容差内，继续粘住（保住 prompt cache）
    assert auth_manager.pick_account(model="m")["id"] == first


def test_sticky_yields_so_load_spreads():
    """粘住的账号明显更累时让位，负载不再长期压在同一个账号上。

    两个账号从同一水位出发：旧逻辑会一直粘住第一个选中的账号，20 次请求全落在它身上；
    修好之后应该在两者之间交替，偏差不超过容差（一个权重单位的两倍）。
    """
    a = _make_account("a", "www.workbuddy.ai")
    b = _make_account("b", "www.workbuddy.ai")
    auth_manager.record_account_models(a, ["m"])
    auth_manager.record_account_models(b, ["m"])

    picks = []
    for _ in range(20):
        chosen = auth_manager.pick_account(model="m")["id"]
        picks.append(chosen)
        _serve(chosen)

    assert set(picks) == {a, b}, "两个账号都该被用上"
    assert abs(picks.count(a) - picks.count(b)) <= 4, picks


def test_sticky_yields_to_idle_account_immediately():
    a = _make_account("a", "www.workbuddy.ai")
    b = _make_account("b", "www.workbuddy.ai")
    auth_manager.record_account_models(a, ["m"])
    auth_manager.record_account_models(b, ["m"])
    auth_manager._set_sticky_account(a, "workbuddy", "m")
    _serve(a, 500)
    assert auth_manager.pick_account(model="m")["id"] == b


def test_sticky_slack_is_constant_not_weight_scaled():
    """容差是常量，不按账号权重放大。

    否则高权重账号会被允许无限领先（权重 100 就容差 100 个负载单位），粘性会退化成
    「永远压在权重最大的那个账号上」。
    """
    light = _make_account("light", "www.workbuddy.ai")
    heavy = _make_account("heavy", "www.workbuddy.ai")
    db.update_account(light, {"weight": 100})
    db.update_account(heavy, {"weight": 200})
    auth_manager.record_account_models(light, ["m"])
    auth_manager.record_account_models(heavy, ["m"])
    _serve(light, 5000)   # 负载 50.0
    _serve(heavy, 4000)   # 负载 20.0
    auth_manager._set_sticky_account(light, "workbuddy", "m")
    # heavy 权重更高、负载更低，应该让位给它
    assert auth_manager.pick_account(model="m")["id"] == heavy


def test_lifetime_counters_do_not_affect_routing():
    """终身计数差很大也不能把请求锁死在同一个账号上（真实故障的回归测试）。

    线上实测两个国际账号终身计数 1110 / 754。旧逻辑拿它当负载信号，而它只增不减，
    差值远大于粘性容差 1.0，「少的先用」就退化成「永远只用计数较低的那个」——
    表现为「所有请求都固定路由到一个账号」。
    选路改用滑动窗口后，终身计数只用于展示，不再影响选择。
    """
    a = _make_account("a", "www.workbuddy.ai")
    b = _make_account("b", "www.workbuddy.ai")
    auth_manager.record_account_models(a, ["m"])
    auth_manager.record_account_models(b, ["m"])
    _set_requests(a, 1110)
    _set_requests(b, 754)

    picks = []
    for _ in range(20):
        chosen = auth_manager.pick_account(model="m")["id"]
        picks.append(chosen)
        _serve(chosen)

    assert set(picks) == {a, b}, f"终身计数不该决定路由，实际只用了 {set(picks)}"
    assert abs(picks.count(a) - picks.count(b)) <= 4, picks


def test_route_window_expires_so_old_load_is_forgiven():
    """窗口之外的旧负载不计入 —— 否则「窗口」只是换了个名字的终身计数。"""
    a = _make_account("a", "www.workbuddy.ai")
    b = _make_account("b", "www.workbuddy.ai")
    auth_manager.record_account_models(a, ["m"])
    auth_manager.record_account_models(b, ["m"])
    # a 曾经很忙，但都是很久以前的请求
    _serve(a, 500, age_seconds=db.ROUTE_WINDOW_SECONDS + 60)
    assert db.recent_account_loads().get(a, 0) == 0
    auth_manager._set_sticky_account(a, "workbuddy", "m")
    # 旧负载已出窗口，a 不再被判为更累
    assert auth_manager.pick_account(model="m")["id"] == a


def test_failed_requests_do_not_count_as_load():
    """只统计 2xx：失败/被拒的尝试没占用上游生成额度，不该让账号显得很忙。"""
    a = _make_account("a", "www.workbuddy.ai")
    conn = db.get_conn()
    for _ in range(50):
        conn.execute(
            "INSERT INTO logs (account_id, account_name, model, status_code, created_at, provider)"
            " VALUES (?,?,'m',400,?,'workbuddy')",
            (a, f"acc-{a}", int(time.time())),
        )
    conn.commit()
    conn.close()
    assert db.recent_account_loads().get(a, 0) == 0


# ============================================================
# 上游「模型不可用」的识别与换号
# ============================================================

@pytest.mark.parametrize(
    "detail",
    [
        {"code": 11102, "msg": "model [default-model] service info not found"},
        {"code": 11102, "msg": "model [gpt-5.6-sol] is only available for authorized users"},
        {"msg": "model [x] service info not found"},
        {"error": {"code": 11102, "message": "model [x] nope"}},
    ],
)
def test_model_unavailable_error_recognizes_upstream_rejections(detail):
    assert proxy._model_unavailable_error(400, detail) is True


@pytest.mark.parametrize(
    "status,detail",
    [
        (401, {"code": 11102, "msg": "model [x]"}),
        (500, {"code": 11102}),
        (400, {"code": 11155, "msg": "reasoning_content is required"}),
        (400, {"code": 11128, "msg": "first message is not system prompt"}),
        (400, b"not a dict"),
        (400, None),
    ],
)
def test_model_unavailable_error_ignores_other_failures(status, detail):
    assert proxy._model_unavailable_error(status, detail) is False


def test_model_blocked_400_switches_account_without_blaming_it(monkeypatch):
    """400「模型不属于这个账号」要换号，但不能把账号记成故障。"""
    blocked = _make_account("blocked", "www.workbuddy.cn")
    healthy = _make_account("healthy", "www.workbuddy.ai")
    _serve(healthy, 1)  # 让被拒的那个（负载 0）先被选中

    seen: list[int] = []

    async def fake_headers(account):
        return {"Authorization": "Bearer x"}

    async def fake_collect(url, headers, body, account, api_key_info, model_name, t0):
        seen.append(account["id"])
        if account["id"] == blocked:
            return ("error", (400, {"code": 11102, "msg": "model [m] service info not found"}))
        return ("json", {"id": "ok", "choices": [], "usage": {"total_tokens": 0}})

    async def no_delay(_attempt):
        return None

    monkeypatch.setattr(auth_manager, "get_valid_headers", fake_headers)
    monkeypatch.setattr(proxy, "_collect_stream", fake_collect)
    monkeypatch.setattr(proxy, "_retry_delay", no_delay)

    result = asyncio.run(proxy._json_chat_with_stall_retry({"model": "m"}, None, "m"))

    assert seen == [blocked, healthy]
    assert result[0] == "json"
    # 账号本身没坏：不该进冷却，也不该被判成鉴权失败
    assert auth_manager.account_is_cooling_down(blocked) is False
    assert auth_manager.auth_failure_count(blocked) == 0
    assert db.get_account(blocked)["status"] == "active"
    # 但已经学到「这个账号服务不了这个模型」，下次直接跳过
    assert auth_manager.account_supports_model(blocked, "m") is False


def test_pick_account_skips_account_denied_earlier():
    """被拒过的账号在后续请求里直接不再被选中。"""
    blocked = _make_account("blocked", "www.workbuddy.cn")
    healthy = _make_account("healthy", "www.workbuddy.cn")
    auth_manager.mark_model_denied(blocked, "m")
    for _ in range(5):
        assert auth_manager.pick_account(model="m")["id"] == healthy


# ============================================================
# 模型目录：多账号并集
# ============================================================

def test_catalog_samples_every_account_and_records_capability(monkeypatch):
    """目录取全部账号的并集，并把每个账号的能力写回路由层。"""
    intl = _make_account("intl", "www.workbuddy.ai")
    cn = _make_account("cn", "www.workbuddy.cn")

    async def fake_fetch(account):
        if str(account["domain"]).endswith(".cn"):
            return [{"id": "auto", "name": "Auto"}, {"id": "deepseek-v4.1-flash", "name": "DS"}]
        return [{"id": "default-model", "name": "Auto"}, {"id": "gpt-5.6-sol", "name": "Sol"}]

    monkeypatch.setitem(catalog.LIVE_FETCHERS, "workbuddy", fake_fetch)

    result = asyncio.run(catalog.refresh_one("workbuddy"))
    assert result["mode"] == "live"
    ids = {item["id"] for item in result["models"]}
    assert {"auto", "deepseek-v4.1-flash", "default-model", "gpt-5.6-sol"} <= ids

    assert auth_manager.account_supports_model(intl, "gpt-5.6-sol") is True
    assert auth_manager.account_supports_model(intl, "auto") is False
    assert auth_manager.account_supports_model(cn, "auto") is True
    assert auth_manager.account_supports_model(cn, "gpt-5.6-sol") is False


def test_catalog_ignores_disabled_accounts(monkeypatch):
    """停用的账号不该被采样，否则会把它的模型混进目录。"""
    active = _make_account("active", "www.workbuddy.ai")
    _make_account("off", "www.workbuddy.cn", status="inactive")

    async def fake_fetch(account):
        assert account["id"] == active
        return [{"id": "only-active", "name": "Only"}]

    monkeypatch.setitem(catalog.LIVE_FETCHERS, "workbuddy", fake_fetch)
    result = asyncio.run(catalog.refresh_one("workbuddy"))
    assert "only-active" in {item["id"] for item in result["models"]}


# ============================================================
# 站点分组与站点偏好
# ============================================================

def test_site_group_splits_domestic_from_international():
    """同一模型两边计费不同，所以「这个账号要不要花钱」只能按域名后缀判定。"""
    for domain in ("www.workbuddy.cn", "www.codebuddy.cn", "WWW.WorkBuddy.CN/"):
        assert sites.site_group(domain) == sites.SITE_DOMESTIC
    for domain in ("www.workbuddy.ai", "www.codebuddy.ai", "copilot.tencent.com"):
        assert sites.site_group(domain) == sites.SITE_INTERNATIONAL
    # domain 为空时与 backend_url_for 的回退方向一致（走全局上游 = 国际站）
    assert sites.site_group("") == sites.SITE_INTERNATIONAL
    assert sites.site_group(None) == sites.SITE_INTERNATIONAL


def test_site_preference_defaults_to_auto_and_is_neutral_without_data():
    """默认 auto：从实测计费学「哪边免费/更便宜」。

    关键是「没数据时不偏向任何一边」——auto 在没有计费样本时必须退回不区分站点，
    否则引入这个默认值就等于把某一边的账号静默降级了。
    """
    assert auth_manager.model_site_preference() == {"default": "auto", "models": {}}
    assert auth_manager.preferred_site_for("deepseek-v4.1-flash") == ""
    assert auth_manager.preferred_site_for(None) == ""


def test_auto_prefers_the_free_site_for_that_model():
    """实测数据说国际站免费、国内站扣费 → auto 优先国际站。

    这是本次修复的核心诉求：免费模型不该被路由到收费的账号上。
    """
    intl = _make_account("intl", "www.workbuddy.ai")
    cn = _make_account("cn", "www.workbuddy.cn")
    auth_manager.record_account_models(intl, ["m"])
    auth_manager.record_account_models(cn, ["m"])
    _serve(cn, 10, model="m", credit=0.34)   # 国内站：每次都扣费
    _serve(intl, 10, model="m", credit=0.0)  # 国际站：完全免费
    auth_manager.forget_cost_profile()

    assert auth_manager.preferred_site_for("m") == sites.SITE_INTERNATIONAL
    picks = [
        auth_manager.pick_account(model="m")["id"]
        for _ in range(10)
    ]
    assert set(picks) == {intl}, "免费模型不该落到收费账号上"


def test_auto_prefers_cheaper_site_when_both_charge():
    """两边都收费时选单价低的（你说的「先路由收费低的」）。"""
    intl = _make_account("intl", "www.workbuddy.ai")
    cn = _make_account("cn", "www.workbuddy.cn")
    auth_manager.record_account_models(intl, ["m"])
    auth_manager.record_account_models(cn, ["m"])
    _serve(cn, 10, model="m", credit=1.0)   # 均价 1.0
    _serve(intl, 10, model="m", credit=0.1)  # 均价 0.1
    auth_manager.forget_cost_profile()
    assert auth_manager.preferred_site_for("m") == sites.SITE_INTERNATIONAL


def test_auto_stays_neutral_when_costs_are_equal():
    """收费一样就不区分站点，否则白白损失负载均衡。"""
    intl = _make_account("intl", "www.workbuddy.ai")
    cn = _make_account("cn", "www.workbuddy.cn")
    auth_manager.record_account_models(intl, ["m"])
    auth_manager.record_account_models(cn, ["m"])
    _serve(cn, 10, model="m", credit=0.5)
    _serve(intl, 10, model="m", credit=0.5)
    auth_manager.forget_cost_profile()
    assert auth_manager.preferred_site_for("m") == ""


def test_auto_waits_for_enough_samples():
    """样本太少不下结论：宁可先不区分站点，也不要凭一两次请求误判计费。"""
    intl = _make_account("intl", "www.workbuddy.ai")
    cn = _make_account("cn", "www.workbuddy.cn")
    _serve(cn, 2, model="m", credit=0.9)
    _serve(intl, 2, model="m", credit=0.0)
    auth_manager.forget_cost_profile()
    assert auth_manager.preferred_site_for("m") == ""


def test_auto_ignores_failed_requests_when_learning_costs():
    """学计费只看成功的请求：失败的尝试没有扣费记录，混进来会把画像带偏。"""
    intl = _make_account("intl", "www.workbuddy.ai")
    cn = _make_account("cn", "www.workbuddy.cn")
    conn = db.get_conn()
    for _ in range(20):
        conn.execute(
            "INSERT INTO logs (account_id, account_name, model, status_code, credit,"
            " created_at, provider) VALUES (?,?,'m',500,0.0,?,'workbuddy')",
            (cn, f"acc-{cn}", int(time.time())),
        )
    conn.commit()
    conn.close()
    _serve(cn, 10, model="m", credit=0.5)
    _serve(intl, 10, model="m", credit=0.5)
    auth_manager.forget_cost_profile()
    # 500 的 20 条不计入，两边各 10 条且均价相同 → 不区分
    assert auth_manager.preferred_site_for("m") == ""


def test_site_preference_falls_back_to_default_for_unlisted_model():
    db.set_setting(
        "model_site_preference",
        {"default": "international", "models": {"deepseek-v4.1-flash": "domestic"}},
    )
    assert auth_manager.preferred_site_for("deepseek-v4.1-flash") == "domestic"
    assert auth_manager.preferred_site_for("gpt-5.6-sol") == "international"


def test_site_preference_survives_broken_setting():
    """配置写坏时用默认值，不能让路由整个挂掉。"""
    db.set_setting("model_site_preference", "not-a-dict")
    assert auth_manager.model_site_preference() == {"default": "auto", "models": {}}
    # 字段存在但取值非法 → 退回「不区分」（与「设置不存在 → auto」不同，但同样安全）
    db.set_setting("model_site_preference", {"default": "nonsense", "models": {"m": 7}})
    assert auth_manager.model_site_preference() == {"default": "", "models": {"m": ""}}


def test_site_preference_overrides_load_balancing_for_that_model():
    """偏好只调优先级：不花钱那边即使更累，也要先被选中。"""
    intl = _make_account("intl", "www.workbuddy.ai", status="active")
    cn = _make_account("cn", "www.workbuddy.cn", status="active")
    auth_manager.record_account_models(intl, ["m"])
    auth_manager.record_account_models(cn, ["m"])
    _serve(intl, 999)   # 国际账号很累，但配置要求优先用它
    db.set_setting("model_site_preference", {"default": "", "models": {"m": "international"}})
    assert auth_manager.pick_account(model="m")["id"] == intl


def test_site_preference_falls_back_when_preferred_site_is_exhausted():
    """偏好那边的账号都被试过 → 退回另一边，不能报 No available accounts。"""
    intl = _make_account("intl", "www.workbuddy.ai", status="active")
    cn = _make_account("cn", "www.workbuddy.cn", status="active")
    db.set_setting("model_site_preference", {"default": "international", "models": {}})
    assert auth_manager.pick_account(model="m")["id"] == intl
    assert auth_manager.pick_account(exclude_ids={intl}, model="m")["id"] == cn


def test_site_preference_falls_back_when_preferred_site_has_no_account():
    """偏好站点一个账号都没导入时，照样用另一边的账号。"""
    cn = _make_account("cn", "www.workbuddy.cn", status="active")
    db.set_setting("model_site_preference", {"default": "international", "models": {}})
    assert auth_manager.pick_account(model="m")["id"] == cn


def test_site_preference_ignores_cooling_down_accounts():
    """偏好不是绕过冷却的理由：冷却中的账号不能被粘性重新捡回来。"""
    intl = _make_account("intl", "www.workbuddy.ai", status="active")
    cn = _make_account("cn", "www.workbuddy.cn", status="active")
    db.set_setting("model_site_preference", {"default": "international", "models": {}})
    auth_manager._set_sticky_account(intl, "workbuddy", "m")
    auth_manager.mark_account_failure(intl, 429)
    assert auth_manager.pick_account(model="m")["id"] == cn


def test_site_preference_clean_rejects_bad_values():
    """写入侧是严格的：管理页/API 传错要明确报错，不能静默存成部分配置。"""
    assert site_preference.clean({}) == {"default": "", "models": {}}
    assert site_preference.clean(
        {"default": "domestic", "models": {"m": "international"}}
    ) == {"default": "domestic", "models": {"m": "international"}}

    with pytest.raises(site_preference.SitePreferenceError, match="must be one of"):
        site_preference.clean({"default": "cn"})
    with pytest.raises(site_preference.SitePreferenceError, match="models must be an object"):
        site_preference.clean({"models": ["international"]})
    with pytest.raises(site_preference.SitePreferenceError, match="unsupported keys"):
        site_preference.clean({"sites": {}})
    with pytest.raises(site_preference.SitePreferenceError, match="model id is required"):
        site_preference.clean({"models": {"": "international"}})


def test_reset_request_counts_levels_the_field():
    """归零只是清掉展示用的累计统计；选路看窗口，不靠它。"""
    a = _make_account("a", "www.workbuddy.ai", status="active")
    b = _make_account("b", "www.workbuddy.cn", status="active")
    _set_requests(a, 1110)
    _set_requests(b, 97)

    assert db.reset_account_request_counts() == 2
    assert db.get_account(a)["total_requests"] == 0
    assert db.get_account(b)["total_requests"] == 0

    # 即使不归零，窗口选路也会把负载摊到两个账号上（见
    # test_lifetime_counters_do_not_affect_routing）；归零后同样如此。
    picks = []
    for _ in range(10):
        chosen = auth_manager.pick_account(model="m")["id"]
        picks.append(chosen)
        _serve(chosen)
    assert set(picks) == {a, b}, picks


# ============================================================
# 按模型限流（429 / code 6004）不得把健康账号挤出重试窗口
#
# 实测背景（2026-09-20）：4 个老账号在 deepseek-v4.1-flash 上被上游限流
# （`{"code":6004,"msg":"usage exceeds frequency limit ... 您也可以切换其他模型
# 继续使用"}`，重置窗口 1~6 小时），另加了 2 个健康账号，但客户端仍然请求不了
# deepseek —— 请求每次都在 3 次重试里撞限额账号，健康账号从未被选中。
#
# 两个成因，两条回归：
#   1. 429 与其它失败共用 30s×2ⁿ、封顶 300s 的冷却，冷却一过，被限流的账号就以
#      「零负载」身份回到候选池最前（负载只统计 2xx），再次霸占重试额度；
#   2. 重试上限硬编码 3 次，账号多于 3 个时健康账号根本没有出场机会。
# ============================================================

def test_rate_limited_account_is_skipped_for_that_model():
    """被限流的账号必须从本模型的候选里去掉，而不是只降优先级。

    只降优先级不够：负载排序看 2xx，限额账号的负载是 0（它一直失败、从没服务成功），
    在排序上反而「最空闲」，每次重试都会被优先选中。
    """
    limited = _make_account("limited", "www.workbuddy.cn")
    healthy = _make_account("healthy", "www.workbuddy.cn")

    auth_manager.mark_account_failure(limited, 429, "deepseek-v4.1-flash")

    for _ in range(6):
        assert auth_manager.pick_account(model="deepseek-v4.1-flash")["id"] == healthy


def test_rate_limit_is_per_model_not_per_account():
    """限流只锁「该账号 + 该模型」：该账号在其它模型上必须照常可用。

    回归点：按账号整体冷却会把账号在其它模型上的正常服务一起堵掉。实测数据支持
    按模型限流 —— 账号在 deepseek 上吃 429 后十几分钟内仍在正常服务 glm-5.3-flash，
    上游报文也明确写「您也可以切换其他模型继续使用」。
    """
    a = _make_account("a", "www.workbuddy.cn")

    auth_manager.mark_account_failure(a, 429, "deepseek-v4.1-flash")

    assert auth_manager.account_model_rate_limited(a, "deepseek-v4.1-flash") is True
    assert auth_manager.account_model_rate_limited(a, "glm-5.3-flash") is False
    assert auth_manager.pick_account(model="glm-5.3-flash")["id"] == a
    # 同一账号在别的模型上仍然「不冷却」—— 账号级冷却没有被触发
    assert auth_manager.account_is_cooling_down(a) is False


def test_rate_limit_cooldown_outlasts_plain_backoff():
    """限流冷却必须长于通用退避（30s/60s/.../300s），否则冷却形同虚设。

    上游给的重置窗口是分钟到小时级（实测「将在 18:26 重置」距报错 1h44m）。
    """
    a = _make_account("a", "www.workbuddy.cn")
    auth_manager.mark_account_failure(a, 429, "m")
    expires = auth_manager._model_rate_limits[(a, "m")]
    remaining = expires - time.monotonic()
    assert remaining >= auth_manager.RATE_LIMIT_COOLDOWN_SECONDS - 1
    assert auth_manager.RATE_LIMIT_COOLDOWN_SECONDS > 300, "冷却不能短于通用退避上限"


def test_success_clears_the_model_rate_limit():
    """模型上真的成功了就立刻解除冷却，不等计时器走完。"""
    a = _make_account("a", "www.workbuddy.cn")
    auth_manager.mark_account_failure(a, 429, "m")
    assert auth_manager.account_model_rate_limited(a, "m") is True

    auth_manager.mark_account_success(a, "m")

    assert auth_manager.account_model_rate_limited(a, "m") is False


def test_all_candidates_limited_still_returns_an_account():
    """全部候选都在限流时仍要返回账号，不能报 No available accounts。

    宁可让上游再判一次（说不定额度已恢复），也不能让一个「明明有账号」的请求
    直接失败。
    """
    a = _make_account("a", "www.workbuddy.cn")
    b = _make_account("b", "www.workbuddy.cn")
    for aid in (a, b):
        auth_manager.mark_account_failure(aid, 429, "m")

    chosen = auth_manager.pick_account(model="m")
    assert chosen is not None and chosen["id"] in {a, b}


def test_rate_limit_does_not_exclude_accounts_in_expired_refresh_fallback():
    """限流账号也不走「过期账号刷新」回退路径（否则限流立刻被绕过）。"""
    limited = _make_account("limited", "www.workbuddy.cn", status="expired")
    auth_manager.mark_account_failure(limited, 429, "m")

    assert auth_manager.pick_account(model="m") is None


def test_pinned_account_reports_unavailable_while_rate_limited():
    """绑定账号在限流中时返回 None：绑定语义是「只用这个账号」，不是反复撞墙。"""
    a = _make_account("a", "www.workbuddy.cn")
    auth_manager.set_pinned_account(a)
    try:
        assert auth_manager.pick_account(model="m") is not None
        auth_manager.mark_account_failure(a, 429, "m")
        assert auth_manager.pick_account(model="m") is None
    finally:
        auth_manager.set_pinned_account(0)


def test_retry_budget_exceeds_account_pool(monkeypatch):
    """换号重试的账号数上限必须大于账号池规模。

    这是本次事故的直接原因：6 个账号、重试上限 3，前 3 次全撞在限额账号上，
    健康账号从头到尾没被试过。这里用真实选路跑一遍，断言最终落到健康账号。
    """
    limited = [_make_account(f"limited-{i}", "www.workbuddy.cn") for i in range(4)]
    healthy = _make_account("healthy", "www.workbuddy.cn")
    for aid in limited:
        auth_manager.mark_account_failure(aid, 429, "m")

    tried: set[int] = set()
    attempts = 0
    while attempts < auth_manager.MAX_ACCOUNT_ATTEMPTS:
        account = auth_manager.pick_account(tried, model="m")
        if not account:
            break
        tried.add(account["id"])
        attempts += 1
        if account["id"] == healthy:
            break

    assert healthy in tried, f"健康账号必须有机会出场，实际试过 {tried}"
    assert auth_manager.MAX_ACCOUNT_ATTEMPTS > len(limited), "上限必须大于账号池规模"


def test_rate_limit_state_is_swept_when_account_is_deleted():
    """账号删除后清掉它的限流状态，别留成内存泄漏。"""
    a = _make_account("a", "www.workbuddy.cn")
    auth_manager.mark_account_failure(a, 429, "m")
    assert (a, "m") in auth_manager._model_rate_limits

    db.delete_account(a)
    auth_manager.forget_account(a)

    assert (a, "m") not in auth_manager._model_rate_limits
