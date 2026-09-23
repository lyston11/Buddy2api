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
import buddy2api.control_plane as control_plane
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


# ------------------------------------------------------------
# 时间戳备份快照（2026-09-20：auth 目录里 16 个文件中 13 个是备份，
# 扫描面板看起来"账号爆炸"；更糟的是按 uid 导入会用旧 token 降级覆盖）
# ------------------------------------------------------------

def _write_snapshot(path: Path, uid: str, access_token: str):
    path.write_text(json.dumps({
        "account": {"uid": uid, "nickname": "snap"},
        "auth": {"accessToken": access_token, "refreshToken": "rt",
                 "expiresAt": 1, "sessionState": "ss"},
    }), encoding="utf-8")


def test_is_backup_auth_file_matches_real_snapshots():
    """真实快照文件名必须命中，活跃文件不得误伤。"""
    assert auth_manager.is_backup_auth_file(
        "workbuddy-desktop.2026-09-12T16-22-52-595Z.43534.c1e1c986-62ab-4d8e-9ab4-9f8d4bde6a01.info")
    assert auth_manager.is_backup_auth_file(
        "workbuddy-desktop-ai.2026-09-14T10-31-16-054Z.33125.577d210d-6547-44e8-bc03-0625843a2d02.info")
    assert auth_manager.is_backup_auth_file(
        "workbuddy-desktop.2026-08-20T08-10-43-049Z.50371.f7db3101-f0ce-4696-862e-aef6e29316d2.info")
    assert not auth_manager.is_backup_auth_file("workbuddy-desktop.info")
    assert not auth_manager.is_backup_auth_file("workbuddy-desktop-ai.info")
    assert not auth_manager.is_backup_auth_file("Tencent-Cloud.coding-copilot.info")
    assert not auth_manager.is_backup_auth_file("")


def test_discover_meta_flags_backup(tmp_path):
    p = tmp_path / "workbuddy-desktop.2026-09-12T16-22-52-595Z.43534.c1e1c986-62ab-4d8e-9ab4-9f8d4bde6a01.info"
    _write_snapshot(p, "uid-x", "tok")
    meta = auth_manager._safe_auth_file_meta(p, {"uid-x"})
    assert meta["is_backup"] is True
    assert meta["already_imported"] is True


def test_auto_scan_skips_backup_snapshots(tmp_path):
    """手动扫描（POST /admin/accounts/scan）不得用备份旧 token 覆盖现有账号。"""
    aid = db.add_account({"name": "buka", "uid": "uid-x", "access_token": "current-token"})
    backup = tmp_path / "workbuddy-desktop.2026-09-12T16-22-52-595Z.43534.c1e1c986-62ab-4d8e-9ab4-9f8d4bde6a01.info"
    _write_snapshot(backup, "uid-x", "stale-backup-token")
    live = tmp_path / "workbuddy-desktop-ai.info"
    _write_snapshot(live, "uid-y", "fresh-live-token")

    result = auth_manager.auto_scan_and_import(str(tmp_path))

    assert result["skipped"] == 1, "备份快照应计为跳过"
    assert db.get_account(aid)["access_token"] == "current-token", "备份旧 token 不得覆盖现有账号"
    imported = [a for a in db.list_accounts() if a.get("uid") == "uid-y"]
    assert len(imported) == 1 and imported[0]["access_token"] == "fresh-live-token"


def test_startup_import_skips_backup_snapshots(tmp_path):
    """启动自动导入（import_workbuddy）同样必须拦截备份快照。"""
    aid = db.add_account({"name": "buka", "uid": "uid-x", "access_token": "current-token"})
    backup = tmp_path / "workbuddy-desktop.2026-09-12T16-22-52-595Z.43534.c1e1c986-62ab-4d8e-9ab4-9f8d4bde6a01.info"
    _write_snapshot(backup, "uid-x", "stale-backup-token")

    files = [{"path": str(backup)}]
    token = control_plane.issue_preview("workbuddy", files)
    result = control_plane.import_workbuddy([str(backup)], control_plane.lookup_preview(token, "workbuddy"), str(tmp_path))

    assert result["skipped"] == 1 and result["imported"] == 0 and result["updated"] == 0
    assert db.get_account(aid)["access_token"] == "current-token"


# ------------------------------------------------------------
# 加密信封识别 + 按账号聚合的凭据来源视图（2026-09-20：
# 「本机登录检测」把文件列表当成了账号列表，16 个文件看起来像 16 个账号）
# ------------------------------------------------------------

def _write_envelope(path: Path, uid: str):
    """写一份新版客户端格式：token 为 $wbEncrypted 信封，uid 仍明文。"""
    path.write_text(json.dumps({
        "account": {"uid": uid, "nickname": {"$wbEncrypted": 1, "envelope": "x"}},
        "auth": {"accessToken": {"$wbEncrypted": 1, "envelope": "abc"},
                 "refreshToken": {"$wbEncrypted": 1, "envelope": "def"},
                 "expiresAt": 1, "sessionState": "ss"},
    }), encoding="utf-8")


def test_is_encrypted_field_wrapper():
    assert auth_manager.is_encrypted_field_wrapper({"$wbEncrypted": 1, "envelope": "abc"})
    assert not auth_manager.is_encrypted_field_wrapper("plain-token")
    assert not auth_manager.is_encrypted_field_wrapper({"$wbEncrypted": 1})
    assert not auth_manager.is_encrypted_field_wrapper(None)
    assert not auth_manager.is_encrypted_field_wrapper({"other": 1, "envelope": "abc"})


