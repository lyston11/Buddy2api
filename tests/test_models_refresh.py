import asyncio
import json
import os
from pathlib import Path

import pytest

import buddy2api.credential_crypto as credential_crypto
import buddy2api.database as db
import buddy2api.providers as providers
import buddy2api.router as router
import buddy2api.server as server
from buddy2api.providers.protocol import KeyChannelMismatch, UnknownModel
from buddy2api.providers.qclaw.constants import STATIC_MODELS as QCLAW_STATIC
from buddy2api.providers.qwenwork.constants import STATIC_MODELS as QWEN_STATIC
from buddy2api.providers.traework.constants import STATIC_MODELS as TRAE_STATIC

QCLAW_NEW_ID = "qclaw-live-only-model"
QWEN_NEW_ID = "qwen-live-only-model"
WB_NEW_ID = "wb-live-only-model"
TRAE_NEW_DOUBAO = "Doubao-Seed-2.2-Pro"
TRAE_DOUBAO_TURBO = "Doubao-Seed-2.1-Turbo"
TRAE_DOUBAO_CODE = "Doubao-Seed-2.0-Code"


@pytest.mark.parametrize("channel", ["workbuddy", "qclaw", "qwenwork", "traework"])
def test_capacity_survives_supplier_catalog_and_public_listing(channel, isolated_db, all_channels):
    import buddy2api.catalog as catalog
    from buddy2api.providers.workbuddy.models import parse_supplier_models as wb
    from buddy2api.providers.qclaw.jprx import parse_model_list as qc
    from buddy2api.providers.qwenwork.models import parse_supplier_models as qw
    from buddy2api.providers.traework.models import parse_supplier_models as tw

    row = {"id": "capacity-test", "maxInputTokens": 1000000, "maxOutputTokens": 128000,
           "contextWindow": {"defaultLength": 300000, "supportedLengths": [300000, 1000000]}}
    parsed = {"workbuddy": wb, "qclaw": qc, "qwenwork": qw, "traework": tw}[channel]([row])
    normalized = catalog.normalize_models(parsed)
    catalog.save_catalog(channel, normalized)
    public_id = "capacity-test" if channel == "workbuddy" else f"{channel}/capacity-test"
    item = next(item for item in server.collect_v1_models() if item["id"] == public_id)
    assert item["context_window"] == 1000000
    assert item["max_output_tokens"] == 128000
    assert item["capacity_source"] == {"context_window": "catalog", "max_output_tokens": "catalog"}
    if channel == "workbuddy":
        prefixed = next(item for item in server.collect_v1_models() if item["id"] == f"workbuddy/{public_id}")
        assert prefixed["context_window"] == item["context_window"]


def test_all_channels_publish_capacity_defaults_without_persisting_guesses(isolated_db, all_channels):
    import buddy2api.catalog as catalog

    for channel in ("workbuddy", "qclaw", "qwenwork", "traework"):
        catalog.save_catalog(channel, [{"id": "unknown"}])
    items = server.collect_v1_models()
    assert len(items) == 5
    for item in items:
        assert item["context_window"] == 262144
        assert item["max_output_tokens"] == 32768
        assert set(item["capacity_source"].values()) == {"fallback"}
        assert "reasoning" not in item
        assert "supportsReasoning" not in item
        assert catalog.stored_catalog(item["channel"]) == [{"id": "unknown"}]


def test_workbuddy_reasoning_survives_supplier_catalog_and_public_listing(isolated_db, all_channels):
    import buddy2api.catalog as catalog
    from buddy2api.providers.workbuddy.models import parse_supplier_models

    payload = {
        "data": {
            "models": [
                {
                    "id": "glm-5.3",
                    "name": "GLM-5.3",
                    "supportsReasoning": True,
                    "reasoning": {
                        "supportedEfforts": ["low", "high", "max", "max", ""],
                        "defaultEffort": "high",
                        "canDisableThinking": True,
                    },
                },
                {
                    "id": "deepseek-v4-pro",
                    "supportsReasoning": True,
                    "reasoning": {"effort": "high", "summary": "auto"},
                },
                {
                    "id": "hy4-preview",
                    "supportsReasoning": True,
                    "reasoning": {"supportedEfforts": ["high"], "defaultEffort": "high"},
                },
            ]
        }
    }
    parsed = parse_supplier_models(payload)
    by_id = {item["id"]: item for item in parsed}
    assert by_id["glm-5.3"]["reasoning"]["supportedEfforts"] == ["low", "high", "max"]
    assert by_id["glm-5.3"]["reasoning"]["defaultEffort"] == "high"
    assert by_id["glm-5.3"]["supportsReasoning"] is True
    assert "supportedEfforts" not in by_id["deepseek-v4-pro"]["reasoning"]
    assert by_id["deepseek-v4-pro"]["reasoning"]["defaultEffort"] == "high"

    catalog.save_catalog("workbuddy", catalog.normalize_models(parsed))
    public = {item["id"]: item for item in server.collect_v1_models()}
    listed = public["glm-5.3"]
    prefixed = public["workbuddy/glm-5.3"]
    assert listed["reasoning"]["supportedEfforts"] == ["low", "high", "max"]
    assert listed["reasoning"]["defaultEffort"] == "high"
    assert listed["reasoning"]["canDisableThinking"] is True
    assert listed["supportsReasoning"] is True
    assert prefixed["reasoning"] == listed["reasoning"]
    assert public["deepseek-v4-pro"]["reasoning"] == {"defaultEffort": "high"}
    assert public["hy4-preview"]["reasoning"]["supportedEfforts"] == ["high"]
    stored = catalog.stored_catalog("workbuddy")
    assert stored is not None
    assert "summary" not in stored[1].get("reasoning", {})


