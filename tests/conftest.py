"""全局测试隔离。

任何测试都不应该写到仓库里的 `codebuddy_gateway.db`。此前 `tests/test_core.py` 里
`_collect_non_stream_upstream` 这类辅助函数既没有屏蔽 `proxy._log_request`、也没有隔离
DB 路径，于是每跑一次测试就往真实库的 `logs` 表写一条 `account_name='test-account'`、
`duration_ms` 约等于 epoch 毫秒（因为传了 `t0=0`）的脏记录，污染管理页的请求日志与统计。

这里统一把 `db.DB_PATH` 指向每个测试自己的临时目录，并初始化表结构；测试文件里已有的
`isolated_db` fixture 用的是同一个 `tmp_path`，因此不会冲突。
"""

import pytest

import buddy2api.credential_crypto as credential_crypto
import buddy2api.database as db
import buddy2api.upstream_transport as upstream_transport


@pytest.fixture(autouse=True)
def _isolate_gateway_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "gateway.db")
    monkeypatch.setenv("CB_GATEWAY_MASTER_KEY", "pytest-master-key")
    credential_crypto.reset_cache()
    db.init_db()
    yield
    credential_crypto.reset_cache()


def patch_upstream_client(monkeypatch, client):
    """把出站客户端换成假客户端。

    proxy / auth_manager 现在都通过 `upstream_transport.client_scope` 拿**共享**客户端
    （复用的 keep-alive 连接，见 upstream_transport.shared_client），所以替换点统一在
    `upstream_transport.shared_client`，而不是各自的 `httpx.AsyncClient`。
    """
    monkeypatch.setattr(upstream_transport, "shared_client", lambda *args, **kwargs: client)
    return client
