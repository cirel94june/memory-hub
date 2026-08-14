"""
MemoryMaintenanceDecision 测试。

覆盖：
- _map_relation_to_action 映射
- _is_auto_executable 自动执行规则
- _execute_maintenance_action 各动作执行
- info_type 持久化
- maintenance_audit 写入
- _write_audit 审计记录

运行：ALLOW_DEFAULT_HUB_SECRET=1 python -m pytest tests/test_maintenance_decision.py -q
"""
import os
import sys
import json
import asyncio
from datetime import datetime, timezone

os.environ.setdefault("ALLOW_DEFAULT_HUB_SECRET", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import database
import memory_ops
from memory_ops import (
    _map_relation_to_action, _is_auto_executable,
    _execute_maintenance_action, _write_audit,
)


# ── _map_relation_to_action ──

def test_unrelated_maps_to_create():
    rel = {"relation": "unrelated", "should_supersede": False}
    assert _map_relation_to_action(rel, "ai_summary", {}) == "create"


def test_same_topic_high_confidence_maps_to_annotate():
    rel = {"relation": "same_topic", "should_supersede": False, "confidence": 0.8}
    assert _map_relation_to_action(rel, "ai_summary", {}) == "annotate"


def test_same_topic_low_confidence_maps_to_no_change():
    rel = {"relation": "same_topic", "should_supersede": False, "confidence": 0.5}
    assert _map_relation_to_action(rel, "ai_summary", {}) == "no_change"


def test_supplements_maps_to_supplement():
    rel = {"relation": "supplements", "should_supersede": False}
    assert _map_relation_to_action(rel, "user_statement", {}) == "supplement"


def test_updates_supersede_user_maps_to_supersede():
    rel = {"relation": "updates", "should_supersede": True}
    assert _map_relation_to_action(rel, "user_statement", {}) == "supersede"


def test_updates_supersede_ai_maps_to_update():
    rel = {"relation": "updates", "should_supersede": True}
    assert _map_relation_to_action(rel, "ai_summary", {}) == "update"


def test_contradicts_supersede_ai_maps_to_correct():
    rel = {"relation": "contradicts", "should_supersede": True}
    assert _map_relation_to_action(rel, "ai_summary", {}) == "correct"


def test_contradicts_supersede_user_maps_to_supersede():
    rel = {"relation": "contradicts", "should_supersede": True}
    assert _map_relation_to_action(rel, "user_statement", {}) == "supersede"


def test_updates_no_supersede_maps_to_annotate():
    rel = {"relation": "updates", "should_supersede": False}
    assert _map_relation_to_action(rel, "user_statement", {}) == "annotate"


def test_contradicts_no_supersede_maps_to_annotate():
    rel = {"relation": "contradicts", "should_supersede": False}
    assert _map_relation_to_action(rel, "ai_summary", {}) == "annotate"


# ── _is_auto_executable ──

def test_no_change_always_auto():
    assert _is_auto_executable("no_change", "ai_summary") is True


def test_annotate_always_auto():
    assert _is_auto_executable("annotate", "ai_summary") is True


def test_supplement_always_auto():
    assert _is_auto_executable("supplement", "ai_summary") is True


def test_resolve_thread_always_auto():
    assert _is_auto_executable("resolve_thread", "ai_summary") is True


def test_supersede_user_auto():
    assert _is_auto_executable("supersede", "user_statement") is True


def test_supersede_ai_not_auto():
    assert _is_auto_executable("supersede", "ai_summary") is False


def test_update_ai_not_auto():
    assert _is_auto_executable("update", "ai_summary") is False


def test_correct_never_auto():
    assert _is_auto_executable("correct", "user_statement") is False


def test_create_never_auto():
    assert _is_auto_executable("create", "user_statement") is False


def test_reopen_thread_never_auto():
    assert _is_auto_executable("reopen_thread", "user_statement") is False


# ── _execute_maintenance_action ──

@pytest.fixture
def fake_env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    database.DB_PATH = tmp_path / "test.db"
    asyncio.run(database.init_db(db_path))

    import github_store
    mems = {}
    monkeypatch.setattr(github_store, "get_all_memories", lambda: mems)
    monkeypatch.setattr(github_store, "get_memory", lambda mid: mems.get(mid))

    def fake_set(m):
        mems[m["id"]] = m
        database.set_memory(m)
    monkeypatch.setattr(github_store, "set_memory", fake_set)
    monkeypatch.setattr(memory_ops, "store", github_store)

    async def fake_embed(text):
        import hashlib
        h = hashlib.md5(text.encode()).hexdigest()
        vec = [int(c, 16) / 15.0 for c in h] * 64
        return vec[:1024]
    monkeypatch.setattr(memory_ops, "get_embedding", fake_embed)
    monkeypatch.setattr("memory_ops.pack_embedding", lambda v: b"\x00" * 4096 if v else None)

    async def fake_analyze(text):
        return {"tags": ["test"], "suggested_category": "test", "domain": ["general"], "valence": 0.5, "arousal": 0.3}
    monkeypatch.setattr(memory_ops.analyzer, "analyze", fake_analyze)

    yield mems


def _make_memory(mems, mem_id="m1", content="小猫住在北京", room="living_room", **extra):
    now = datetime.now(timezone.utc).isoformat()
    mem = {
        "id": mem_id, "content": content, "layer": "shared",
        "room": room, "category": "", "owner_ai": "",
        "importance": 0.5, "emotion_arousal": 0.3, "valence": 0.5,
        "domain": "[]", "decay_score": 1.0, "activation_count": 0,
        "last_activated": "", "source_ai": "claude", "source_platform": "",
        "tags": "[]", "linked_memories": "[]", "supersedes": "[]",
        "superseded_by": "", "event_date": "", "source_context": "",
        "comments": [], "embedding": None, "status": "active",
        "created_at": now, "updated_at": now, "history": [],
        "resolved": None, "anchored": None, "provenance_type": "user_statement",
        "fact_confidence": 0.9, "subject_id": "", "source_actor_id": "",
        "info_type": "fact",
        **extra,
    }
    mems[mem_id] = mem
    return mem


def test_execute_no_change(fake_env):
    mem = _make_memory(fake_env)
    result = asyncio.run(_execute_maintenance_action(
        "no_change", mem, "新内容", "无新信息", "claude",
    ))
    assert result["status"] == "no_change"
    assert result["maintenance_action"] == "no_change"


def test_execute_annotate(fake_env):
    mem = _make_memory(fake_env)
    result = asyncio.run(_execute_maintenance_action(
        "annotate", mem, "一些补充", "同话题补注", "claude",
    ))
    assert result["status"] == "annotated"
    updated = fake_env["m1"]
    assert len(updated["comments"]) == 1
    assert updated["comments"][0]["kind"] == "annotation"


def test_execute_supplement(fake_env):
    mem = _make_memory(fake_env)
    result = asyncio.run(_execute_maintenance_action(
        "supplement", mem, "详细补充", "添加细节", "claude",
    ))
    assert result["status"] == "supplemented"
    updated = fake_env["m1"]
    assert len(updated["comments"]) == 1
    assert updated["comments"][0]["kind"] == "supplement"


def test_execute_resolve_thread(fake_env):
    # Seed real DB too — the atomic maintenance helper writes there directly
    mem = _make_memory(fake_env, resolved=0, info_type="task")
    database.set_memory(mem)
    result = asyncio.run(_execute_maintenance_action(
        "resolve_thread", mem, "搞定了", "待办完成", "claude",
    ))
    assert result["status"] == "resolved"
    # H4: read from DB (source of truth after the atomic tx)
    updated = database.get_memory("m1")
    assert updated["resolved"] == 1


def test_execute_supersede_user_provenance(fake_env):
    mem = _make_memory(fake_env, content="小猫住在北京", provenance_type="user_statement")
    database.set_memory(mem)
    result = asyncio.run(_execute_maintenance_action(
        "supersede", mem, "小猫搬到上海了", "状态更新",
        "claude", "user_statement",
    ))
    assert result["status"] == "superseded"
    assert result["superseded_id"] == "m1"
    # H4: DB is source of truth
    assert database.get_memory("m1")["status"] == "superseded"


def test_execute_supersede_blocked_by_provenance(fake_env):
    mem = _make_memory(fake_env, content="小猫住在北京", provenance_type="user_statement")
    result = asyncio.run(_execute_maintenance_action(
        "supersede", mem, "AI觉得小猫搬家了", "AI推测",
        "claude", "ai_summary",
    ))
    assert result is None


# ── info_type 持久化 ──

def test_info_type_stored_in_memory(fake_env):
    result = asyncio.run(memory_ops.remember(
        content="小猫养了一只布偶猫叫团团",
        room="living_room",
        source_ai="claude",
        info_type="identity",
        quick=False,
    ))
    assert result.get("status") == "created"
    mem = fake_env.get(result["id"])
    assert mem is not None
    assert mem["info_type"] == "identity"


def test_info_type_defaults_to_fact(fake_env):
    result = asyncio.run(memory_ops.remember(
        content="今天天气真好",
        room="living_room",
        source_ai="claude",
        quick=False,
    ))
    mem = fake_env.get(result["id"])
    assert mem["info_type"] == "fact"


# ── maintenance_audit 写入 ──

def test_write_audit_creates_record(fake_env):
    _write_audit("annotate", "m1", "new stuff", "testing", {}, {}, True, "claude")
    audits = database.list_audits(action="annotate")
    assert len(audits) >= 1
    audit = audits[0]
    assert audit["action"] == "annotate"
    assert audit["target_id"] == "m1"
    assert audit["auto_executed"] == 1


def test_audit_count(fake_env):
    _write_audit("supplement", "m2", "details", "reason", {}, {}, True, "claude")
    _write_audit("no_change", "m3", "nope", "reason", {}, {}, True, "claude")
    assert database.count_audits() >= 2
    assert database.count_audits(action="supplement") >= 1


def test_execute_reopen_thread(fake_env):
    mem = _make_memory(fake_env, resolved=1, info_type="task")
    database.set_memory(mem)
    result = asyncio.run(_execute_maintenance_action(
        "reopen_thread", mem, "又出问题了", "待办重开", "claude",
    ))
    assert result["status"] == "reopened"
    # H4: DB is source of truth
    updated = database.get_memory("m1")
    assert updated["resolved"] == 0


def test_write_audit_with_model_and_version(fake_env):
    _write_audit("annotate", "m1", "stuff", "reason", {}, {}, True, "claude",
                 model_id="claude-3.5-sonnet", prompt_version="v1.2")
    audits = database.list_audits(action="annotate")
    assert len(audits) >= 1
    assert audits[0]["model_id"] == "claude-3.5-sonnet"
    assert audits[0]["prompt_version"] == "v1.2"


def test_write_audit_raises_on_failure(fake_env, monkeypatch):
    monkeypatch.setattr(database, "insert_audit", lambda row: (_ for _ in ()).throw(RuntimeError("db error")))
    with pytest.raises(RuntimeError, match="db error"):
        _write_audit("annotate", "m1", "stuff", "reason", {}, {}, True, "claude")


def test_h4_supersede_and_audit_atomic_rollback(fake_env, monkeypatch):
    """H4 regression: injecting an INSERT-audit failure via
    commit_maintenance_atomic must roll back the memory UPDATE so the memory
    is NOT left superseded. sqlite3.Connection.execute is C-level and can't
    be monkeypatched — wrap the connection instead."""
    mem = _make_memory(fake_env, content="小猫住在北京",
                       provenance_type="user_statement")
    database.set_memory(mem)
    assert database.get_memory("m1")["status"] == "active"

    real_conn = database._get_conn()

    class FailingConn:
        def __init__(self, wrapped): self._c = wrapped
        def execute(self, sql, *args, **kwargs):
            if "INSERT INTO maintenance_audit" in sql:
                raise RuntimeError("audit insert boom")
            return self._c.execute(sql, *args, **kwargs)
        def __getattr__(self, name): return getattr(self._c, name)

    monkeypatch.setattr(database, "_get_conn",
                        lambda: FailingConn(real_conn))
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(_execute_maintenance_action(
            "supersede", mem, "小猫搬到上海了", "状态更新",
            "claude", "user_statement",
        ))
    # Restore real conn to check state
    monkeypatch.undo()
    assert database.get_memory("m1")["status"] == "active", \
        "H4 regressed: memory left superseded despite audit failure"


def test_state_before_stores_full_content(fake_env):
    long_content = "这是一段很长的记忆内容" * 50
    mem = _make_memory(fake_env, content=long_content)
    asyncio.run(_execute_maintenance_action(
        "annotate", mem, "补充", "测试完整存储", "claude",
    ))
    audits = database.list_audits(action="annotate")
    audit = audits[0]
    state = json.loads(audit["state_before"])
    assert state["content"] == long_content
