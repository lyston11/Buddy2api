"""指纹头的回归测试。

上游的渠道校验（2026-10-24 逐头实测）只认三件事，任何一件丢了整批请求都会变成
400 code 11128（见 fingerprint.py 开头）：
  1. 首条消息是 system（由 proxy._ensure_leading_system_message 保证）；
  2. x-conversation-id / -request-id / -message-id 三个头都在；
  3. User-Agent 自称官方客户端（WorkBuddy/<app>... 或 CLI/<cli> CodeBuddy/<cli>）。

2026-10-27 起还有一层「像不像官方客户端」的约束 —— 依据是 app.asar 里的官方实现
（axios 1.16.1 + Node https.Agent，桌面端主进程走 fetch），本文件把这些形态钉死：
头名全小写、uuid 是 v7、x-request-id 与 x-conversation-message-id 同值、
x-conversation-request-id 一轮一个、`x-no-*` 的取值是 "true"。
"""

import base64
import json
import uuid

import buddy2api.fingerprint as fingerprint

ACCOUNT = {
    "access_token": "tok",
    "uid": "user-1",
    "domain": "www.codebuddy.cn",
    "enterprise_id": "ent-1",
}

PERSONAL_ACCOUNT = {
    "access_token": "tok",
    "uid": "user-1",
    "domain": "www.codebuddy.cn",
    "enterprise_id": "",
}


def _messages(user="hello"):
    return [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": user},
    ]


def _assert_lowercase_names(headers):
    for name in headers:
        assert name == name.lower(), f"头名必须是全小写: {name}"


def test_chat_headers_default_to_desktop_app_profile():
    fingerprint.begin_request(None)
    headers = fingerprint.chat_headers(ACCOUNT)
    assert headers["x-ide-type"] == "WorkBuddy"
    assert headers["x-ide-name"] == "WorkBuddy"
    assert headers["x-private-data"] == "true"
    assert headers["x-agent-purpose"] == "conversation"
    assert headers["x-product"] == "SaaS"
    assert headers["user-agent"].startswith("WorkBuddy/")
    assert " CLI/" in headers["user-agent"]
    assert headers["authorization"] == "Bearer tok"
    _assert_lowercase_names(headers)


def test_chat_headers_follow_official_assembly_order():
    """头序照抄官方客户端的拼装顺序（桌面端主进程 fetch 路径）。"""
    fingerprint.begin_request(None)
    headers = list(fingerprint.chat_headers(ACCOUNT))
    expected_prefix = [
        "accept",
        "content-type",
        "x-requested-with",
        "x-conversation-id",
        "x-conversation-request-id",
        "acp-connection-id",
        "x-conversation-message-id",
        "x-request-id",
        "x-agent-intent",
        "x-agent-type",
        "x-agent-purpose",
        "x-codebuddy-request",
        "x-ide-type",
        "x-ide-name",
        "x-ide-version",
        "x-private-data",
        "x-product",
    ]
    assert headers[: len(expected_prefix)] == expected_prefix


def test_chat_headers_keep_channel_verification_headers():
    fingerprint.begin_request(None)
    headers = fingerprint.chat_headers(ACCOUNT)
    for name in fingerprint.CONVERSATION_HEADERS:
        assert headers.get(name.lower()), name
    assert headers["x-request-id"] == headers["x-conversation-message-id"]
    assert headers["traceparent"].startswith("00-")
    assert headers["b3"].count("-") == 3


def test_chat_headers_drop_refresh_token_and_stainless():
    fingerprint.begin_request(None)
    headers = fingerprint.chat_headers(ACCOUNT)
    lowered = {k.lower() for k in headers}
    # 安全红线：chat 请求绝不携带 x-refresh-token
    assert "x-refresh-token" not in lowered
    assert "cosy-key" not in lowered
    # 官方客户端不发 x-stainless-*（那是别的 SDK 的痕迹），我们也不发
    assert not any(name.startswith("x-stainless") for name in lowered)
    # 官方客户端的 chat 请求里没有 origin/referer（Node/主进程 fetch 都不加）
    assert "origin" not in lowered and "referer" not in lowered


def test_chat_headers_cli_profile_matches_axios_shape():
    fingerprint.begin_request(None)
    headers = fingerprint.chat_headers(ACCOUNT, channel=fingerprint.CHANNEL_CLI)
    assert headers["x-ide-type"] == "CLI"
    assert headers["x-ide-name"] == "CLI"
    assert headers["user-agent"].startswith("CLI/")
    assert "CodeBuddy/" in headers["user-agent"]
    # 内置 CLI（axios）不发这些东西
    assert "x-agent-purpose" not in headers
    assert "acp-connection-id" not in headers
    assert "x-codebuddy-request" not in headers
    assert "x-requested-with" not in headers
    assert "traceparent" not in headers
    assert headers["accept"] == "application/json, text/plain, */*"
    _assert_lowercase_names(headers)


