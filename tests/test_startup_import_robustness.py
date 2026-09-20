"""启动导入路径对 auth 文件格式变更的健壮性。

事故（2026-09-20）：WorkBuddy 桌面端把 auth 文件的 accessToken/refreshToken
从明文字符串改成了 {"$wbEncrypted": .., "envelope": ..} 加密信封。启动自动导入
把信封 dict 原样透传到 _protect_account_data，encrypt_secret 对 dict 调
.startswith 直接 AttributeError —— 服务崩溃循环，模型路由全断。

钉死两道防线：
1. parse_auth_file 遇到解不开的信封按无凭据跳过，绝不覆盖库里可用令牌；
2. _protect_account_data 遇到非字符串凭据序列化落库，不再崩溃。
"""

import json
from pathlib import Path

import buddy2api.auth_manager as auth_manager
import buddy2api.database as db


def _write_auth(path: Path, access_token, refresh_token="rt-string"):
    payload = {
        "account": {"uid": "uid-1", "nickname": "tester"},
        "auth": {"accessToken": access_token, "refreshToken": refresh_token,
                 "expiresAt": 123, "sessionState": "ss"},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_parse_auth_file_skips_encrypted_envelope(tmp_path):
    """信封结构的 token 解不开，必须返回 None 跳过，不能透传 dict。"""
    p = _write_auth(tmp_path / "a.info", {"$wbEncrypted": 1, "envelope": "xxx"})
    assert auth_manager.parse_auth_file(p) is None


def test_parse_auth_file_skips_envelope_refresh_token(tmp_path):
    """只有 refreshToken 是信封同样跳过 —— 否则加密路径照样崩。"""
    p = _write_auth(tmp_path / "b.info", "at-string",
                    refresh_token={"$wbEncrypted": 1, "envelope": "xxx"})
    assert auth_manager.parse_auth_file(p) is None


def test_parse_auth_file_still_accepts_string_tokens(tmp_path):
    """旧格式（明文字符串）必须照常解析，不能被防线误伤。"""
    p = _write_auth(tmp_path / "c.info", "at-string")
    parsed = auth_manager.parse_auth_file(p)
    assert parsed is not None
    assert parsed["access_token"] == "at-string"
    assert parsed["refresh_token"] == "rt-string"
    assert parsed["session_state"] == "ss"


def test_update_account_with_dict_credential_does_not_crash():
    """凭据字段混入 dict 不得崩溃（2026-09-20 启动崩溃循环的病灶）。"""
    aid = db.add_account({"name": "robust", "access_token": "plain-token"})
    assert aid
    db.update_account(aid, {"access_token": {"$wbEncrypted": 1, "envelope": "xxx"}})

    account = db.get_account(aid)
    # 落库内容被序列化成 JSON 字符串：服务活着，且不会把 dict 塞进 TEXT 列
    assert isinstance(account["access_token"], str)
    assert "$wbEncrypted" in account["access_token"]