def test_supplier_description_keeps_zh_version_note(isolated_db, all_channels):
    """上游把官方说明放在 descriptionZh/descriptionEn 里，只认 description 会丢版本号。

    实测背景（2026-10 国内站目录）：deepseek-v4.1-flash 的 descriptionZh 是
    「已更新至 V4.1-Flash-1017最新版，支持1M上下文窗口，原生多模态」——上游没有
    单独的 version 字段、响应体里的 model 也只有裸 id，说明文本是唯一能看出
    「当前是 1017 版」的地方，丢了用户就只能靠行为探测猜。
    """
    import buddy2api.catalog as catalog
    from buddy2api.providers.workbuddy.models import parse_supplier_models

    payload = {
        "data": {
            "models": [
                {
                    "id": "deepseek-v4.1-flash",
                    "name": "Deepseek-V4.1-Flash",
                    "descriptionZh": "已更新至 V4.1-Flash-1017最新版，支持1M上下文窗口，原生多模态",
                    "descriptionEn": "DeepSeek flagship model, native multimodal model",
                },
                {"id": "glm-5.3", "descriptionEn": "English only"},
                {"id": "hy3", "description": "plain description"},
                {"id": "kimi-k3", "descriptionZh": "   "},
            ]
        }
    }
    parsed = {item["id"]: item for item in parse_supplier_models(payload)}
    assert parsed["deepseek-v4.1-flash"]["description"].startswith("已更新至 V4.1-Flash-1017")
    # 中文缺失才退到英文，别为了「有值」把中文覆盖成英文。
    assert parsed["glm-5.3"]["description"] == "English only"
    assert parsed["hy3"]["description"] == "plain description"
    assert "description" not in parsed["kimi-k3"]

    # 目录页读的是 catalog_snapshot，落库后说明不能丢。
    catalog.save_catalog("workbuddy", catalog.normalize_models(list(parsed.values())))
    source = next(
        item for item in catalog.catalog_snapshot()["sources"] if item["channel"] == "workbuddy"
    )
    row = next(item for item in source["models"] if item["id"] == "deepseek-v4.1-flash")
    assert "1017" in row["description"]


def test_empty_or_invalid_reasoning_is_not_published():
    from buddy2api.model_reasoning import reasoning_fields

    assert reasoning_fields({"id": "x", "reasoning": {"supportedEfforts": []}}) == {}
    assert reasoning_fields({"id": "x", "supportedEfforts": ["low", 1, None]}) == {
        "reasoning": {"supportedEfforts": ["low"]}
    }
    assert reasoning_fields({"id": "x", "supportsReasoning": "true"}) == {}
    assert reasoning_fields(["not-a-row"]) == {}


def test_other_channels_do_not_invent_reasoning_from_thinking_tags(isolated_db, all_channels):
    import buddy2api.catalog as catalog
    from buddy2api.providers.qclaw.jprx import parse_model_list
    from buddy2api.providers.qwenwork.models import parse_supplier_models as parse_qwen
    from buddy2api.providers.traework.models import parse_supplier_models as parse_trae

    catalog.save_catalog("qclaw", catalog.normalize_models(parse_model_list({
        "model_status_list": [{"id": "pool-hy3-preview", "capabilities": ["深度思考"]}],
    })))
    catalog.save_catalog("qwenwork", catalog.normalize_models(parse_qwen({
        "qwork": [{"key": "pro", "enable": True, "is_reasoning": False}],
    })))
    catalog.save_catalog("traework", catalog.normalize_models(parse_trae({
        "data": {"list": [{"models": [{
            "name": "glm-5.3",
            "reasoning_effort_config": {"support_thinking": False, "options": None},
        }]}]},
    })))
    public = {item["id"]: item for item in server.collect_v1_models()}
    for model_id in ("qclaw/pool-hy3-preview", "qwenwork/pro", "traework/glm-5.3"):
        assert "reasoning" not in public[model_id]
        assert "supportsReasoning" not in public[model_id]


@pytest.mark.parametrize("bad", [True, False, 0, -1, 1.5, "1000000", None, {}])
def test_invalid_capacity_values_fall_back(bad):
    from buddy2api.model_capacity import discovery_capacity

    item = discovery_capacity({"maxInputTokens": bad, "maxOutputTokens": bad})
    assert item["context_window"] == 262144
    assert item["max_output_tokens"] == 32768


def test_string_models_and_partial_capacity_remain_supported():
    import buddy2api.catalog as catalog
    from buddy2api.model_capacity import discovery_capacity
    from buddy2api.providers.qclaw.jprx import parse_model_list
    from buddy2api.providers.traework.models import parse_supplier_models

    assert catalog.normalize_models(["legacy"]) == [{"id": "legacy", "name": "legacy"}]
    assert parse_model_list(["legacy"])[0]["id"] == "legacy"
    assert parse_supplier_models(["legacy"])[0]["id"] == "legacy"
    item = discovery_capacity({"max_output_tokens": 64000})
    assert item["context_window"] == 262144
    assert item["max_output_tokens"] == 64000
    assert item["capacity_source"] == {"context_window": "fallback", "max_output_tokens": "catalog"}


@pytest.mark.parametrize("field", ["max_tokens", "max_completion_tokens"])
def test_explicit_output_budget_is_clamped_only_to_known_capacity(field):
    from buddy2api.model_capacity import clamp_output_tokens

    payload = {field: 128000}
    assert clamp_output_tokens(payload, {"max_output_tokens": 64000})[field] == 64000
    assert payload[field] == 128000
    assert clamp_output_tokens(payload, {})[field] == 128000
    assert clamp_output_tokens({field: 8000}, {"max_output_tokens": 64000})[field] == 8000
    assert clamp_output_tokens({}, {"max_output_tokens": 64000}) == {}


