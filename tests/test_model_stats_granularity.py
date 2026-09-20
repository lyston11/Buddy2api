"""模型占比统计粒度：`@后缀` 账号别名必须归一到物理模型，并附账号分布。

背景（2026-09-20）：为做账号隔离，settings.model_aliases 手工注册了
`deepseek-v4.1-flash@team-buka` 这类 `@后缀` 别名（都映射到同一物理模型）。
「模型占比」原先直接 GROUP BY logs.model，有两个粒度问题：
  1. 同一物理模型被拆成多行（裸名 + 各 @别名），占比失真；
  2. 裸名行聚合了共享路由的全部流量，看不出落在哪些账号 —— 用户反馈权
     "deepseek-v4.1-flash 没有@某个账号"。
"""

import buddy2api.database as db


def _register_aliases(mapping: dict):
    db.set_setting("model_aliases", mapping)


def _add(model, account_id, account_name, **kw):
    db.add_log({
        "model": model,
        "account_id": account_id,
        "account_name": account_name,
        "total_tokens": 10,
        "finish_reason": "stop",
        "status_code": 200,
        **kw,
    })


def test_at_suffix_aliases_merge_into_one_model():
    """@team-buka/@xiaoyao 等别名必须与裸名合并成同一物理模型的一行。"""
    _register_aliases({
        "deepseek-v4.1-flash@team-buka": "deepseek-v4.1-flash",
        "deepseek-v4.1-flash@xiaoyao": "deepseek-v4.1-flash",
    })
    _add("deepseek-v4.1-flash", 1, "buka")
    _add("deepseek-v4.1-flash@team-buka", 5, "m8ksd0g7g9xu")
    _add("deepseek-v4.1-flash@team-buka", 5, "m8ksd0g7g9xu")
    _add("deepseek-v4.1-flash@xiaoyao", 2, "xiaoyaoaiqima")

    rows = {r["model"]: r for r in db.get_stats()["model_stats"]}
    assert set(rows) == {"deepseek-v4.1-flash"}, f"应合并为一行，实际: {list(rows)}"
    assert rows["deepseek-v4.1-flash"]["count"] == 4
    assert rows["deepseek-v4.1-flash"]["tokens"] == 40


def test_model_row_carries_account_breakdown():
    """裸名行必须能下钻看账号分布，按请求数降序。"""
    _register_aliases({})
    _add("deepseek-v4.1-flash", 1, "buka")
    for _ in range(3):
        _add("deepseek-v4.1-flash", 2, "xiaoyaoaiqima")

    row = next(r for r in db.get_stats()["model_stats"] if r["model"] == "deepseek-v4.1-flash")
    accounts = row["accounts"]
    assert [a["name"] for a in accounts] == ["xiaoyaoaiqima", "buka"]
    assert [a["count"] for a in accounts] == [3, 1]
    assert accounts[0]["id"] == 2


def test_unregistered_at_suffix_falls_back_to_prefix():
    """没注册别名但带 @ 的名字按运维约定剥离后缀，而不是单独成行。"""
    _register_aliases({})
    _add("deepseek-v4.1-flash", 1, "buka")
    _add("deepseek-v4.1-flash@ghost", 9, "ghost")

    rows = {r["model"]: r for r in db.get_stats()["model_stats"]}
    assert set(rows) == {"deepseek-v4.1-flash"}
    assert rows["deepseek-v4.1-flash"]["count"] == 2
    assert {a["name"] for a in rows["deepseek-v4.1-flash"]["accounts"]} == {"buka", "ghost"}


def test_plain_aliases_not_folded():
    """普通兼容别名（gpt-4o → glm-5.2）不参与归一，流量仍单独可见。"""
    _register_aliases({})
    _add("gpt-4o", 1, "buka")
    _add("glm-5.2", 1, "buka")

    rows = {r["model"]: r for r in db.get_stats()["model_stats"]}
    assert set(rows) == {"gpt-4o", "glm-5.2"}


def test_avg_duration_merged_weighted_by_count():
    """合并后平均耗时必须按请求数加权，各组 AVG 不能直接相加。"""
    _register_aliases({"deepseek-v4.1-flash@team-buka": "deepseek-v4.1-flash"})
    _add("deepseek-v4.1-flash", 1, "buka", duration_ms=100)
    _add("deepseek-v4.1-flash@team-buka", 5, "m8ksd0g7g9xu", duration_ms=300)
    _add("deepseek-v4.1-flash@team-buka", 5, "m8ksd0g7g9xu", duration_ms=300)

    row = next(r for r in db.get_stats()["model_stats"] if r["model"] == "deepseek-v4.1-flash")
    # (100*1 + 300*2) / 3 ≈ 233，而不是 100+300=400
    assert row["avg_duration_ms"] == 233


def test_account_rename_uses_latest_name(monkeypatch):
    """同一账号改名后，分布里取最近一次请求用的名字。"""
    monkeypatch.setattr(db.time, "time", lambda: 1000)
    db.add_log({"model": "glm-5.2", "account_id": 1, "account_name": "old-name",
                "finish_reason": "stop", "status_code": 200})
    monkeypatch.setattr(db.time, "time", lambda: 2000)
    db.add_log({"model": "glm-5.2", "account_id": 1, "account_name": "new-name",
                "finish_reason": "stop", "status_code": 200})

    row = next(r for r in db.get_stats()["model_stats"] if r["model"] == "glm-5.2")
    assert len(row["accounts"]) == 1, "同一 account_id 不得因改名拆成两行"
    assert row["accounts"][0]["name"] == "new-name"


def test_rows_without_account_attribution_flagged():
    """account_id/name 都缺失的日志归入「未知账号」，暴露归属缺口而不是静默吞掉。"""
    db.add_log({"model": "glm-5.2", "finish_reason": "stop", "status_code": 200})

    row = next(r for r in db.get_stats()["model_stats"] if r["model"] == "glm-5.2")
    assert row["accounts"][0]["id"] is None
    assert row["accounts"][0]["name"] == "未知账号"
    assert row["accounts"][0]["count"] == 1
    assert row["accounts"][0]["last_used_at"] > 0