def test_ids_are_uuid7_and_message_id_matches_request_id():
    fingerprint.begin_request(None)
    headers = fingerprint.chat_headers(ACCOUNT)
    conversation_id = uuid.UUID(headers["x-conversation-id"])
    assert conversation_id.version == 7, "官方客户端用的是 uuidv7"
    assert conversation_id.variant == uuid.RFC_4122

    message_id = headers["x-conversation-message-id"]
    assert len(message_id) == 32 and "-" not in message_id
    # 去短横线的 uuid7：第 13 位是版本位
    assert message_id[12] == "7"
    assert int(message_id[16], 16) >= 8  # variant 位
    assert headers["x-request-id"] == message_id


def test_conversation_request_id_is_stable_within_one_request():
    """一轮 prompt 里的多次模型调用共用一个 x-conversation-request-id。"""
    fingerprint.begin_request(fingerprint.conversation_key(_messages(), 1))
    first = fingerprint.chat_headers(ACCOUNT)
    second = fingerprint.chat_headers(ACCOUNT)
    assert first["x-conversation-request-id"] == second["x-conversation-request-id"]
    # 但每条消息（每次 HTTP）是新的 message id
    assert first["x-conversation-message-id"] != second["x-conversation-message-id"]

    fingerprint.begin_request(fingerprint.conversation_key(_messages(), 1))
    third = fingerprint.chat_headers(ACCOUNT)
    assert third["x-conversation-request-id"] != first["x-conversation-request-id"]
    # 同一段对话仍然是同一个 x-conversation-id
    assert third["x-conversation-id"] == first["x-conversation-id"]


def test_conversation_id_is_stable_per_conversation_and_differs_across_ones():
    key = fingerprint.conversation_key(_messages("第一个话题"), 7)
    other_key = fingerprint.conversation_key(_messages("另一个话题"), 7)
    fingerprint.begin_request(key)
    first = fingerprint.chat_headers(ACCOUNT)
    fingerprint.begin_request(key)
    second = fingerprint.chat_headers(ACCOUNT)
    fingerprint.begin_request(other_key)
    other = fingerprint.chat_headers(ACCOUNT)
    assert first["x-conversation-id"] == second["x-conversation-id"]
    assert other["x-conversation-id"] != first["x-conversation-id"]
    for header in (first, other):
        assert uuid.UUID(header["x-conversation-id"]).version == 7


def test_switch_channel_flips_profile_and_begin_request_resets_it():
    fingerprint.begin_request(None)
    assert fingerprint.chat_headers(ACCOUNT)["x-ide-type"] == "WorkBuddy"
    assert fingerprint.switch_channel() == fingerprint.CHANNEL_CLI
    assert fingerprint.chat_headers(ACCOUNT)["x-ide-type"] == "CLI"
    fingerprint.begin_request(None)
    assert fingerprint.chat_headers(ACCOUNT)["x-ide-type"] == "WorkBuddy"


def test_identity_headers_match_official_markers():
    """`x-no-*` 的真实取值是 "true"；企业账号才带 x-userinfo（base64 JSON）。"""
    fingerprint.begin_request(None)
    enterprise = fingerprint.chat_headers(ACCOUNT)
    assert enterprise["x-user-id"] == "user-1"
    assert enterprise["x-enterprise-id"] == "ent-1"
    assert enterprise["x-tenant-id"] == "ent-1"
    assert enterprise["x-domain"] == "www.codebuddy.cn"
    claim = json.loads(base64.b64decode(enterprise["x-userinfo"]).decode("utf-8"))
    assert claim == {"uin": "user-1", "owner_uin": "ent-1"}

    personal = fingerprint.chat_headers(PERSONAL_ACCOUNT)
    assert "x-enterprise-id" not in personal
    assert personal["x-no-enterprise-id"] == "true"
    assert "x-userinfo" not in personal