@pytest.mark.parametrize("responses_api", [False, True])
def test_dispatch_clamps_capacity_after_alias_resolution(monkeypatch, responses_api):
    from buddy2api.providers.protocol import BindResult

    class Provider:
        def translate_model(self, model):
            assert model == "alias"
            return "real-model"

        def list_models(self):
            return [{"id": "real-model", "max_output_tokens": 64000}]

        async def chat_completions(self, payload, info):
            assert payload["model"] == "real-model"
            assert payload["max_tokens"] == 64000
            return ("json", {"choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]})

    monkeypatch.setattr(providers, "get_provider", lambda channel: Provider())
    bound = BindResult(channel="workbuddy", inner="alias", original="workbuddy/alias")
    if responses_api:
        result = asyncio.run(router.responses_after_bind(bound, {"input": "hello", "max_output_tokens": 128000}, None))
    else:
        result = asyncio.run(router.chat_after_bind(bound, {"messages": [], "max_tokens": 128000}, None))
    assert result[0] == "json"

QCLAW_HTTP_PAYLOAD = {
    "ret": 0,
    "data": {
        "resp": {
            "common": {"code": 0},
            "data": {
                "model_status_list": [
                    {"id": "default", "name": "Default"},
                    {"id": "pool-glm-5.2", "name": "GLM 5.2"},
                    {"id": QCLAW_NEW_ID, "name": "QClaw Live Only"},
                ]
            },
        }
    },
}

QWENWORK_HTTP_PAYLOAD = {
    "qwork": [
        {"key": "pro", "display_name": "高级", "enable": True},
        {"key": QWEN_NEW_ID, "display_name": "Qwen Live Only", "enable": True},
        {"key": "disabled-model", "display_name": "Hidden", "enable": False},
    ]
}

WORKBUDDY_HTTP_PAYLOAD = {
    "code": 0,
    "msg": "ok",
    "data": {
        "models": [
            {"id": "auto", "name": "Auto", "tags": ["craft"], "credits": "x0.79 credits"},
            {"id": "glm-5.2", "name": "GLM-5.2", "tags": ["craft"], "credits": "x3.31 credits"},
            {"id": WB_NEW_ID, "name": "WB Live Only", "tags": [], "credits": "x0.00"},
            {"id": "hunyuan-image-v3.0", "name": "Hunyuan Image V3", "tags": ["text-to-image"], "credits": "x1.0"},
            {"id": 123, "name": "numeric-id-skipped"},
        ]
    },
}

TRAEWORK_HTTP_PAYLOAD = {
    "code": 0,
    "message": "success",
    "data": {
        "list": [
            {
                "function": "solo_work_remote",
                "models": [
                    {"name": TRAE_DOUBAO_CODE, "display_name": TRAE_DOUBAO_CODE},
                    {"name": TRAE_DOUBAO_TURBO, "display_name": TRAE_DOUBAO_TURBO},
                    {"name": TRAE_NEW_DOUBAO, "display_name": TRAE_NEW_DOUBAO},
                    {"name": "qwen-3.6-plus", "display_name": "qwen-3.6-plus"},
                ],
            }
        ]
    },
}


class _FakeResponse:
    def __init__(self, payload, status=200):
        self.status_code = status
        self._payload = payload
        self.headers = {}
        self.content = b"{}"

    def json(self):
        return self._payload


def _ids(models):
    return {str(item.get("id") if isinstance(item, dict) else item) for item in models}


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
def all_channels(monkeypatch):
    monkeypatch.setenv("CB_GATEWAY_PROVIDERS", "workbuddy,qclaw,qwenwork,traework")
    yield
    monkeypatch.delenv("CB_GATEWAY_PROVIDERS", raising=False)


def _seed_live_accounts():
    db.add_account(
        {
            "name": "wb",
            "uid": "wb-1",
            "provider": "workbuddy",
            "status": "active",
            "access_token": "tok-wb",
            "expires_at": 9_999_999_999_999,
        }
    )
    db.add_account(
        {
            "name": "qc",
            "uid": "qc-1",
            "provider": "qclaw",
            "status": "active",
            "access_token": "sk-qclaw",
            "refresh_token": "jwt-qclaw",
            "extra": {"guid": "guid-1"},
        }
    )
    db.add_account(
        {
            "name": "tw",
            "uid": "tw-1",
            "provider": "traework",
            "status": "active",
            "access_token": "tok-trae",
            "expires_at": 9_999_999_999_999,
            "extra": {"device_id": "dev-1"},
        }
    )
    db.add_account(
        {
            "name": "qw",
            "uid": "qw-1",
            "provider": "qwenwork",
            "status": "active",
            "access_token": "tok-qwen",
            "extra": {"login_device_id": "dev-qw", "email": "a@b"},
        }
    )


def _install_supplier_http(monkeypatch):
    requested = []

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, **kwargs):
            requested.append(("POST", str(url)))
            if "/data/4320/forward" in str(url):
                return _FakeResponse(QCLAW_HTTP_PAYLOAD)
            raise AssertionError(f"unexpected POST {url}")

        async def get(self, url, **kwargs):
            requested.append(("GET", str(url)))
            if "/api/remote/v1/models" in str(url):
                assert kwargs.get("params") == {
                    "functions": "solo_work_remote",
                    "show_custom_model": "true",
                }
                return _FakeResponse(TRAEWORK_HTTP_PAYLOAD)
            if "/api/v2/model/list" in str(url):
                return _FakeResponse(QWENWORK_HTTP_PAYLOAD)
            if "/v2/enterprises/personal/models" in str(url):
                return _FakeResponse(WORKBUDDY_HTTP_PAYLOAD)
            raise AssertionError(f"unexpected GET {url}")

    monkeypatch.setattr("buddy2api.providers.qclaw.jprx.httpx.AsyncClient", FakeAsyncClient)
    monkeypatch.setattr("buddy2api.providers.traework.models.httpx.AsyncClient", FakeAsyncClient)
    monkeypatch.setattr("buddy2api.providers.qwenwork.models.httpx.AsyncClient", FakeAsyncClient)
    monkeypatch.setattr("buddy2api.providers.workbuddy.models.httpx.AsyncClient", FakeAsyncClient)
    return requested


def _by_channel(result):
    return {item["channel"]: item for item in result["sources"]}


def test_web_and_cli_version_come_from_version_py():
    from buddy2api.version import VERSION

    html = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(encoding="utf-8")
    assert "__APP_VERSION__" in html
    assert "v2.1." not in html
    rendered = server._render_index_html()
    assert f"Local model gateway · v{VERSION}" in rendered
    assert "__APP_VERSION__" not in rendered
    assert "Buddy 2 API v{VERSION}" in Path(server.__file__).read_text(encoding="utf-8")


def test_admin_models_page_has_one_click_control():
    html = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(encoding="utf-8")
    assert "一键读取供应模型" in html
    assert "/admin/models/refresh" in html
    assert "syncSources" in html
    assert "请选择通道" in html
    assert "addForm.channel" in html
    assert "/admin/models/catalogs" in html
    assert "submitAdd" in html
    assert "rmExtra" in html
    assert "restoreHidden" in html
    assert "恢复已删除" in html
    assert "aliasChannel" in html
    assert "别名映射" in html
    assert "编辑模型" not in html
    assert "editingModels" not in html


def test_dashboard_does_not_block_on_credit_summary():
    html = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(encoding="utf-8")
    assert "api.get('/admin/stats'" in html
    assert "api.get('/admin/credit-summary'" in html
    assert "Promise.all([api.get('/admin/stats'" not in html
    assert "正在读取上游额度" in html


def test_dashboard_uses_line_and_pie_charts():
    html = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(encoding="utf-8")
    assert "hour-line" in html
    assert "hour-poly" in html
    assert "qualityPie" in html
    assert "modelPie" in html
    assert "donut-split" in html
    assert "spark-line" in html
    assert "mini-line" in html
    assert "mini-dot" in html
    assert "近 7 日" in html
    assert "hour-bars" not in html
    assert "今日质量" in html
    assert "模型占比" in html


def test_account_test_ui_lets_user_pick_model():
    html = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(encoding="utf-8")
    assert "openTest" in html
    assert "runTest" in html
    assert "test.model" in html
    assert "开始测试" in html
    assert "{model:'auto',prompt:'ping'}" not in html


def test_supplier_catalog_refresh_keeps_channels_distinct(isolated_db, all_channels, monkeypatch):
    assert QCLAW_NEW_ID not in QCLAW_STATIC
    assert TRAE_NEW_DOUBAO not in TRAE_STATIC
    assert TRAE_DOUBAO_TURBO in TRAE_STATIC

    qclaw = providers.get_provider("qclaw")
    traework = providers.get_provider("traework")
    workbuddy = providers.get_provider("workbuddy")
    qwenwork = providers.get_provider("qwenwork")

    assert QCLAW_NEW_ID not in _ids(qclaw.list_models())
    assert TRAE_NEW_DOUBAO not in _ids(traework.list_models())
    assert WB_NEW_ID not in _ids(workbuddy.list_models())
    assert not qclaw.accepts_model(QCLAW_NEW_ID)
    assert not traework.accepts_model(TRAE_NEW_DOUBAO)
    assert not workbuddy.accepts_model(WB_NEW_ID)

    _seed_live_accounts()
    requested = _install_supplier_http(monkeypatch)

    chat_hits = []

    async def boom_chat(payload, api_key_info):
        chat_hits.append((payload.get("model"), api_key_info))
        raise AssertionError("chat should not run")

    for channel in providers.enabled_provider_ids():
        monkeypatch.setattr(providers.get_provider(channel), "chat_completions", boom_chat)

    monkeypatch.setattr(server, "ALLOW_NO_ADMIN_AUTH", True)
    result = asyncio.run(server.admin_refresh_models())
    sources = _by_channel(result)

    assert sources["qclaw"]["mode"] == "live"
    assert sources["traework"]["mode"] == "live"
    assert sources["workbuddy"]["mode"] == "live"
    assert sources["qwenwork"]["mode"] == "live"

    assert QCLAW_NEW_ID in _ids(sources["qclaw"]["models"])
    assert TRAE_NEW_DOUBAO in _ids(sources["traework"]["models"])
    assert TRAE_DOUBAO_TURBO in _ids(sources["traework"]["models"])

    wb_ids = _ids(sources["workbuddy"]["models"])
    assert WB_NEW_ID in wb_ids
    assert "glm-5.2" in wb_ids
    assert "auto" in wb_ids
    assert "hunyuan-image-v3.0" not in wb_ids
    assert QCLAW_NEW_ID not in wb_ids
    assert TRAE_NEW_DOUBAO not in wb_ids
    assert TRAE_DOUBAO_TURBO not in wb_ids

    qwen_ids = _ids(sources["qwenwork"]["models"])
    assert "pro" in qwen_ids
    assert QWEN_NEW_ID in qwen_ids
    assert "disabled-model" not in qwen_ids
    assert TRAE_NEW_DOUBAO not in qwen_ids
    assert QCLAW_NEW_ID not in qwen_ids

    assert any("/data/4320/forward" in url for method, url in requested if method == "POST")
    assert any("/api/remote/v1/models" in url for method, url in requested if method == "GET")
    assert any("/api/v2/model/list" in url for method, url in requested if method == "GET")
    assert any("/v2/enterprises/personal/models" in url for method, url in requested if method == "GET")
    # workbuddy 通道的模型列表请求打的是「账号自己的站点」：
    # 种子账号没写 domain，add_account 会填 DB 默认值 www.codebuddy.cn，
    # 而 codebuddy.cn 与 workbuddy.cn 一样是真实的国内站点（见 auth_manager.backend_url_for）。
    # 因此这里接受该国内域名或全局回退域名 copilot.tencent.com。
    wb_backend_urls = [
        url
        for _, url in requested
        if "copilot.tencent.com" in url or "www.codebuddy.cn" in url
    ]
    assert wb_backend_urls
    assert all("/v2/enterprises/personal/models" in url for url in wb_backend_urls)

    assert QCLAW_NEW_ID in _ids(qclaw.list_models())
    assert TRAE_NEW_DOUBAO in _ids(traework.list_models())
    assert QWEN_NEW_ID in _ids(qwenwork.list_models())
    assert WB_NEW_ID in _ids(workbuddy.list_models())
    assert qclaw.accepts_model(QCLAW_NEW_ID)
    assert traework.accepts_model(TRAE_NEW_DOUBAO)
    assert qwenwork.accepts_model(QWEN_NEW_ID)
    assert qwenwork.accepts_model("pro")
    assert traework.accepts_model(TRAE_DOUBAO_TURBO)
    assert workbuddy.accepts_model(WB_NEW_ID)
    assert not workbuddy.accepts_model("hunyuan-image-v3.0")
    assert not workbuddy.accepts_model(TRAE_NEW_DOUBAO)
    assert not workbuddy.accepts_model(TRAE_DOUBAO_TURBO)
    assert not qclaw.accepts_model(TRAE_NEW_DOUBAO)
    assert not qwenwork.accepts_model(TRAE_NEW_DOUBAO)
    assert not qwenwork.accepts_model(QCLAW_NEW_ID)

    bound = router.bind({"model": "traework/" + TRAE_NEW_DOUBAO}, {"default_channel": "traework"})
    assert bound.channel == "traework"
    assert bound.inner == TRAE_NEW_DOUBAO
    bound = router.bind({"model": "qclaw/" + QCLAW_NEW_ID}, {"default_channel": "qclaw"})
    assert bound.channel == "qclaw"
    assert bound.inner == QCLAW_NEW_ID

    def attempt_chat(payload, key):
        bound = router.bind(payload, key)
        return asyncio.run(router.chat_after_bind(bound, payload, key))

    with pytest.raises(UnknownModel):
        attempt_chat({"model": TRAE_NEW_DOUBAO, "messages": [{"role": "user", "content": "hi"}]}, {"default_channel": "workbuddy"})
    with pytest.raises(UnknownModel):
        attempt_chat({"model": TRAE_DOUBAO_TURBO, "messages": [{"role": "user", "content": "hi"}]}, {"default_channel": "workbuddy"})
    with pytest.raises(KeyChannelMismatch):
        attempt_chat({"model": "traework/" + TRAE_NEW_DOUBAO, "messages": [{"role": "user", "content": "hi"}]}, {"default_channel": "workbuddy"})
    with pytest.raises(UnknownModel):
        attempt_chat({"model": QCLAW_NEW_ID, "messages": [{"role": "user", "content": "hi"}]}, {"default_channel": "workbuddy"})

    assert chat_hits == []

    payload = {"object": "list", "data": server.collect_v1_models()}
    data = payload["data"]
    by_id = {item["id"]: item for item in data}

    assert by_id["qclaw/" + QCLAW_NEW_ID]["channel"] == "qclaw"
    assert by_id["traework/" + TRAE_NEW_DOUBAO]["channel"] == "traework"
    assert by_id["traework/" + TRAE_DOUBAO_TURBO]["channel"] == "traework"
    assert TRAE_NEW_DOUBAO not in by_id
    assert TRAE_DOUBAO_TURBO not in by_id
    assert QCLAW_NEW_ID not in by_id
    assert "glm-5.2" in by_id
    assert by_id["glm-5.2"]["channel"] == "workbuddy"
    assert by_id["workbuddy/glm-5.2"]["channel"] == "workbuddy"
    assert WB_NEW_ID in by_id
    assert by_id[WB_NEW_ID]["channel"] == "workbuddy"
    assert by_id["workbuddy/" + WB_NEW_ID]["channel"] == "workbuddy"
    assert "hunyuan-image-v3.0" not in by_id

    evidence = os.environ.get("BUDDY2API_EVIDENCE_DIR")
    if evidence:
        Path(evidence).mkdir(parents=True, exist_ok=True)
        Path(evidence, "v1-models.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def test_parse_workbuddy_supplier_models_skips_image_and_non_string_ids():
    from buddy2api.providers.workbuddy.models import parse_supplier_models

    models = parse_supplier_models(WORKBUDDY_HTTP_PAYLOAD)
    ids = {item["id"] for item in models}
    assert ids == {"auto", "glm-5.2", WB_NEW_ID}
    assert next(item["name"] for item in models if item["id"] == WB_NEW_ID) == "WB Live Only"


def test_manual_add_goes_to_selected_channel(isolated_db, all_channels):
    import buddy2api.catalog as catalog

    custom_qw = "qwork-user-added"
    custom_wb = "wb-user-added"
    custom_qc = "qclaw-user-added"

    qwenwork = providers.get_provider("qwenwork")
    workbuddy = providers.get_provider("workbuddy")
    qclaw = providers.get_provider("qclaw")

    catalog.upsert_model("qwenwork", custom_qw, "Qwen extra")
    catalog.upsert_model("workbuddy", custom_wb, "WB extra")
    catalog.upsert_model("qclaw", "qclaw/" + custom_qc, "QC extra")

    assert qwenwork.accepts_model(custom_qw)
    assert not workbuddy.accepts_model(custom_qw)
    assert not qclaw.accepts_model(custom_qw)
    assert workbuddy.accepts_model(custom_wb)
    assert not qwenwork.accepts_model(custom_wb)
    assert qclaw.accepts_model(custom_qc)
    assert not workbuddy.accepts_model(custom_qc)

    by_id = {item["id"]: item for item in server.collect_v1_models()}
    assert by_id["qwenwork/" + custom_qw]["channel"] == "qwenwork"
    assert custom_qw not in by_id
    assert custom_wb in by_id
    assert by_id[custom_wb]["channel"] == "workbuddy"
    assert by_id["qclaw/" + custom_qc]["channel"] == "qclaw"
    assert custom_qc not in by_id

    snap = {item["channel"]: item for item in catalog.catalog_snapshot()["sources"]}
    qw_manual = [item for item in snap["qwenwork"]["models"] if item.get("id") == custom_qw]
    assert qw_manual and qw_manual[0].get("manual") is True
    wb_manual = [item for item in snap["workbuddy"]["models"] if item.get("id") == custom_wb]
    assert wb_manual and wb_manual[0].get("manual") is True

    catalog.remove_model("qwenwork", custom_qw)
    assert not qwenwork.accepts_model(custom_qw)
    catalog.remove_model("workbuddy", custom_wb)
    assert not workbuddy.accepts_model(custom_wb)


def test_manual_extra_survives_live_refresh(isolated_db, all_channels, monkeypatch):
    import buddy2api.catalog as catalog

    extra = "qclaw-hand-added"
    wb_extra = "wb-hand-added"
    catalog.upsert_model("qclaw", extra, "Hand added")
    catalog.upsert_model("workbuddy", wb_extra, "WB hand added")
    assert extra not in _ids(QCLAW_STATIC)
    assert providers.get_provider("qclaw").accepts_model(extra)
    assert providers.get_provider("workbuddy").accepts_model(wb_extra)

    _seed_live_accounts()
    _install_supplier_http(monkeypatch)
    monkeypatch.setattr(server, "ALLOW_NO_ADMIN_AUTH", True)
    result = asyncio.run(server.admin_refresh_models())
    sources = _by_channel(result)

    assert sources["qclaw"]["mode"] == "live"
    assert extra in _ids(sources["qclaw"]["models"])
    assert QCLAW_NEW_ID in _ids(sources["qclaw"]["models"])
    assert providers.get_provider("qclaw").accepts_model(extra)
    assert providers.get_provider("qclaw").accepts_model(QCLAW_NEW_ID)
    assert extra not in _ids(providers.get_provider("workbuddy").list_models())
    assert sources["workbuddy"]["mode"] == "live"
    assert wb_extra in _ids(sources["workbuddy"]["models"])
    assert WB_NEW_ID in _ids(sources["workbuddy"]["models"])
    assert providers.get_provider("workbuddy").accepts_model(wb_extra)


def test_manual_add_rejects_unknown_channel(isolated_db, all_channels):
    import buddy2api.catalog as catalog

    with pytest.raises(catalog.CatalogError):
        catalog.upsert_model("not-a-channel", "foo")
    with pytest.raises(catalog.CatalogError):
        catalog.upsert_model("qwenwork", "")
    with pytest.raises(catalog.CatalogError):
        catalog.upsert_model("qwenwork", "qwork-advanced")
    with pytest.raises(catalog.CatalogError):
        catalog.upsert_model("workbuddy", "glm-5.2")
    # 官方模型可以删（落墓碑隐藏），但不存在于任何列表的 id 仍然报错。
    with pytest.raises(catalog.CatalogError):
        catalog.remove_model("qwenwork", "qwork-nonexistent")
    with pytest.raises(catalog.CatalogError):
        catalog.remove_model("workbuddy", "")


def test_official_model_can_be_removed_and_restored(isolated_db, all_channels, monkeypatch):
    """官方模型也要能删：删除落墓碑，一键读取不会带回来，重新添加同一个 ID 即恢复。"""
    import buddy2api.catalog as catalog

    workbuddy = providers.get_provider("workbuddy")
    assert workbuddy.accepts_model("glm-5.2")

    removed = catalog.remove_model("workbuddy", "glm-5.2")
    assert "glm-5.2" not in _ids(removed["models"])
    assert "glm-5.2" not in _ids(workbuddy.list_models())
    assert not workbuddy.accepts_model("glm-5.2")
    assert "glm-5.2" not in {item["id"] for item in server.collect_v1_models()}

    # 一键读取（在线目录）不会把删掉的官方模型带回来。
    _seed_live_accounts()
    _install_supplier_http(monkeypatch)
    monkeypatch.setattr(server, "ALLOW_NO_ADMIN_AUTH", True)
    sources = _by_channel(asyncio.run(server.admin_refresh_models()))
    wb_ids = _ids(sources["workbuddy"]["models"])
    assert WB_NEW_ID in wb_ids
    assert "glm-5.2" not in wb_ids
    assert sources["workbuddy"]["hidden_count"] == 1
    assert not workbuddy.accepts_model("glm-5.2")

    # 手动添加过的模型：删除即移除，不会留下墓碑。
    catalog.upsert_model("workbuddy", "wb-user-model", "WB user")
    assert workbuddy.accepts_model("wb-user-model")
    catalog.remove_model("workbuddy", "wb-user-model")
    assert not workbuddy.accepts_model("wb-user-model")
    assert "wb-user-model" not in catalog.removed_ids("workbuddy")

    # 恢复：填回同一个 ID 即重新可见（墓碑被清掉，也不变成手动项）。
    restored = catalog.upsert_model("workbuddy", "glm-5.2", "")
    assert restored["restored"] is True
    assert workbuddy.accepts_model("glm-5.2")
    assert catalog.removed_ids("workbuddy") == set()
    row = next(item for item in catalog.catalog_snapshot()["sources"][0]["models"] if item["id"] == "glm-5.2")
    assert not row.get("manual")

    # 删除是幂等的：重复删同一个官方模型只留一条墓碑。
    catalog.remove_model("workbuddy", "glm-5.2")
    catalog.remove_model("workbuddy", "glm-5.2")
    assert catalog.removed_ids("workbuddy") == {"glm-5.2"}
    assert "glm-5.2" not in _ids(workbuddy.list_models())


def test_catalog_snapshot_annotates_credit_and_sites(isolated_db, all_channels):
    """目录接口要能直接喂给模型配置页：每个模型带累计积分与可用站点。

    「刷新」按钮就是重新拉一次这个接口，所以积分必须在请求时现算 —— 这里往日志里写
    两条请求，断言快照里的 credit 跟着变，而不是读某个缓存。
    """
    import buddy2api.auth_manager as auth_manager
    import buddy2api.catalog as catalog
    import buddy2api.sites as sites

    intl = db.add_account(
        {
            "name": "intl",
            "uid": "intl-1",
            "provider": "workbuddy",
            "status": "active",
            "access_token": "tok-intl",
            "domain": "www.workbuddy.ai",
            "expires_at": 9_999_999_999_999,
        }
    )
    cn = db.add_account(
        {
            "name": "cn",
            "uid": "cn-1",
            "provider": "workbuddy",
            "status": "active",
            "access_token": "tok-cn",
            "domain": "www.workbuddy.cn",
            "expires_at": 9_999_999_999_999,
        }
    )
    auth_manager.record_account_models(intl, ["glm-5.2", "kimi-k2.7"])
    auth_manager.record_account_models(cn, ["auto", "kimi-k2.7"])
    # 持久化的站点/扣费基准（真实场景由刷新目录时写入）。
    db.set_setting(catalog.MODEL_SITES_SETTING, {
        "workbuddy": {
            "auto": {"sites": ["domestic"], "rate": {"domestic": 2.2}},
            "glm-5.2": {"sites": ["international"], "rate": {"international": 0.79}},
            "kimi-k2.7": {"sites": ["domestic", "international"],
                          "rate": {"domestic": 1.2, "international": 0.9}},
        }
    })
    try:
        db.add_log({"model": "auto", "account_id": cn, "credit": 1.5, "status_code": 200})
        db.add_log({"model": "workbuddy/auto", "account_id": cn, "credit": 0.5, "status_code": 200})
        db.add_log({"model": "auto", "account_id": cn, "credit": 9.9, "status_code": 500})
        db.add_log({"model": "glm-5.2", "account_id": intl, "credit": 0, "status_code": 200})

        snap = {item["channel"]: item for item in catalog.catalog_snapshot()["sources"]}
        wb = {item["id"]: item for item in snap["workbuddy"]["models"]}

        # 带通道前缀与不带前缀的日志都算进同一个模型；失败请求不计。
        assert wb["auto"]["credit"] == 2.0
        assert wb["auto"]["credit_requests"] == 2
        assert wb["auto"]["sites"] == [sites.SITE_DOMESTIC]
        assert wb["glm-5.2"]["sites"] == [sites.SITE_INTERNATIONAL]
        assert wb["kimi-k2.7"]["sites"] == sorted([sites.SITE_DOMESTIC, sites.SITE_INTERNATIONAL])
        # 扣费基准：按站点记录的官方倍率（站点未知时不能瞎猜一个）。
        assert wb["auto"]["credit_rate"] == 2.2
        assert wb["glm-5.2"]["credit_rate"] == 0.79
        # 两边倍率不同的模型不给单一值，由前端按站点分别展示。
        assert "credit_rate" not in wb["kimi-k2.7"]
        assert wb["kimi-k2.7"]["site_rates"] == {"domestic": 1.2, "international": 0.9}
        # 没有持久化信息的模型不瞎补字段。
        assert "credit_rate" not in wb["glm-5v-turbo"]
        # 没有成功请求的模型显示 0 而不是缺字段（前端据此渲染「—」）。
        assert wb["kimi-k2.7"]["credit"] == 0
        assert wb["kimi-k2.7"]["credit_requests"] == 0

        # 非 WorkBuddy 通道不贴站点标签：它们的账号没有国内/国际站之分。
        for channel, source in snap.items():
            if channel == "workbuddy":
                continue
            assert all(not item["sites"] for item in source["models"])
            assert all("credit" in item for item in source["models"])
    finally:
        auth_manager.forget_account(intl)
        auth_manager.forget_account(cn)


def test_refresh_returns_credit_annotations_too(isolated_db, all_channels, monkeypatch):
    """一键读取供应模型同样要带积分与站点 —— 它是页面上另一个会刷新列表的按钮。"""
    import buddy2api.auth_manager as auth_manager
    import buddy2api.catalog as catalog
    import buddy2api.sites as sites

    _seed_live_accounts()
    _install_supplier_http(monkeypatch)
    monkeypatch.setattr(server, "ALLOW_NO_ADMIN_AUTH", True)
    account = db.list_accounts(provider="workbuddy")[0]
    db.update_account(account["id"], {"domain": "www.workbuddy.cn"})
    try:
        db.add_log({"model": WB_NEW_ID, "account_id": account["id"], "credit": 3.25, "status_code": 200})
        sources = _by_channel(asyncio.run(server.admin_refresh_models()))
        rows = {item["id"]: item for item in sources["workbuddy"]["models"]}
        assert rows[WB_NEW_ID]["credit"] == 3.25
        assert rows[WB_NEW_ID]["credit_requests"] == 1
        assert rows[WB_NEW_ID]["sites"] == [sites.SITE_DOMESTIC]
        # 刷新会把上游的 credits 字段写入持久化映射，下次快照（重启后）仍有倍率。
        stored = db.get_setting(catalog.MODEL_SITES_SETTING, {})["workbuddy"]
        assert stored[WB_NEW_ID]["sites"] == [sites.SITE_DOMESTIC]
        assert catalog.catalog_snapshot()["sources"]
    finally:
        auth_manager.forget_account(account["id"])


def test_parse_supplier_models_keeps_credit_rate():
    """上游的 credits 字段是官方客户端「x0.79 / Free now」的来源，不能在解析时丢掉。

    同一个模型两边的倍率不同（实测 hy4-preview 国际 x0.00、国内 x0.29），
    所以这里必须原样保留数值，由目录层按站点分组记录。
    """
    from buddy2api.providers.workbuddy.models import _parse_credits, parse_supplier_models

    assert _parse_credits("x0.79 credits") == 0.79
    assert _parse_credits("x0.00") == 0.0
    assert _parse_credits("X3.31") == 3.31
    assert _parse_credits(None) is None
    assert _parse_credits("") is None
    assert _parse_credits("not-a-number") is None

    models = parse_supplier_models(WORKBUDDY_HTTP_PAYLOAD)
    by_id = {item["id"]: item for item in models}
    assert by_id["auto"]["credit_rate"] == 0.79
    assert by_id[WB_NEW_ID]["credit_rate"] == 0.0
    # 非 WorkBuddy 的通道与 text-to-image 行不涉及；缺失字段不给默认值。
    assert "credit_rate" not in by_id.get("hunyuan-image-v3.0", {"id": 123})


def test_credit_rate_falls_back_to_observed_free_usage(isolated_db, all_channels):
    """目录里没有某站点的倍率时，用成功日志反推「免费」。

    线上实测：deepseek-v4.1-flash 在国际站目录里不存在，但国际站账号打它
    5026 次全部 credit=0。此时若沿用国内目录的 x0.03，会让人以为国际站也扣费。
    """
    import buddy2api.auth_manager as auth_manager
    import buddy2api.catalog as catalog
    import buddy2api.sites as sites

    intl = db.add_account(
        {
            "name": "intl",
            "uid": "intl-free",
            "provider": "workbuddy",
            "status": "active",
            "access_token": "tok-intl",
            "domain": "www.workbuddy.ai",
            "expires_at": 9_999_999_999_999,
        }
    )
    cn = db.add_account(
        {
            "name": "cn",
            "uid": "cn-free",
            "provider": "workbuddy",
            "status": "active",
            "access_token": "tok-cn",
            "domain": "www.workbuddy.cn",
            "expires_at": 9_999_999_999_999,
        }
    )
    auth_manager.record_account_models(intl, ["deepseek-v4.1-flash"])
    auth_manager.record_account_models(cn, ["deepseek-v4.1-flash"])
    # 目录只在采样国内站时给了倍率（国际站目录里根本没这个模型）。
    db.set_setting(catalog.CATALOG_SETTING, {
        "workbuddy": [
            {"id": "deepseek-v4.1-flash", "name": "Deepseek-V4.1-Flash"},
            {"id": "glm-5.2", "name": "GLM-5.2"},
        ]
    })
    db.set_setting(catalog.MODEL_SITES_SETTING, {
        "workbuddy": {"deepseek-v4.1-flash": {"sites": ["domestic"], "rate": {"domestic": 0.03}}}
    })
    try:
        for _ in range(6):
            db.add_log({"model": "deepseek-v4.1-flash", "account_id": intl, "credit": 0, "status_code": 200})
        for _ in range(3):
            db.add_log({"model": "deepseek-v4.1-flash", "account_id": cn, "credit": 0.5, "status_code": 200})
        auth_manager.forget_cost_profile()

        wb = {
            item["id"]: item
            for source in catalog.catalog_snapshot()["sources"]
            if source["channel"] == "workbuddy"
            for item in source["models"]
        }
        row = wb["deepseek-v4.1-flash"]
        # 国际站：实测 6 次 0 扣费 → 免费；国内站：目录给的 0.03。
        assert row["sites"] == [sites.SITE_DOMESTIC, sites.SITE_INTERNATIONAL]
        assert row["site_rates"] == {"domestic": 0.03, "international": 0.0}
        # 两边倍率不同 → 不给单一值，由前端按站点分别展示。
        assert "credit_rate" not in row

        # 样本太少（<5）时不猜免费：宁可显示未知。
        auth_manager.forget_cost_profile()
        db.add_log({"model": "glm-5.2", "account_id": intl, "credit": 0, "status_code": 200})
        wb2 = {
            item["id"]: item
            for source in catalog.catalog_snapshot()["sources"]
            if source["channel"] == "workbuddy"
            for item in source["models"]
        }
        assert wb2["glm-5.2"]["sites"] == [sites.SITE_INTERNATIONAL]
        assert "site_rates" not in wb2["glm-5.2"]
        assert "credit_rate" not in wb2["glm-5.2"]
    finally:
        auth_manager.forget_account(intl)
        auth_manager.forget_account(cn)
        auth_manager.forget_cost_profile()
