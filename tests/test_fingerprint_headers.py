"""指纹头的回归测试。

上游的渠道校验（2026-10-24 逐头实测）只认三件事，任何一件丢了整批请求都会变成
400 code 11128（见 fingerprint.py 开头）：
  1. 首条消息是 system（由 proxy._ensure_leading_system_message 保证）；
  2. X-Conversation-ID / -Request-ID / -Message-ID 三个头都在；
  3. User-Agent 自称官方客户端（WorkBuddy/<app>... 或 CLI/<cli> CodeBuddy/<cli>）。
"""

import buddy2api.fingerprint as fingerprint

ACCOUNT = {
    "access_token": "tok",
    "uid": "user-1",
    "domain": "www.codebuddy.cn",
    "enterprise_id": "ent-1",
}


def _messages(user="hello"):
    return [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": user},
    ]


def test_chat_headers_default_to_desktop_app_profile():
    fingerprint.begin_request(None)
    headers = fingerprint.chat_headers(ACCOUNT)
    assert headers["X-IDE-Type"] == "WorkBuddy"
    assert headers["X-IDE-Name"] == "WorkBuddy"
    assert headers["X-Private-Data"] == "true"
    assert headers["X-Agent-Purpose"] == "conversation"
    assert headers["X-Product"] == "SaaS"
    assert headers["User-Agent"].startswith("WorkBuddy/")
    assert " CLI/" in headers["User-Agent"]
    assert headers["Authorization"] == "Bearer tok"


def test_chat_headers_keep_channel_verification_headers():
    fingerprint.begin_request(None)
    headers = fingerprint.chat_headers(ACCOUNT)
    for name in fingerprint.CONVERSATION_HEADERS:
        assert headers.get(name), name
    assert headers["X-Request-ID"] == headers["X-Conversation-Message-ID"]
    assert headers["traceparent"].startswith("00-")
    assert headers["b3"].count("-") == 3


def test_chat_headers_drop_refresh_token_and_stainless():
    fingerprint.begin_request(None)
    headers = fingerprint.chat_headers(ACCOUNT)
    lowered = {k.lower() for k in headers}
    # 安全红线：chat 请求绝不携带 X-Refresh-Token
    assert "x-refresh-token" not in lowered
    assert "cosy-key" not in lowered
    # 官方客户端不发 x-stainless-*（那是别的 SDK 的痕迹），我们也不发
    assert not any(name.startswith("x-stainless") for name in lowered)


def test_cli_profile_keeps_old_identity():
    headers = fingerprint.chat_headers(ACCOUNT, channel=fingerprint.CHANNEL_CLI)
    assert headers["X-IDE-Type"] == "CLI"
    assert headers["X-IDE-Name"] == "CLI"
    assert headers["User-Agent"].startswith("CLI/")
    assert "CodeBuddy/" in headers["User-Agent"]
    assert "X-Agent-Purpose" not in headers
    # 三个会话头在 CLI 身份下同样必须在（渠道校验与身份无关）
    for name in fingerprint.CONVERSATION_HEADERS:
        assert headers.get(name), name


def test_conversation_id_is_stable_per_conversation():
    key = fingerprint.conversation_key(_messages("同一个话题"), 7)
    fingerprint.begin_request(key)
    first = fingerprint.chat_headers(ACCOUNT)
    # 第二轮：历史往后追加，会话指纹不变
    fingerprint.begin_request(fingerprint.conversation_key(
        _messages("同一个话题") + [{"role": "assistant", "content": "ok"},
                                   {"role": "user", "content": "继续"}],
        7,
    ))
    second = fingerprint.chat_headers(ACCOUNT)
    assert first["X-Conversation-ID"] == second["X-Conversation-ID"]
    assert first["X-Conversation-Message-ID"] != second["X-Conversation-Message-ID"]

    fingerprint.begin_request(fingerprint.conversation_key(_messages("另一个话题"), 7))
    other = fingerprint.chat_headers(ACCOUNT)
    assert other["X-Conversation-ID"] != first["X-Conversation-ID"]


def test_switch_channel_flips_profile_and_begin_request_resets_it():
    fingerprint.begin_request(None)
    assert fingerprint.channel_name() == fingerprint.CHANNEL_APP
    switched = fingerprint.switch_channel()
    assert switched == fingerprint.CHANNEL_CLI
    assert fingerprint.chat_headers(ACCOUNT)["X-IDE-Type"] == "CLI"
    fingerprint.begin_request(None)
    assert fingerprint.channel_name() == fingerprint.CHANNEL_APP


def test_refresh_headers_include_refresh_token():
    headers = fingerprint.refresh_headers(
        {
            "access_token": "tok",
            "refresh_token": "ref",
            "uid": "user-1",
            "domain": "www.codebuddy.cn",
        }
    )
    assert headers["X-Refresh-Token"] == "ref"
    assert headers["Authorization"] == "Bearer tok"
