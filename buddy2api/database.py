"""
database.py — SQLite 数据层

表结构：
  - accounts:   WorkBuddy 账号（auth 凭据）
  - api_keys:   客户端 API Key
  - logs:       请求日志
  - settings:   系统设置（key-value）
"""

import hashlib
import json
import sqlite3
import threading
import time
import os
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

import buddy2api.credential_crypto as credential_crypto
from buddy2api.paths import PROJECT_ROOT

DB_PATH = Path(os.environ.get("CB_GATEWAY_DB_PATH", PROJECT_ROOT / "codebuddy_gateway.db"))
_lock = threading.Lock()
_CREDENTIAL_FIELDS = ("access_token", "refresh_token", "session_state")


def _hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _key_prefix(key: str) -> str:
    if len(key) <= 16:
        return key[:6] + "..."
    return f"{key[:12]}...{key[-4:]}"


def _today_start_ts() -> int:
    now = time.localtime()
    return int(time.mktime((now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, now.tm_wday, now.tm_yday, now.tm_isdst)))


def _load_allowed_models(value: Any) -> Optional[list]:
    if not value:
        return None
    if isinstance(value, list):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def connection():
    conn = get_conn()
    try:
        yield conn
    finally:
        conn.close()


def _protect_account_data(data: dict) -> dict:
    protected = dict(data)
    for field in _CREDENTIAL_FIELDS:
        if field not in protected:
            continue
        value = protected.get(field)
        if isinstance(value, str) or value is None:
            protected[field] = credential_crypto.encrypt_secret(value, DB_PATH)
        else:
            # 启动路径必须对脏数据健壮：凭据字段混进非字符串（如上游格式
            # 变更漏进来的 dict）不能让整个服务崩溃，序列化后照常落库。
            protected[field] = credential_crypto.encrypt_secret(
                json.dumps(value, ensure_ascii=False, sort_keys=True), DB_PATH
            )
    return protected


def _account_dict(row: sqlite3.Row) -> dict:
    account = dict(row)
    for field in _CREDENTIAL_FIELDS:
        if field in account:
            account[field] = credential_crypto.decrypt_secret(account.get(field), DB_PATH)
    extra = account.get("extra")
    if isinstance(extra, str) and extra:
        try:
            account["extra"] = json.loads(extra)
        except (json.JSONDecodeError, TypeError):
            account["extra"] = {}
    elif extra is None:
        account["extra"] = {}
    if not account.get("provider"):
        account["provider"] = "workbuddy"
    return account


def init_db():
    with _lock:
        conn = get_conn()
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS accounts (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT NOT NULL,
            uid             TEXT,
            nickname        TEXT,
            phone           TEXT,
            account_type    TEXT DEFAULT 'personal',
            access_token    TEXT,
            refresh_token   TEXT,
            expires_at      INTEGER,
            refresh_expires_at INTEGER,
            domain          TEXT DEFAULT 'www.codebuddy.cn',
            enterprise_id   TEXT,
            session_state   TEXT,
            status          TEXT DEFAULT 'active',
            weight          INTEGER DEFAULT 1,
            priority        INTEGER DEFAULT 0,
            credit_limit    REAL DEFAULT 0,
            credit_baseline REAL DEFAULT 0,
            last_used_at    INTEGER,
            total_requests  INTEGER DEFAULT 0,
            total_tokens    INTEGER DEFAULT 0,
            total_credits   REAL DEFAULT 0,
            created_at      INTEGER,
            updated_at      INTEGER
        );

        CREATE TABLE IF NOT EXISTS api_keys (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            key_prefix      TEXT,
            key_hash        TEXT UNIQUE,
            key_secret      TEXT,
            name            TEXT,
            status          TEXT DEFAULT 'active',
            allowed_models  TEXT,
            daily_limit     INTEGER DEFAULT 0,
            client_type     TEXT DEFAULT 'custom',
            total_requests  INTEGER DEFAULT 0,
            total_tokens    INTEGER DEFAULT 0,
            created_at      INTEGER,
            last_used_at    INTEGER
        );

        CREATE TABLE IF NOT EXISTS logs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            api_key_id      INTEGER,
            api_key_name    TEXT,
            account_id      INTEGER,
            account_name    TEXT,
            model           TEXT,
            stream          INTEGER,
            prompt_tokens   INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            total_tokens    INTEGER DEFAULT 0,
            credit          REAL DEFAULT 0,
            cached_tokens   INTEGER DEFAULT 0,
            finish_reason   TEXT,
            duration_ms     INTEGER,
            status_code     INTEGER,
            error_msg       TEXT,
            created_at      INTEGER
        );

        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS account_resource_cache (
            account_id INTEGER PRIMARY KEY,
            payload    TEXT NOT NULL,
            updated_at INTEGER,
            FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS account_checkin_cache (
            account_id   INTEGER PRIMARY KEY,
            checkin_date TEXT,
            payload      TEXT NOT NULL,
            updated_at   INTEGER,
            FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_logs_created ON logs(created_at);
        CREATE INDEX IF NOT EXISTS idx_logs_api_key ON logs(api_key_id);
        CREATE INDEX IF NOT EXISTS idx_logs_account ON logs(account_id);
        CREATE INDEX IF NOT EXISTS idx_logs_model ON logs(model);
        CREATE INDEX IF NOT EXISTS idx_logs_status ON logs(status_code, finish_reason);
        CREATE INDEX IF NOT EXISTS idx_resource_cache_updated ON account_resource_cache(updated_at);
        CREATE INDEX IF NOT EXISTS idx_checkin_cache_date ON account_checkin_cache(checkin_date);
        """)
        _migrate_accounts(conn)
        _migrate_api_keys(conn)
        _migrate_logs_provider(conn)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS api_key_daily_usage (
                api_key_id    INTEGER NOT NULL,
                usage_date    TEXT NOT NULL,
                request_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(api_key_id, usage_date),
                FOREIGN KEY(api_key_id) REFERENCES api_keys(id) ON DELETE CASCADE
            )
        """)
        _migrate_account_credentials(conn)
        _migrate_daily_usage(conn)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash)")
        _prune_logs(conn)
        conn.execute("PRAGMA optimize")
        conn.commit()
        conn.close()