def test_meta_flags_envelope_as_not_importable(tmp_path):
    p = tmp_path / "workbuddy-desktop.info"
    _write_envelope(p, "uid-env")

    meta = auth_manager._safe_auth_file_meta(p, set())

    assert meta["encrypted"] is True
    assert "无感登录" in meta["reason"], "面板要能说清「为什么不能导入」"
    assert meta["uid"] == "uid-env", "信封文件的 uid 仍是明文，聚合要靠它"


def test_discover_accounts_view_maps_accounts_to_sources(tmp_path):
    """每行一个账号：本机文件 / 仅数据库 / 信封，备份数挂到账号下。"""
    db.add_account({"name": "with-file", "uid": "uid-a", "access_token": "tok-a"})
    db.add_account({"name": "db-only", "uid": "uid-b", "access_token": "tok-b"})
    db.add_account({"name": "enveloped", "uid": "uid-c", "access_token": "tok-c"})
    _write_snapshot(tmp_path / "workbuddy-desktop-ai.info", "uid-a", "tok-a")
    _write_snapshot(tmp_path / "workbuddy-desktop.2026-09-12T16-22-52-595Z.43534.c1e1c986-62ab-4d8e-9ab4-9f8d4bde6a01.info", "uid-a", "old")
    _write_envelope(tmp_path / "workbuddy-desktop.info", "uid-c")

    disc = auth_manager.discover_auth_files(str(tmp_path))
    rows = {r["name"]: r for r in disc["accounts"]}

    assert rows["with-file"]["source"] == "file"
    assert rows["with-file"]["live_files"] == ["workbuddy-desktop-ai.info"]
    assert rows["with-file"]["backup_count"] == 1
    assert rows["db-only"]["source"] == "db" and rows["db-only"]["live_files"] == []
    assert rows["enveloped"]["source"] == "encrypted"
    assert rows["enveloped"]["live_files"] == ["workbuddy-desktop.info"]
    assert disc["importable_count"] == 0, "信封文件不可导入"


def test_discover_accounts_view_lists_unimported_uid(tmp_path):
    """本机有文件、库里没账号的 uid 必须单列，否则看不到「可导入」。"""
    _write_snapshot(tmp_path / "workbuddy-desktop-ai.info", "uid-new", "fresh")

    disc = auth_manager.discover_auth_files(str(tmp_path))
    new_rows = [r for r in disc["accounts"] if r["source"] == "unimported"]

    assert len(new_rows) == 1 and new_rows[0]["id"] is None
    assert new_rows[0]["importable_files"] == ["workbuddy-desktop-ai.info"]
    assert disc["importable_count"] == 1


# ------------------------------------------------------------
# 凭据来源视图只覆盖 WorkBuddy（2026-09-21）
# ------------------------------------------------------------

def test_discover_credential_sources_only_list_workbuddy_accounts(tmp_path):
    """发现面板只扫 WorkBuddy 的 *.info，账号视图也必须只列 WorkBuddy。

    回归：原实现用 db.list_accounts()（全部通道），QClaw / TraeWork 账号会
    出现在「本机凭据导入」面板里并被标成「仅数据库 · 刷新续期」，看起来
    像是本机凭据丢了。
    """
    db.add_account({"name": "wb", "uid": "u-wb", "access_token": "t",
                    "domain": "www.workbuddy.cn"})
    db.add_account({"name": "qclaw", "uid": "u-qc", "access_token": "t", "provider": "qclaw"})
    db.add_account({"name": "traework", "uid": "u-tw", "access_token": "t", "provider": "traework"})

    view = auth_manager.discover_auth_files(str(tmp_path / "no-such-auth-dir"))

    providers = {row["provider"] for row in view["accounts"]}
    assert providers == {"workbuddy"}, f"只应列出 WorkBuddy 账号，实际: {providers}"
    assert [row["name"] for row in view["accounts"]] == ["wb"]


def test_uidless_file_is_not_claimed_by_account_without_uid(tmp_path):
    """自审修复（2026-09-23）：uid 为空的文件不得被 uid 为空的账号认领。

    原实现按 `meta["uid"] or ""` 归桶，于是「uid 为空的账号」会把所有缺 uid 的
    文件都算成自己的凭据来源（实测误报 source=file），同时这些文件也不会出现在
    「待导入」里 —— 既误报又漏报。
    """
    db.add_account({"name": "account-without-uid", "uid": "", "status": "active",
                    "access_token": "t"})
    _write_snapshot(tmp_path / "workbuddy-desktop-ai.info", "", "plain-token")

    rows = auth_manager.discover_auth_files(str(tmp_path))["accounts"]
    owner = next(r for r in rows if r["name"] == "account-without-uid")
    orphans = [r for r in rows if r["id"] is None]

    assert owner["source"] == "db" and owner["live_files"] == [], "缺 uid 的文件不得归给该账号"
    assert len(orphans) == 1, "缺 uid 的文件应单列为待导入，而不是消失"
    assert orphans[0]["live_files"] == ["workbuddy-desktop-ai.info"]
    assert "uid" in orphans[0]["name"]