def test_device_headers_follow_client_and_skip_missing_qimei(monkeypatch, tmp_path):
    cache = tmp_path / "qimei-cache.json"
    cache.write_text('{"qimei36":"c918e83b51bc0186deabc42c200015e1a111"}', encoding="utf-8")
    monkeypatch.setitem(
        fingerprint._QIMEI_CACHE_FILES, "domestic", str(cache)
    )
    monkeypatch.delenv("CB_GATEWAY_QIMEI36", raising=False)
    fingerprint.begin_request(None)
    headers = fingerprint.chat_headers(ACCOUNT)
    assert headers["x-custom-channel-id"] == "unknown"
    assert json.loads(headers["x-device-id-list"]) == {
        "qimei36": "c918e83b51bc0186deabc42c200015e1a111"
    }

    # 缓存文件没了、SDK 也还没算出来（首次启动那一小段）就先不发这个头
    monkeypatch.setitem(
        fingerprint._QIMEI_CACHE_FILES, "domestic", str(tmp_path / "missing.json")
    )
    monkeypatch.setattr(fingerprint, "_QIMEI_RUNTIME", {})
    monkeypatch.setattr(fingerprint, "_QIMEI_CACHE_PATH", tmp_path / "qimei36.json")
    monkeypatch.setattr(fingerprint, "_kick_off_qimei_detection", lambda _group: None)
    fingerprint._qimei_from_file.cache_clear()
    assert "x-device-id-list" not in fingerprint.chat_headers(ACCOUNT)


def test_billing_and_refresh_headers_are_lowercase_and_scoped():
    fingerprint.begin_request(None)
    billing = fingerprint.billing_headers(ACCOUNT)
    _assert_lowercase_names(billing)
    assert "x-conversation-id" not in billing
    assert billing["authorization"] == "Bearer tok"

    refresh = fingerprint.refresh_headers({**ACCOUNT, "refresh_token": "refresh-1"})
    _assert_lowercase_names(refresh)
    assert refresh["x-refresh-token"] == "refresh-1"
    assert refresh["authorization"] == "Bearer tok"


def test_international_site_resolves_qimei_via_bundled_sdk(monkeypatch, tmp_path):
    """国际版 product.json 没有 appKey，靠官方 SDK 现算设备标识。

    实测（把 HOME 指到空目录、用国内版 appKey 跑国际版自带的 helper）返回的仍是这台
    机器原来那个 qimei36 —— 说明它是设备级 id，用哪款客户端的 appKey 算都一样。
    """
    monkeypatch.delenv("CB_GATEWAY_QIMEI36", raising=False)
    monkeypatch.setattr(fingerprint, "_QIMEI_RUNTIME", {})
    monkeypatch.setattr(fingerprint, "_QIMEI_PENDING", set())
    monkeypatch.setattr(fingerprint, "_QIMEI_CACHE_PATH", tmp_path / "qimei36.json")

    calls = []

    def fake_helper(app_dir, stamp):
        calls.append(app_dir)
        return "c918e83b51bc0186deabc42c200015e1a111"

    monkeypatch.setattr(fingerprint, "_qimei_from_helper", fake_helper)
    monkeypatch.setitem(
        fingerprint._QIMEI_CACHE_FILES, "international", str(tmp_path / "missing.json")
    )

    # 同步跑一次探测（正常路径是后台线程，这里直接调同一个函数）
    value = fingerprint.detect_qimei_via_sdk("international")
    assert value == "c918e83b51bc0186deabc42c200015e1a111"
    # 先试国际版那款客户端
    assert calls[0] == fingerprint._MAC_APP_BUNDLES[fingerprint.sites.SITE_INTERNATIONAL]


def test_qimei_off_switch_and_env_override(monkeypatch):
    monkeypatch.setattr(fingerprint, "_QIMEI_RUNTIME", {})
    monkeypatch.setenv("CB_GATEWAY_QIMEI36", "deadbeef" * 4 + "dead")
    assert fingerprint.chat_headers(ACCOUNT)["x-device-id-list"].startswith('{"qimei36":"deadbeef')
    monkeypatch.setenv("CB_GATEWAY_QIMEI36", "off")
    fingerprint.begin_request(None)
    assert "x-device-id-list" not in fingerprint.chat_headers(ACCOUNT)


def test_qimei_persisted_value_is_reused_without_spawning(monkeypatch, tmp_path):
    """算过一次就落盘，重启后直接读，不再 spawn SDK。"""
    cache = tmp_path / "qimei36.json"
    cache.write_text('{"international": "c918e83b51bc0186deabc42c200015e1a111"}', encoding="utf-8")
    monkeypatch.delenv("CB_GATEWAY_QIMEI36", raising=False)
    monkeypatch.setattr(fingerprint, "_QIMEI_RUNTIME", {})
    monkeypatch.setattr(fingerprint, "_QIMEI_CACHE_PATH", cache)
    monkeypatch.setitem(
        fingerprint._QIMEI_CACHE_FILES, "international", str(tmp_path / "missing.json")
    )

    def boom(*_args, **_kwargs):
        raise AssertionError("有落盘缓存时不该再跑 SD")

    monkeypatch.setattr(fingerprint, "_kick_off_qimei_detection", boom)
    assert fingerprint.qimei36("www.workbuddy.ai") == "c918e83b51bc0186deabc42c200015e1a111"