def _migrate_accounts(conn: sqlite3.Connection):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(accounts)").fetchall()}
    if "provider" not in cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN provider TEXT NOT NULL DEFAULT 'workbuddy'")
    if "extra" not in cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN extra TEXT")
    conn.execute("UPDATE accounts SET provider='workbuddy' WHERE provider IS NULL OR provider=''")
    _dedupe_accounts_provider_uid(conn)
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_provider_uid
            ON accounts(provider, uid)
            WHERE uid IS NOT NULL AND uid != ''
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_accounts_provider_status
            ON accounts(provider, status, priority, id)
        """
    )
    if "weight" not in cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN weight INTEGER DEFAULT 1")
    if "priority" not in cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN priority INTEGER DEFAULT 0")
    if "credit_limit" not in cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN credit_limit REAL DEFAULT 0")
    if "credit_baseline" not in cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN credit_baseline REAL DEFAULT 0")
        conn.execute("""
            UPDATE accounts
            SET credit_baseline=COALESCE(total_credits, 0)
            WHERE credit_limit IS NOT NULL AND credit_limit > 0
        """)
    conn.execute("UPDATE accounts SET weight=1 WHERE weight IS NULL OR weight < 1")
    conn.execute("UPDATE accounts SET priority=0 WHERE priority IS NULL")
    conn.execute("UPDATE accounts SET credit_limit=0 WHERE credit_limit IS NULL OR credit_limit < 0")
    conn.execute("UPDATE accounts SET credit_baseline=0 WHERE credit_baseline IS NULL OR credit_baseline < 0")


def _migrate_api_keys(conn: sqlite3.Connection):
    """Keep older plaintext-key databases usable while moving to hash-only storage."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(api_keys)").fetchall()}
    if "key" in cols:
        rows = conn.execute("SELECT * FROM api_keys ORDER BY id").fetchall()
        conn.execute("ALTER TABLE api_keys RENAME TO api_keys_legacy")
        conn.execute("""
        CREATE TABLE api_keys (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            key_prefix      TEXT,
            key_hash        TEXT UNIQUE,
            key_secret      TEXT,
            name            TEXT,
            status          TEXT DEFAULT 'active',
            allowed_models  TEXT,
            daily_limit     INTEGER DEFAULT 0,
            client_type     TEXT DEFAULT 'custom',
            total_requests  INTEGER DEFAULT 0,
            total_tokens    INTEGER DEFAULT 0,
            created_at      INTEGER,
            last_used_at    INTEGER
        )
        """)
        for row in rows:
            d = dict(row)
            raw_key = d.get("key") or ""
            conn.execute("""
                INSERT INTO api_keys
                    (id, key_prefix, key_hash, key_secret, name, status, allowed_models,
                     daily_limit, client_type, total_requests, total_tokens, created_at,
                     last_used_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                d.get("id"),
                d.get("key_prefix") or _key_prefix(raw_key),
                d.get("key_hash") or (_hash_api_key(raw_key) if raw_key else None),
                d.get("key_secret") or (
                    credential_crypto.encrypt_secret(raw_key, DB_PATH) if raw_key else None
                ),
                d.get("name", ""),
                d.get("status", "active"),
                d.get("allowed_models"),
                d.get("daily_limit") or 0,
                d.get("client_type") or "custom",
                d.get("total_requests") or 0,
                d.get("total_tokens") or 0,
                d.get("created_at"),
                d.get("last_used_at"),
            ))
        conn.execute("DROP TABLE api_keys_legacy")
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(api_keys)").fetchall()}

    if "key_prefix" not in cols:
        conn.execute("ALTER TABLE api_keys ADD COLUMN key_prefix TEXT")
    if "key_hash" not in cols:
        conn.execute("ALTER TABLE api_keys ADD COLUMN key_hash TEXT")
    if "key_secret" not in cols:
        conn.execute("ALTER TABLE api_keys ADD COLUMN key_secret TEXT")
    if "daily_limit" not in cols:
        conn.execute("ALTER TABLE api_keys ADD COLUMN daily_limit INTEGER DEFAULT 0")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(api_keys)").fetchall()}
    if "client_type" not in cols:
        conn.execute("ALTER TABLE api_keys ADD COLUMN client_type TEXT DEFAULT 'custom'")
    # 归一化历史数据：v1.4.0 之前 UI 允许落库 opencode/openclaw/cherry/nextchat，
    # 后端仅有 codex/custom 两种行为，其余一律归为 custom。
    conn.execute(
        "UPDATE api_keys SET client_type='custom' "
        "WHERE client_type IS NULL OR client_type NOT IN ('custom','codex')"
    )
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(api_keys)").fetchall()}
    if "default_channel" not in cols:
        conn.execute(
            "ALTER TABLE api_keys ADD COLUMN default_channel TEXT NOT NULL DEFAULT 'workbuddy'"
        )
    conn.execute(
        "UPDATE api_keys SET default_channel='workbuddy' "
        "WHERE default_channel IS NULL OR default_channel=''"
    )
    # API Key 级账号绑定：0 = 不绑定（走优先级 + 粘性调度），>0 = 只用该 accounts.id。
    # 与 default_channel 一样在这里补列：新建库与历史库都会经过这个迁移。
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(api_keys)").fetchall()}
    if "default_account" not in cols:
        conn.execute(
            "ALTER TABLE api_keys ADD COLUMN default_account INTEGER NOT NULL DEFAULT 0"
        )
    conn.execute(
        "UPDATE api_keys SET default_account=0 "
        "WHERE default_account IS NULL OR default_account < 0"
    )


def _dedupe_accounts_provider_uid(conn: sqlite3.Connection):
    dupes = conn.execute(
        """
        SELECT provider, uid, MIN(id) AS keep_id
        FROM accounts
        WHERE uid IS NOT NULL AND uid != ''
        GROUP BY provider, uid
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    for row in dupes:
        conn.execute(
            """
            DELETE FROM accounts
            WHERE provider=? AND uid=? AND id!=?
            """,
            (row["provider"], row["uid"], row["keep_id"]),
        )


def _migrate_logs_provider(conn: sqlite3.Connection):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(logs)").fetchall()}
    if "provider" not in cols:
        conn.execute("ALTER TABLE logs ADD COLUMN provider TEXT")
    if "cached_tokens" not in cols:
        conn.execute("ALTER TABLE logs ADD COLUMN cached_tokens INTEGER DEFAULT 0")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_provider ON logs(provider)")


def _migrate_account_credentials(conn: sqlite3.Connection):
    rows = conn.execute(
        "SELECT id, access_token, refresh_token, session_state FROM accounts"
    ).fetchall()
    for row in rows:
        updates = {}
        for field in _CREDENTIAL_FIELDS:
            value = row[field]
            if value and not credential_crypto.is_encrypted(value):
                updates[field] = credential_crypto.encrypt_secret(value, DB_PATH)
        if updates:
            fields = ", ".join(f"{field}=?" for field in updates)
            conn.execute(
                f"UPDATE accounts SET {fields} WHERE id=?",
                [*updates.values(), row["id"]],
            )


def _migrate_daily_usage(conn: sqlite3.Connection):
    # 只聚合仍然存在的 key：logs.api_key_id 没有外键约束，删 key 的历史遗留（或异常
    # 数据）会留下孤儿引用。这里的 INSERT 带外键目标表，孤儿一旦混入就是
    # IntegrityError —— 而 init_db 在启动路径上，等于一个坏行能把整个服务卡死
    # （2026-09-17 实测）。所以过滤条件必须留在 SQL 里，启动对脏数据健壮。
    conn.execute(
        """
        INSERT INTO api_key_daily_usage (api_key_id, usage_date, request_count)
        SELECT api_key_id, date(created_at, 'unixepoch', 'localtime'), COUNT(*)
        FROM logs
        WHERE api_key_id IS NOT NULL AND created_at >= ?
          AND api_key_id IN (SELECT id FROM api_keys)
        GROUP BY api_key_id, date(created_at, 'unixepoch', 'localtime')
        ON CONFLICT(api_key_id, usage_date) DO UPDATE SET
            request_count=MAX(api_key_daily_usage.request_count, excluded.request_count)
        """,
        (_today_start_ts(),),
    )


def _prune_logs(conn: sqlite3.Connection):
    try:
        retention_days = max(1, int(os.environ.get("CB_GATEWAY_LOG_RETENTION_DAYS", "90")))
    except ValueError:
        retention_days = 90
    cutoff_ts = int(time.time()) - retention_days * 86400
    cutoff_date = (date.today() - timedelta(days=retention_days)).isoformat()
    conn.execute("DELETE FROM logs WHERE created_at < ?", (cutoff_ts,))
    conn.execute("DELETE FROM api_key_daily_usage WHERE usage_date < ?", (cutoff_date,))


# ============================================================
# Accounts
# ============================================================

def add_account(data: dict) -> int:
    data = _protect_account_data(data)
    now = int(time.time())
    weight = max(1, int(data.get("weight", 1) or 1))
    priority = int(data.get("priority", 0) or 0)
    credit_limit = max(0.0, float(data.get("credit_limit", 0) or 0))
    credit_baseline = max(0.0, float(data.get("credit_baseline", 0) or 0))
    provider = str(data.get("provider") or "workbuddy").strip() or "workbuddy"
    extra = data.get("extra")
    if isinstance(extra, dict):
        extra_text = json.dumps(extra, ensure_ascii=False)
    elif extra is None:
        extra_text = None
    else:
        extra_text = str(extra)
    with _lock:
        conn = get_conn()
        cur = conn.execute("""
            INSERT INTO accounts
                (name, uid, nickname, phone, account_type, access_token, refresh_token,
                 expires_at, refresh_expires_at, domain, enterprise_id, session_state,
                 status, weight, priority, credit_limit, credit_baseline, provider, extra,
                 created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            data.get("name", ""),
            data.get("uid", ""),
            data.get("nickname", ""),
            data.get("phone", ""),
            data.get("account_type", "personal"),
            data.get("access_token", ""),
            data.get("refresh_token", ""),
            data.get("expires_at", 0),
            data.get("refresh_expires_at", 0),
            data.get("domain", "www.codebuddy.cn"),
            data.get("enterprise_id", ""),
            data.get("session_state", ""),
            data.get("status", "active"),
            weight,
            priority,
            credit_limit,
            credit_baseline,
            provider,
            extra_text,
            now, now,
        ))
        aid = cur.lastrowid
        conn.commit()
        conn.close()
        return aid


def update_account(aid: int, data: dict):
    data = _protect_account_data(data)
    now = int(time.time())
    fields = []
    values = []
    for k in ["name", "uid", "nickname", "phone", "account_type", "access_token",
              "refresh_token", "expires_at", "refresh_expires_at", "domain",
              "enterprise_id", "session_state", "status", "weight", "priority",
              "credit_limit", "credit_baseline", "provider", "extra"]:
        if k in data:
            if k == "weight":
                data[k] = max(1, int(data[k] or 1))
            elif k == "priority":
                data[k] = int(data[k] or 0)
            elif k in {"credit_limit", "credit_baseline"}:
                data[k] = max(0.0, float(data[k] or 0))
            elif k == "provider":
                data[k] = str(data[k] or "workbuddy").strip() or "workbuddy"
            elif k == "extra" and isinstance(data[k], dict):
                data[k] = json.dumps(data[k], ensure_ascii=False)
            fields.append(f"{k}=?")
            values.append(data[k])
    if not fields:
        return
    fields.append("updated_at=?")
    values.append(now)
    values.append(aid)
    with _lock:
        conn = get_conn()
        conn.execute(f"UPDATE accounts SET {','.join(fields)} WHERE id=?", values)
        conn.commit()
        conn.close()


def delete_account(aid: int):
    with _lock:
        conn = get_conn()
        conn.execute("DELETE FROM account_resource_cache WHERE account_id=?", (aid,))
        conn.execute("DELETE FROM account_checkin_cache WHERE account_id=?", (aid,))
        conn.execute("DELETE FROM accounts WHERE id=?", (aid,))
        conn.commit()
        conn.close()


def get_account(aid: int) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM accounts WHERE id=?", (aid,)).fetchone()
    conn.close()
    return _account_dict(row) if row else None


def list_accounts(*, provider: Optional[str] = None) -> list[dict]:
    conn = get_conn()
    if provider:
        rows = conn.execute(
            "SELECT * FROM accounts WHERE provider=? ORDER BY id",
            (provider,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()
    conn.close()
    return [_account_dict(r) for r in rows]


def get_active_accounts(provider: str = "workbuddy") -> list[dict]:
    if not provider:
        raise ValueError("get_active_accounts requires provider")
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT * FROM accounts
        WHERE status='active' AND provider=?
        ORDER BY priority DESC,
                 (CAST(total_requests AS REAL) / CASE WHEN weight > 0 THEN weight ELSE 1 END) ASC,
                 total_requests ASC,
                 id ASC
        """,
        (provider,),
    ).fetchall()
    conn.close()
    return [_account_dict(r) for r in rows]


def account_increment_usage(aid: int, tokens: int, credit: float):
    now = int(time.time())
    with _lock:
        conn = get_conn()
        conn.execute("""
            UPDATE accounts SET
                total_requests = total_requests + 1,
                total_tokens = total_tokens + ?,
                total_credits = total_credits + ?,
                last_used_at = ?,
                updated_at = ?
            WHERE id=?
        """, (tokens, credit, now, now, aid))
        conn.commit()
        conn.close()


# ============================================================
# Account cache
# ============================================================

def upsert_account_resource_cache(account_id: int, payload: dict):
    now = int(time.time())
    safe_payload = dict(payload or {})
    safe_payload["cached"] = False
    safe_payload["stale"] = False
    safe_payload["updated_at"] = int(safe_payload.get("updated_at") or now)
    with _lock:
        conn = get_conn()
        conn.execute(
            """
            INSERT INTO account_resource_cache (account_id, payload, updated_at)
            VALUES (?,?,?)
            ON CONFLICT(account_id) DO UPDATE SET
                payload=excluded.payload,
                updated_at=excluded.updated_at
            """,
            (account_id, json.dumps(safe_payload, ensure_ascii=False), safe_payload["updated_at"]),
        )
        conn.commit()
        conn.close()


def get_account_resource_cache(account_id: int) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute(
        "SELECT payload, updated_at FROM account_resource_cache WHERE account_id=?",
        (account_id,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    try:
        payload = json.loads(row["payload"] or "{}")
    except (json.JSONDecodeError, TypeError):
        return None
    payload["cached"] = True
    payload["cache_updated_at"] = int(row["updated_at"] or payload.get("updated_at") or 0)
    payload["age_seconds"] = max(0, int(time.time()) - int(payload["cache_updated_at"] or 0))
    return payload


def upsert_account_checkin_cache(account_id: int, payload: dict):
    now = int(time.time())
    checkin_date = date.today().isoformat()
    safe_payload = dict(payload or {})
    safe_payload["cached"] = False
    safe_payload["stale"] = False
    safe_payload["updated_at"] = int(safe_payload.get("updated_at") or now)
    with _lock:
        conn = get_conn()
        conn.execute(
            """
            INSERT INTO account_checkin_cache (account_id, checkin_date, payload, updated_at)
            VALUES (?,?,?,?)
            ON CONFLICT(account_id) DO UPDATE SET
                checkin_date=excluded.checkin_date,
                payload=excluded.payload,
                updated_at=excluded.updated_at
            """,
            (account_id, checkin_date, json.dumps(safe_payload, ensure_ascii=False), safe_payload["updated_at"]),
        )
        conn.commit()
        conn.close()


def get_account_checkin_cache(account_id: int, today_only: bool = True) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute(
        "SELECT checkin_date, payload, updated_at FROM account_checkin_cache WHERE account_id=?",
        (account_id,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    if today_only and row["checkin_date"] != date.today().isoformat():
        return None
    try:
        payload = json.loads(row["payload"] or "{}")
    except (json.JSONDecodeError, TypeError):
        return None
    payload["cached"] = True
    payload["cache_date"] = row["checkin_date"]
    payload["cache_updated_at"] = int(row["updated_at"] or payload.get("updated_at") or 0)
    payload["age_seconds"] = max(0, int(time.time()) - int(payload["cache_updated_at"] or 0))
    return payload


# ============================================================
# API Keys
# ============================================================

def add_api_key(key: str, name: str, allowed_models: Optional[list] = None,
                daily_limit: Optional[int] = None, client_type: str = "custom",
                default_channel: str = "workbuddy",
                default_account: Optional[int] = None) -> int:
    now = int(time.time())
    models_json = json.dumps(allowed_models) if allowed_models else None
    limit = int(daily_limit or 0)
    channel = str(default_channel or "workbuddy").strip() or "workbuddy"
    account_id = max(0, int(default_account or 0))
    with _lock:
        conn = get_conn()
        cur = conn.execute("""
            INSERT INTO api_keys
                (key_prefix, key_hash, key_secret, name, status, allowed_models,
                 daily_limit, client_type, default_channel, default_account, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (
            _key_prefix(key),
            _hash_api_key(key),
            credential_crypto.encrypt_secret(key, DB_PATH),
            name,
            "active",
            models_json,
            limit,
            client_type,
            channel,
            account_id,
            now,
        ))
        kid = cur.lastrowid
        conn.commit()
        conn.close()
        return kid


def update_api_key(kid: int, data: dict):
    fields = []
    values = []
    for k in ["name", "status", "allowed_models", "daily_limit", "client_type",
              "default_channel", "default_account"]:
        if k in data:
            val = data[k]
            if k == "allowed_models" and isinstance(val, list):
                val = json.dumps(val) if val else None
            if k == "default_account":
                # 写入侧已由 server._validate_key_account 严格校验；这里只做兜底归一，
                # 保证任何调用方都写不进负数。
                val = max(0, int(val or 0))
            fields.append(f"{k}=?")
            values.append(val)
    if not fields:
        return
    values.append(kid)
    with _lock:
        conn = get_conn()
        conn.execute(f"UPDATE api_keys SET {','.join(fields)} WHERE id=?", values)
        conn.commit()
        conn.close()


def delete_api_key(kid: int):
    with _lock:
        conn = get_conn()
        # 先摘掉引用再删 key：logs 保留原文（只去掉归属），daily_usage 行随之失去意义、
        # 一并删除。否则留下孤儿 api_key_id，下次重启 _migrate_daily_usage 聚合 logs 时
        # 会撞 api_key_daily_usage 的外键，直接把整个服务卡死在启动阶段（2026-09-17 实测）。
        conn.execute("UPDATE logs SET api_key_id=NULL WHERE api_key_id=?", (kid,))
        conn.execute("DELETE FROM api_key_daily_usage WHERE api_key_id=?", (kid,))
        conn.execute("DELETE FROM api_keys WHERE id=?", (kid,))
        conn.commit()
        conn.close()


def get_api_key_by_key(key: str) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM api_keys WHERE key_hash=? AND status='active'", (_hash_api_key(key),)).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    d.pop("key_hash", None)
    d.pop("key_secret", None)
    d.pop("key", None)
    d["allowed_models"] = _load_allowed_models(d.get("allowed_models"))
    return d


def list_api_keys(*, include_secret: bool = False) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT k.*, COALESCE(u.request_count, 0) AS today_requests
        FROM api_keys AS k
        LEFT JOIN api_key_daily_usage AS u
          ON u.api_key_id=k.id AND u.usage_date=?
        ORDER BY k.id DESC
        """,
        (date.today().isoformat(),),
    ).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        d.pop("key_hash", None)
        encrypted_key = d.pop("key_secret", None)
        d.pop("key", None)
        if include_secret:
            d["key"] = (
                credential_crypto.decrypt_secret(encrypted_key, DB_PATH)
                if encrypted_key else None
            )
        d["allowed_models"] = _load_allowed_models(d.get("allowed_models"))
        result.append(d)
    return result


def get_api_key_daily_requests(kid: int) -> int:
    conn = get_conn()
    row = conn.execute(
        "SELECT request_count AS c FROM api_key_daily_usage WHERE api_key_id=? AND usage_date=?",
        (kid, date.today().isoformat()),
    ).fetchone()
    conn.close()
    return int(row["c"] if row else 0)


def reserve_api_key_request(kid: int, daily_limit: int) -> bool:
    """Atomically reserve one daily request slot for an API key."""
    today = date.today().isoformat()
    with _lock:
        with connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT request_count FROM api_key_daily_usage WHERE api_key_id=? AND usage_date=?",
                (kid, today),
            ).fetchone()
            current = int(row["request_count"] if row else 0)
            if daily_limit > 0 and current >= daily_limit:
                conn.rollback()
                return False
            conn.execute(
                """
                INSERT INTO api_key_daily_usage (api_key_id, usage_date, request_count)
                VALUES (?, ?, 1)
                ON CONFLICT(api_key_id, usage_date) DO UPDATE SET
                    request_count=request_count + 1
                """,
                (kid, today),
            )
            conn.commit()
            return True


def api_key_increment_usage(kid: int, tokens: int):
    now = int(time.time())
    with _lock:
        conn = get_conn()
        conn.execute("""
            UPDATE api_keys SET
                total_requests = total_requests + 1,
                total_tokens = total_tokens + ?,
                last_used_at = ?
            WHERE id=?
        """, (tokens, now, kid))
        conn.commit()
        conn.close()


# 选路用的「近期负载」窗口。
#
# `accounts.total_requests` 是终身累计值且只增不减，拿它当负载信号会让历史欠债永远
# 追不平：线上实测两个国际账号水位 1110 / 754，差值远大于粘性容差 1.0，于是「少的
# 先用」退化成「永远只用计数较低的那个」，看起来就像固定路由到一个账号。窗口只看
# 最近一段时间内实际接了多少请求，才是真正可均衡、能自我纠正的信号。
# 15 分钟：足够覆盖一次突发，又短到让空闲账号迅速回到同一水位。
ROUTE_WINDOW_SECONDS = int(os.environ.get("CB_GATEWAY_ROUTE_WINDOW_SECONDS", "900"))

# 质量口径：一次客户端请求可能被上游拒绝后换账号重试，每次失败尝试都会落一行 log
# （status_code=429/401/502、finish_reason='retry'，见 proxy.py 的重试循环）。这些行是
# **中间过程**而非最终结果 —— 后续尝试往往已经成功返回给客户端。若把它们计入
# errors，成功率和错误数会严重偏离用户实际体验（实测 391 行 retry 中 387 行随后成功）。
# 因此质量统计一律排除 retry 行；retry 行本身仍保留在 logs 表里，可供排障时查看。
_SQL_IS_ERROR = "status_code < 200 OR status_code >= 300 OR finish_reason='error'"
_SQL_NOT_RETRY = "finish_reason IS NOT 'retry'"


def recent_account_loads(window_seconds: Optional[int] = None) -> dict[int, int]:
    """各账号在最近 window_seconds 内成功服务的请求数。

    只统计 2xx（真的服务了的），失败/被拒/重打的尝试不算负载：那类请求没有占用上游
    的生成额度，算进来会让一个正在被上游拒的账号显得「很忙」而被绕开。
    """
    window = ROUTE_WINDOW_SECONDS if window_seconds is None else max(0, int(window_seconds))
    since = int(time.time()) - window
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT account_id, COUNT(*) AS n FROM logs
            WHERE created_at >= ? AND account_id IS NOT NULL
              AND status_code BETWEEN 200 AND 299
            GROUP BY account_id
            """,
            (since,),
        ).fetchall()
    finally:
        conn.close()
    return {int(row["account_id"]): int(row["n"]) for row in rows}


def observed_site_costs(window_days: int = 30) -> dict[str, dict[str, dict]]:
    """从历史请求日志里统计「每个模型在每个站点实际被扣了多少费」。

    同一个模型在两个站点的计费可以完全不同（线上实测 deepseek-v4.1-flash 国际站
    1750 次全部 credit=0，国内站 497 次里 485 次扣费、累计 167.66），而 glm-5.3
    反过来在国际站收费。所以「免费」是 (模型 × 站点) 的属性，只能从实测数据里学，
    不能按站点一刀切。

    返回 {模型: {站点分组: {"requests": n, "paid": k, "credit": 累计扣费}}}。
    站点分组用 sites.site_group 判定，与路由侧共用同一份定义。
    """
    since = int(time.time()) - max(1, int(window_days)) * 86400
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT l.model AS model, a.domain AS domain, l.credit AS credit
            FROM logs l JOIN accounts a ON a.id = l.account_id
            WHERE l.created_at >= ? AND l.account_id IS NOT NULL
              AND l.status_code BETWEEN 200 AND 299
              AND l.model IS NOT NULL AND l.model != ''
            """,
            (since,),
        ).fetchall()
    finally:
        conn.close()

    import buddy2api.sites as sites

    profile: dict[str, dict[str, dict]] = {}
    for row in rows:
        model = str(row["model"] or "").strip()
        if not model:
            continue
        group = sites.site_group(row["domain"])
        credit = float(row["credit"] or 0)
        bucket = profile.setdefault(model, {}).setdefault(
            group, {"requests": 0, "paid": 0, "credit": 0.0}
        )
        bucket["requests"] += 1
        bucket["credit"] += credit
        if credit > 0:
            bucket["paid"] += 1
    return profile


def model_site_usage(window_days: int = 90) -> dict[str, set[str]]:
    """从成功请求日志里反证「这个模型在哪个站点能服务」。

    上游的目录接口（/v2/enterprises/personal/models）只是官方客户端的推荐清单，
    并不等于实际可服务范围：实测国际站目录里没有 deepseek-v4.1-flash，但国际站
    账号打它 24 小时内成功 2641 次且全部免费。所以站点归属除了目录还要看实测。

    返回 {裸模型 id: {站点分组}}；带通道前缀的日志（workbuddy/xxx）会归一化到裸 id。
    """
    since = int(time.time()) - max(1, int(window_days)) * 86400
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT l.model AS model, a.domain AS domain
            FROM logs l JOIN accounts a ON a.id = l.account_id
            WHERE l.created_at >= ? AND l.account_id IS NOT NULL
              AND l.status_code BETWEEN 200 AND 299
              AND l.model IS NOT NULL AND l.model != ''
            GROUP BY l.model, a.domain
            """,
            (since,),
        ).fetchall()
    finally:
        conn.close()
    usage: dict[str, set[str]] = {}
    for row in rows:
        model = str(row["model"] or "").strip().split("/", 1)[-1]
        domain = row["domain"]
        if not model or not domain:
            continue
        import buddy2api.sites as sites

        usage.setdefault(model, set()).add(sites.site_group(domain))
    return usage


def model_credit_totals() -> dict[str, dict]:
    """按模型统计累计积分消耗（全部日志里的成功请求）。

    模型目录页要用它显示「这个模型一共花了多少积分」，所以必须每次现算：日志一直在写，
    缓存住就等于把「刷新」按钮变成摆设。查询只做一次 GROUP BY，实测与
    observed_site_costs 同量级，可以直接放在目录接口里。

    返回 {模型: {"credit": 累计扣费, "requests": 成功次数}}。
    """
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT model AS model,
                   COALESCE(SUM(credit), 0) AS credit,
                   COUNT(*) AS requests
            FROM logs
            WHERE status_code BETWEEN 200 AND 299 AND model IS NOT NULL AND model != ''
            GROUP BY model
            """
        ).fetchall()
    finally:
        conn.close()
    totals: dict[str, dict] = {}
    for row in rows:
        model = str(row["model"] or "").strip()
        if not model:
            continue
        totals[model] = {
            "credit": round(float(row["credit"] or 0), 4),
            "requests": int(row["requests"] or 0),
        }
    return totals


def reset_account_request_counts() -> int:
    """把全部账号的终身请求计数归零，返回受影响行数。

    选路已改用 `recent_account_loads` 的滑动窗口，不再依赖这个终身累计值，所以归零
    对路由没有影响 —— 它现在只是「清掉展示用的累计统计」。保留是因为管理页把它当作
    计数器重置入口；`total_tokens` / `total_credits` 不动，避免弄丢「累计已用」。
    """
    with _lock:
        conn = get_conn()
        cur = conn.execute("UPDATE accounts SET total_requests = 0")
        conn.commit()
        affected = cur.rowcount
        conn.close()
        return affected


# ============================================================
# Logs
# ============================================================

def add_log(data: dict):
    now = int(time.time())
    with _lock:
        conn = get_conn()
        conn.execute("""
            INSERT INTO logs
                (api_key_id, api_key_name, account_id, account_name, model, stream,
                 prompt_tokens, completion_tokens, total_tokens, credit, cached_tokens,
                 finish_reason, duration_ms, status_code, error_msg, provider, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            data.get("api_key_id"), data.get("api_key_name"),
            data.get("account_id"), data.get("account_name"),
            data.get("model", ""), data.get("stream", 0),
            data.get("prompt_tokens", 0), data.get("completion_tokens", 0),
            data.get("total_tokens", 0), data.get("credit", 0),
            data.get("cached_tokens", 0),
            data.get("finish_reason", ""), data.get("duration_ms", 0),
            data.get("status_code", 200), data.get("error_msg", ""),
            data.get("provider") or "workbuddy",
            now,
        ))
        conn.commit()
        conn.close()


def record_request(data: dict):
    """Write a request log and update account/key counters in one transaction."""
    now = int(time.time())
    with _lock:
        with connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO logs
                    (api_key_id, api_key_name, account_id, account_name, model, stream,
                     prompt_tokens, completion_tokens, total_tokens, credit, cached_tokens,
                     finish_reason, duration_ms, status_code, error_msg, provider, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    data.get("api_key_id"), data.get("api_key_name"),
                    data.get("account_id"), data.get("account_name"),
                    data.get("model", ""), data.get("stream", 0),
                    data.get("prompt_tokens", 0), data.get("completion_tokens", 0),
                    data.get("total_tokens", 0), data.get("credit", 0),
                    data.get("cached_tokens", 0),
                    data.get("finish_reason", ""), data.get("duration_ms", 0),
                    data.get("status_code", 200), data.get("error_msg", ""),
                    data.get("provider") or "workbuddy", now,
                ),
            )
            if data.get("account_id") and data.get("increment_usage", True):
                conn.execute(
                    """
                    UPDATE accounts SET
                        total_requests=total_requests + 1,
                        total_tokens=total_tokens + ?,
                        total_credits=total_credits + ?,
                        last_used_at=?, updated_at=?
                    WHERE id=?
                    """,
                    (
                        data.get("total_tokens", 0), data.get("credit", 0),
                        now, now, data["account_id"],
                    ),
                )
            if data.get("api_key_id") and data.get("increment_usage", True):
                conn.execute(
                    """
                    UPDATE api_keys SET
                        total_requests=total_requests + 1,
                        total_tokens=total_tokens + ?,
                        last_used_at=?
                    WHERE id=?
                    """,
                    (data.get("total_tokens", 0), now, data["api_key_id"]),
                )
            conn.commit()


def prune_logs(retention_days: int | None = None) -> int:
    """Delete expired request logs and return the number of removed rows."""
    if retention_days is None:
        try:
            retention_days = int(os.environ.get("CB_GATEWAY_LOG_RETENTION_DAYS", "90"))
        except ValueError:
            retention_days = 90
    retention_days = max(1, retention_days)
    cutoff_ts = int(time.time()) - retention_days * 86400
    cutoff_date = (date.today() - timedelta(days=retention_days)).isoformat()
    with _lock:
        with connection() as conn:
            cursor = conn.execute("DELETE FROM logs WHERE created_at < ?", (cutoff_ts,))
            conn.execute("DELETE FROM api_key_daily_usage WHERE usage_date < ?", (cutoff_date,))
            conn.commit()
            return max(0, cursor.rowcount)


def list_logs(limit: int = 100, offset: int = 0) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM logs ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def search_logs(filters: Optional[dict] = None) -> dict:
    filters = filters or {}
    limit = max(1, min(500, int(filters.get("limit") or 100)))
    offset = max(0, int(filters.get("offset") or 0))
    where = []
    values: list[Any] = []

    q = str(filters.get("q") or "").strip()
    if q:
        like = f"%{q}%"
        where.append(
            "(api_key_name LIKE ? OR account_name LIKE ? OR model LIKE ? OR finish_reason LIKE ? OR error_msg LIKE ?)"
        )
        values.extend([like, like, like, like, like])

    status = str(filters.get("status") or "all").strip()
    if status == "success":
        where.append("status_code BETWEEN 200 AND 299 AND finish_reason NOT IN ('error', 'content_filter')")
    elif status == "error":
        where.append("(status_code < 200 OR status_code >= 300 OR finish_reason='error')")
    elif status == "filtered":
        where.append("finish_reason='content_filter'")

    for key, col in (
        ("api_key_id", "api_key_id"),
        ("account_id", "account_id"),
    ):
        value = filters.get(key)
        if value not in (None, "", "all"):
            where.append(f"{col}=?")
            values.append(int(value))

    model = str(filters.get("model") or "").strip()
    if model:
        where.append("model=?")
        values.append(model)

    start = filters.get("start")
    if start not in (None, "", "all"):
        where.append("created_at>=?")
        values.append(int(start))

    end = filters.get("end")
    if end not in (None, "", "all"):
        where.append("created_at<=?")
        values.append(int(end))

    sql_where = (" WHERE " + " AND ".join(where)) if where else ""
    conn = get_conn()
    total = conn.execute(f"SELECT COUNT(*) AS c FROM logs{sql_where}", values).fetchone()["c"]
    rows = conn.execute(
        f"SELECT * FROM logs{sql_where} ORDER BY id DESC LIMIT ? OFFSET ?",
        [*values, limit, offset],
    ).fetchall()
    model_rows = conn.execute(
        "SELECT DISTINCT model FROM logs WHERE model IS NOT NULL AND model!='' ORDER BY model LIMIT 200"
    ).fetchall()
    conn.close()
    return {
        "items": [dict(r) for r in rows],
        "total": int(total or 0),
        "limit": limit,
        "offset": offset,
        "models": [r["model"] for r in model_rows],
    }


def get_stats() -> dict:
    conn = get_conn()
    # total_requests 是成功率的分母，必须与 success/errors 同口径（排除 retry 中间尝试），
    # 否则 9 次重试后成功 1 次会被算成 10% 成功率，而不是用户视角的 100%。
    total_requests = conn.execute(
        f"SELECT COUNT(*) as c FROM logs WHERE {_SQL_NOT_RETRY}"
    ).fetchone()["c"]
    total_tokens = conn.execute("SELECT COALESCE(SUM(total_tokens),0) as s FROM logs").fetchone()["s"]
    total_credit = conn.execute("SELECT COALESCE(SUM(credit),0) as s FROM logs").fetchone()["s"]
    success_requests = conn.execute("""
        SELECT COUNT(*) as c FROM logs
        WHERE status_code BETWEEN 200 AND 299
          AND finish_reason NOT IN ('error', 'content_filter')
    """).fetchone()["c"]
    error_requests = conn.execute(
        f"SELECT COUNT(*) as c FROM logs WHERE ({_SQL_IS_ERROR}) AND {_SQL_NOT_RETRY}"
    ).fetchone()["c"]
    filtered_requests = conn.execute("SELECT COUNT(*) as c FROM logs WHERE finish_reason='content_filter'").fetchone()["c"]
    avg_duration_ms = conn.execute("SELECT COALESCE(AVG(duration_ms),0) as v FROM logs WHERE duration_ms IS NOT NULL").fetchone()["v"]
    active_accounts = conn.execute("SELECT COUNT(*) as c FROM accounts WHERE status='active'").fetchone()["c"]
    total_accounts = conn.execute("SELECT COUNT(*) as c FROM accounts").fetchone()["c"]
    active_keys = conn.execute("SELECT COUNT(*) as c FROM api_keys WHERE status='active'").fetchone()["c"]
    total_keys = conn.execute("SELECT COUNT(*) as c FROM api_keys").fetchone()["c"]

    today_start = _today_start_ts()
    today = conn.execute(f"""
        SELECT COUNT(*) as requests,
               COALESCE(SUM(total_tokens),0) as tokens,
               COALESCE(SUM(credit),0) as credit,
               COALESCE(AVG(duration_ms),0) as avg_duration_ms,
               COALESCE(SUM(prompt_tokens),0) as prompt_tokens,
               COALESCE(SUM(cached_tokens),0) as cached_tokens
        FROM logs WHERE created_at >= ? AND {_SQL_NOT_RETRY}
    """, (today_start,)).fetchone()
    today_success = conn.execute("""
        SELECT COUNT(*) as c FROM logs
        WHERE created_at >= ?
          AND status_code BETWEEN 200 AND 299
          AND finish_reason NOT IN ('error', 'content_filter')
    """, (today_start,)).fetchone()["c"]
    today_errors = conn.execute(f"""
        SELECT COUNT(*) as c FROM logs
        WHERE created_at >= ? AND ({_SQL_IS_ERROR}) AND {_SQL_NOT_RETRY}
    """, (today_start,)).fetchone()["c"]
    today_filtered = conn.execute(
        "SELECT COUNT(*) as c FROM logs WHERE created_at >= ? AND finish_reason='content_filter'",
        (today_start,),
    ).fetchone()["c"]

    hourly_rows = conn.execute("""
        SELECT CAST(strftime('%H', created_at, 'unixepoch', 'localtime') AS INTEGER) as hour,
               COUNT(*) as requests,
               COALESCE(SUM(total_tokens), 0) as tokens,
               COALESCE(SUM(credit), 0) as credit,
               COALESCE(SUM(prompt_tokens), 0) as prompt_tokens,
               COALESCE(SUM(cached_tokens), 0) as cached_tokens
        FROM logs WHERE created_at >= ?
        GROUP BY hour ORDER BY hour
    """, (today_start,)).fetchall()
    hourly_by_hour = {int(r["hour"]): dict(r) for r in hourly_rows}
    hourly = []
    for hour in range(24):
        row = hourly_by_hour.get(hour, {})
        hourly_prompt = int(row.get("prompt_tokens") or 0)
        hourly_cached = int(row.get("cached_tokens") or 0)
        hourly.append({
            "hour": hour,
            "label": f"{hour:02d}:00",
            "requests": int(row.get("requests") or 0),
            "tokens": int(row.get("tokens") or 0),
            "credit": round(float(row.get("credit") or 0), 4),
            "prompt_tokens": hourly_prompt,
            "cached_tokens": hourly_cached,
            "cache_rate": round(hourly_cached / hourly_prompt * 100, 2) if hourly_prompt else 0.0,
        })

    # 最近 7 个自然日每日统计，补齐 0 值日期，避免图表只显示一根柱子。
    seven_days_ago = _today_start_ts() - 6 * 86400
    daily_rows = conn.execute("""
        SELECT date(created_at, 'unixepoch', 'localtime') as date,
               COUNT(*) as requests,
               COALESCE(SUM(total_tokens), 0) as tokens,
               COALESCE(SUM(credit), 0) as credits
        FROM logs WHERE created_at >= ?
        GROUP BY date ORDER BY date
    """, (seven_days_ago,)).fetchall()
    daily_by_date = {r["date"]: dict(r) for r in daily_rows}
    today_date = date.today()
    daily = []
    for i in range(6, -1, -1):
        day = (today_date - timedelta(days=i)).isoformat()
        daily.append(daily_by_date.get(day, {
            "date": day,
            "requests": 0,
            "tokens": 0,
            "credits": 0,
        }))

    # 模型使用统计
    model_stats = conn.execute("""
        SELECT model, COUNT(*) as count, COALESCE(SUM(total_tokens),0) as tokens,
               COALESCE(SUM(credit),0) as credit,
               COALESCE(AVG(duration_ms),0) as avg_duration_ms
        FROM logs GROUP BY model ORDER BY count DESC LIMIT 10
    """).fetchall()

    key_stats = conn.execute("""
        SELECT api_key_name as name, COUNT(*) as count, COALESCE(SUM(total_tokens),0) as tokens,
               COALESCE(SUM(credit),0) as credit, MAX(created_at) as last_used_at
        FROM logs
        WHERE api_key_id IS NOT NULL
        GROUP BY api_key_id, api_key_name
        ORDER BY count DESC LIMIT 5
    """).fetchall()

    account_stats = conn.execute("""
        SELECT id, name, nickname, status, total_requests, total_tokens, total_credits, last_used_at
        FROM accounts
        ORDER BY status='active' DESC, total_requests DESC, id ASC
        LIMIT 5
    """).fetchall()

    recent_logs = conn.execute("""
        SELECT id, api_key_name, account_name, model, stream, total_tokens, credit,
               finish_reason, duration_ms, status_code, error_msg, created_at
        FROM logs ORDER BY id DESC LIMIT 8
    """).fetchall()

    conn.close()
    return {
        "total_requests": total_requests,
        "total_tokens": total_tokens,
        "total_credit": round(total_credit, 4),
        "success_requests": success_requests,
        "error_requests": error_requests,
        "filtered_requests": filtered_requests,
        "success_rate": round((success_requests / total_requests * 100) if total_requests else 0, 2),
        "avg_duration_ms": int(avg_duration_ms or 0),
        "today": {
            "requests": int(today["requests"] or 0),
            "tokens": int(today["tokens"] or 0),
            "credit": round(float(today["credit"] or 0), 4),
            "success": int(today_success or 0),
            "errors": int(today_errors or 0),
            "filtered": int(today_filtered or 0),
            "success_rate": round((today_success / today["requests"] * 100) if today["requests"] else 0, 2),
            "avg_duration_ms": int(today["avg_duration_ms"] or 0),
            "cached_tokens": int(today["cached_tokens"] or 0),
            "cache_rate": round(
                (int(today["cached_tokens"] or 0) / int(today["prompt_tokens"] or 0) * 100)
                if int(today["prompt_tokens"] or 0) else 0.0, 2
            ),
            "hourly": hourly,
        },
        "active_accounts": active_accounts,
        "total_accounts": total_accounts,
        "active_keys": active_keys,
        "total_keys": total_keys,
        "daily": daily,
        "model_stats": [dict(r) for r in model_stats],
        "key_stats": [dict(r) for r in key_stats],
        "account_stats": [dict(r) for r in account_stats],
        "recent_logs": [dict(r) for r in recent_logs],
    }


# ============================================================
# Settings
# ============================================================

def get_setting(key: str, default: Any = None) -> Any:
    conn = get_conn()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    if row is None:
        return default
    val = row["value"]
    try:
        return json.loads(val)
    except (json.JSONDecodeError, TypeError):
        return val


def set_setting(key: str, value: Any):
    val = json.dumps(value) if not isinstance(value, str) else value
    with _lock:
        conn = get_conn()
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, val),
        )
        conn.commit()
        conn.close()


def get_all_settings() -> dict:
    conn = get_conn()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    conn.close()
    result = {}
    for r in rows:
        try:
            result[r["key"]] = json.loads(r["value"])
        except (json.JSONDecodeError, TypeError):
            result[r["key"]] = r["value"]
    return result
