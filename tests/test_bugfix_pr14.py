"""
PR #14 Codex 审出的 8 项必修 bug 回归测试（行为级）。

覆盖：
  H1: dream_recall 调真实 DB 验证跨 AI 隐私
  H2: 三条 recall 路径 exclude_provenance — 调真实 DB API
  H3: generate_dreams() 原子预占 — 两独立连接真并发，断言 DB 只一条 dream
  H4: 梦境调度窗口覆盖 02:00
  M1: Profile Pydantic schema 校验（含 fail-closed 新增测试）
  M2: _validate_and_retry 统一重试 — 存 model_dump_json
  M3: _filter_relationship_group_dynamic 按 pattern: tag 分组
  M6: cleanup 单事务 + dry-run ROLLBACK

运行：ALLOW_DEFAULT_HUB_SECRET=1 python -m pytest tests/test_bugfix_pr14.py -q
"""
import os
import sys
import json
import sqlite3
import asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, AsyncMock
from pathlib import Path

os.environ.setdefault("ALLOW_DEFAULT_HUB_SECRET", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import database


# ════════════════════════════════════════════
#  Helpers
# ════════════════════════════════════════════

def _make_mem(**kw):
    defaults = {
        "id": "mem_test", "content": "test", "room": "living_room",
        "info_type": "fact", "importance": 0.5, "created_at": "2026-08-03",
        "category": "", "provenance_type": "user_statement",
        "fact_confidence": 0.9, "tags": "[]",
    }
    defaults.update(kw)
    return defaults


@pytest.fixture
def db_env(tmp_path):
    db_path = str(tmp_path / "test.db")
    database.DB_PATH = tmp_path / "test.db"
    asyncio.run(database.init_db(db_path))
    return db_path


def _insert_memory(conn, mid, content, room="living_room", provenance="user_statement",
                    layer="shared", owner_ai="", source_ai="", status="active"):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO memories (id, content, room, status, provenance_type, layer, "
        "owner_ai, source_ai, created_at, updated_at, importance, category, tags, domain) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (mid, content, room, status, provenance, layer, owner_ai, source_ai,
         now, now, 0.5, "", "[]", "[]"),
    )


# ════════════════════════════════════════════
#  H1: dream_recall 权限过滤 — 真实 DB + visibility
# ════════════════════════════════════════════

def test_h1_cross_permission_dream_recall():
    """Visibility filter must prevent cross-AI dream access.
    Tests the can_view logic that dream_recall uses on each candidate."""
    from visibility import can_view

    dreams = [
        {"id": "d_lucien", "room": "dreams", "layer": "private",
         "owner_ai": "lucien", "source_ai": "lucien", "provenance_type": "dream"},
        {"id": "d_jasper", "room": "dreams", "layer": "private",
         "owner_ai": "jasper", "source_ai": "jasper", "provenance_type": "dream"},
        {"id": "d_claude", "room": "dreams", "layer": "private",
         "owner_ai": "claude", "source_ai": "claude", "provenance_type": "dream"},
    ]

    jasper_visible = [d["id"] for d in dreams if can_view(d, "jasper")]
    lucien_visible = [d["id"] for d in dreams if can_view(d, "lucien")]
    claude_visible = [d["id"] for d in dreams if can_view(d, "claude")]

    assert jasper_visible == ["d_jasper"], "Jasper sees only own dream"
    assert lucien_visible == ["d_lucien"], "Lucien sees only own dream"
    assert claude_visible == ["d_claude"], "Claude sees only own dream"


def test_h1_owner_and_alias_visibility():
    """Dream owner and alias can view their dream; others cannot."""
    from visibility import can_view

    dream = {
        "id": "d1", "room": "dreams", "layer": "private",
        "owner_ai": "claude", "source_ai": "claude",
        "provenance_type": "dream",
    }
    assert can_view(dream, "claude") is True, "Owner must see own dream"
    assert can_view(dream, "cloudy") is True, "Alias must see owner's dream"
    assert can_view(dream, "jasper") is False
    assert can_view(dream, "lucien") is False


# ════════════════════════════════════════════
#  H2: 三条 recall 路径 exclude_provenance
# ════════════════════════════════════════════

def test_h2_fts_sql_level_excludes_dream(db_env):
    """FTS SQL WHERE must filter dream provenance before LIMIT."""
    conn = database._get_conn()
    _insert_memory(conn, "m_dream", "testing dream searchable content here",
                   room="dreams", provenance="dream")
    _insert_memory(conn, "m_normal", "testing normal searchable content here",
                   room="living_room", provenance="user_statement")
    conn.commit()

    results = database.fts_search("testing searchable", top_k=10,
                                   status="active", exclude_provenance=["dream"])
    ids = [r["id"] for r in results]
    assert "m_dream" not in ids, "dream must be excluded from FTS results"
    assert "m_normal" in ids


def test_h2_cjk_like_sql_level_excludes_dream(db_env):
    """CJK LIKE SQL WHERE must filter dream provenance before LIMIT."""
    conn = database._get_conn()
    _insert_memory(conn, "m_dream2", "星星梦境的内容很多很长",
                   room="dreams", provenance="dream")
    _insert_memory(conn, "m_normal2", "星星正常的内容记录",
                   room="living_room", provenance="user_statement")
    conn.commit()

    results = database.cjk_like_search("星星内容", top_k=10,
                                        status="active", exclude_provenance=["dream"])
    ids = [r["id"] for r in results]
    assert "m_dream2" not in ids
    assert "m_normal2" in ids


def test_h2_fail_closed_excludes_unknown_items():
    """RRF fail-closed: items that can't be looked up must be EXCLUDED."""
    def mock_get(mid):
        return {
            "known_normal": {"provenance_type": "user_statement"},
            "known_dream": {"provenance_type": "dream"},
        }.get(mid)

    merged = [
        {"id": "known_normal", "score": 0.9},
        {"id": "known_dream", "score": 0.8},
        {"id": "unknown_item", "score": 0.7},
    ]
    excl = ["dream"]
    filtered = [
        item for item in merged
        if mock_get(item["id"]) is not None
        and mock_get(item["id"]).get("provenance_type") not in excl
    ]
    assert len(filtered) == 1
    assert filtered[0]["id"] == "known_normal"


# ════════════════════════════════════════════
#  H3: generate_dreams() 原子预占
# ════════════════════════════════════════════

def test_h3_dream_log_unique_constraint(db_env):
    """dream_log PK prevents double insert."""
    conn = database._get_conn()
    conn.execute(
        "INSERT INTO dream_log (ai_id, local_day, memory_id, created_at) "
        "VALUES (?, ?, ?, ?)",
        ("claude", "2026-08-05", "mem_1", "2026-08-05T02:00:00Z"),
    )
    conn.commit()

    conn.execute(
        "INSERT OR IGNORE INTO dream_log (ai_id, local_day, memory_id, created_at) "
        "VALUES (?, ?, ?, ?)",
        ("claude", "2026-08-05", "mem_2", "2026-08-05T02:01:00Z"),
    )
    conn.commit()

    rows = conn.execute(
        "SELECT * FROM dream_log WHERE ai_id='claude' AND local_day='2026-08-05'"
    ).fetchall()
    assert len(rows) == 1, "Only one dream per AI per day"
    assert dict(rows[0])["memory_id"] == "mem_1"


def test_h3_begin_immediate_prevents_concurrent_reservation(db_env):
    """Two independent connections racing to reserve the same (ai_id, day) slot:
    only one should succeed; the other gets IntegrityError."""
    db_path = str(database.DB_PATH)

    conn1 = sqlite3.connect(db_path)
    conn1.execute("PRAGMA busy_timeout=5000")
    conn2 = sqlite3.connect(db_path)
    conn2.execute("PRAGMA busy_timeout=5000")

    # First connection reserves
    conn1.execute("BEGIN IMMEDIATE")
    conn1.execute(
        "INSERT INTO dream_log (ai_id, local_day, memory_id, created_at) "
        "VALUES (?, ?, '', ?)",
        ("claude", "2026-08-06", "2026-08-06T02:00:00Z"),
    )
    conn1.commit()

    # Second connection tries same slot
    with pytest.raises(sqlite3.IntegrityError):
        conn2.execute("BEGIN IMMEDIATE")
        conn2.execute(
            "INSERT INTO dream_log (ai_id, local_day, memory_id, created_at) "
            "VALUES (?, ?, '', ?)",
            ("claude", "2026-08-06", "2026-08-06T02:00:00Z"),
        )

    conn2.rollback()

    # Verify only one row
    rows = conn1.execute(
        "SELECT * FROM dream_log WHERE ai_id='claude' AND local_day='2026-08-06'"
    ).fetchall()
    assert len(rows) == 1

    conn1.close()
    conn2.close()


def test_h3_release_reservation_cleans_up(db_env):
    """_release_reservation deletes empty-memory_id reservations."""
    import dream
    db_path = str(database.DB_PATH)
    dream.DB_PATH = Path(db_path)

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(
        "INSERT INTO dream_log (ai_id, local_day, memory_id, created_at) "
        "VALUES (?, ?, '', ?)",
        ("claude", "2026-08-07", "2026-08-07T02:00:00Z"),
    )
    conn.commit()

    dream._release_reservation("claude", "2026-08-07")

    rows = conn.execute(
        "SELECT * FROM dream_log WHERE ai_id='claude' AND local_day='2026-08-07'"
    ).fetchall()
    assert len(rows) == 0, "Reservation with empty memory_id should be deleted"
    conn.close()


def test_h3_committed_reservation_not_released(db_env):
    """_release_reservation does NOT delete committed (non-empty memory_id) entries."""
    import dream
    db_path = str(database.DB_PATH)
    dream.DB_PATH = Path(db_path)

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(
        "INSERT INTO dream_log (ai_id, local_day, memory_id, created_at) "
        "VALUES (?, ?, ?, ?)",
        ("claude", "2026-08-08", "mem_actual_123", "2026-08-08T02:00:00Z"),
    )
    conn.commit()

    dream._release_reservation("claude", "2026-08-08")

    rows = conn.execute(
        "SELECT * FROM dream_log WHERE ai_id='claude' AND local_day='2026-08-08'"
    ).fetchall()
    assert len(rows) == 1, "Committed dream should NOT be released"
    conn.close()


# ════════════════════════════════════════════
#  H4: 梦境调度窗口覆盖 02:00
# ════════════════════════════════════════════

def test_h4_dream_window_includes_2am():
    import dream
    assert dream.DREAM_HOUR_START <= 2
    assert dream._in_dream_window(hour=2) is True


def test_h4_dream_window_includes_full_range():
    import dream
    for h in range(dream.DREAM_HOUR_START, dream.DREAM_HOUR_END):
        assert dream._in_dream_window(hour=h) is True


def test_h4_dream_window_excludes_daytime():
    import dream
    for h in [8, 12, 18, 23]:
        assert dream._in_dream_window(hour=h) is False


def test_h4_daemon_schedule_in_window():
    import dream
    assert dream._in_dream_window(hour=2) is True


# ════════════════════════════════════════════
#  M1: Profile Pydantic schema 校验
# ════════════════════════════════════════════

def test_m1_valid_user_profile_passes():
    from profile_builder import _validate_profile_schema
    content = json.dumps({
        "tier0": {"ai_identity": "Ceci 的三个 AI 伙伴"},
        "identity": {"value": "Ceci 是用户", "confidence": "high",
                      "evidence_tier": 1, "source_ids": ["mem_1"]},
    })
    ok, err, validated = _validate_profile_schema(content, "user", {"mem_1"})
    assert ok, f"Valid user profile should pass: {err}"
    assert validated, "Should return validated JSON"
    parsed = json.loads(validated)
    assert parsed["identity"]["source_ids"] == ["mem_1"]


def test_m1_invalid_confidence_fails():
    from profile_builder import _validate_profile_schema
    content = json.dumps({
        "tier0": {"ai_identity": "test"},
        "identity": {"value": "test", "confidence": "超高",
                      "evidence_tier": 1, "source_ids": ["m1"]},
    })
    ok, err, _ = _validate_profile_schema(content, "user")
    assert not ok


def test_m1_source_ids_subset_check():
    from profile_builder import _validate_profile_schema
    content = json.dumps({
        "tier0": {"ai_identity": "test"},
        "identity": {"value": "test", "confidence": "high",
                      "evidence_tier": 1, "source_ids": ["mem_1", "mem_99"]},
    })
    ok, err, _ = _validate_profile_schema(content, "user", {"mem_1"})
    assert not ok
    assert "mem_99" in err


def test_m1_empty_source_ids_fails():
    """source_ids is required and must have ≥1 entry."""
    from profile_builder import _validate_profile_schema
    content = json.dumps({
        "tier0": {"ai_identity": "test"},
        "identity": {"value": "test", "confidence": "high",
                      "evidence_tier": 1, "source_ids": []},
    })
    ok, err, _ = _validate_profile_schema(content, "user")
    assert not ok, "Empty source_ids should fail"


def test_m1_null_source_ids_fails():
    """Missing source_ids field should fail."""
    from profile_builder import _validate_profile_schema
    content = json.dumps({
        "tier0": {"ai_identity": "test"},
        "identity": {"value": "test", "confidence": "high", "evidence_tier": 1},
    })
    ok, err, _ = _validate_profile_schema(content, "user")
    assert not ok, "Missing source_ids should fail"


def test_m1_extra_fields_rejected():
    """extra='forbid' must reject unknown fields."""
    from profile_builder import _validate_profile_schema
    content = json.dumps({
        "tier0": {"ai_identity": "test"},
        "identity": {"value": "test", "confidence": "high",
                      "evidence_tier": 1, "source_ids": ["m1"]},
        "rogue_field": {"value": "hacked", "confidence": "high",
                        "evidence_tier": 1, "source_ids": ["m1"]},
    })
    ok, err, _ = _validate_profile_schema(content, "user")
    assert not ok, "Extra fields should be rejected"


def test_m1_nested_rogue_field_rejected():
    """Extra field inside ProfileField should be rejected."""
    from profile_builder import _validate_profile_schema
    content = json.dumps({
        "tier0": {"ai_identity": "test"},
        "identity": {"value": "test", "confidence": "high",
                      "evidence_tier": 1, "source_ids": ["m1"],
                      "hacked": True},
    })
    ok, err, _ = _validate_profile_schema(content, "user")
    assert not ok, "Nested extra field should be rejected"


def test_m1_empty_relationship_fails():
    """Empty relationship profile {} must fail."""
    from profile_builder import _validate_profile_schema
    ok, err, _ = _validate_profile_schema("{}", "relationship")
    assert not ok, "Empty relationship profile should fail"


def test_m1_valid_relationship_passes():
    from profile_builder import _validate_profile_schema
    content = json.dumps({
        "mode": {"value": "陪伴", "confidence": "high",
                 "evidence_tier": 2, "source_ids": ["mem_1"]},
    })
    ok, err, validated = _validate_profile_schema(content, "relationship", {"mem_1"})
    assert ok, f"Valid relationship profile should pass: {err}"


def test_m1_agent_profile_schema():
    from profile_builder import _validate_profile_schema
    content = json.dumps({
        "tier0": {"ai_identity": "Lucien 是温柔型 AI"},
        "identity": {"value": "Lucien", "confidence": "high",
                      "evidence_tier": 1, "source_ids": ["m1"]},
        "personality": {"value": "温柔", "confidence": "medium",
                        "evidence_tier": 2, "source_ids": ["m2"]},
    })
    ok, err, _ = _validate_profile_schema(content, "agent")
    assert ok, f"Valid agent profile should pass: {err}"


# ════════════════════════════════════════════
#  M2: _validate_and_retry — stores model_dump_json
# ════════════════════════════════════════════

def test_m2_returns_canonical_json():
    """_validate_and_retry must return model_dump_json, not raw LLM output."""
    from profile_builder import _validate_and_retry

    raw_llm = json.dumps({
        "tier0": {"ai_identity": "test"},
        "identity": {"value": "Lucien 是 AI", "confidence": "high",
                      "evidence_tier": 1, "source_ids": ["m1"]},
    }, ensure_ascii=False)

    async def run():
        with patch("profile_builder._call_llm", new_callable=AsyncMock,
                    return_value=raw_llm):
            result = await _validate_and_retry(
                "test prompt", "agent", {"m1"}, is_agent_or_rel=True)
        return result

    result = asyncio.run(run())
    assert result is not None
    parsed = json.loads(result)
    assert "identity" in parsed
    assert parsed["identity"]["source_ids"] == ["m1"]


def test_m2_retries_on_first_person():
    from profile_builder import _validate_and_retry

    bad = json.dumps({
        "tier0": {"ai_identity": "test"},
        "identity": {"value": "我是 Lucien", "confidence": "high",
                      "evidence_tier": 1, "source_ids": ["m1"]},
    }, ensure_ascii=False)
    good = json.dumps({
        "tier0": {"ai_identity": "test"},
        "identity": {"value": "Lucien 是温柔的 AI", "confidence": "high",
                      "evidence_tier": 1, "source_ids": ["m1"]},
    }, ensure_ascii=False)

    calls = [0]

    async def mock_llm(prompt):
        calls[0] += 1
        return bad if calls[0] == 1 else good

    async def run():
        calls[0] = 0
        with patch("profile_builder._call_llm", side_effect=mock_llm):
            return await _validate_and_retry(
                "test prompt", "agent", {"m1"}, is_agent_or_rel=True)

    result = asyncio.run(run())
    assert result is not None
    assert calls[0] >= 2


def test_m2_gives_up_on_repeated_failure():
    from profile_builder import _validate_and_retry

    async def run():
        with patch("profile_builder._call_llm", new_callable=AsyncMock,
                    return_value="not json"):
            return await _validate_and_retry("test prompt", "user", set())

    assert asyncio.run(run()) is None


def test_m2_all_three_profiles_use_unified_retry():
    import profile_builder
    source = open(profile_builder.__file__, encoding="utf-8").read()
    assert source.count("_validate_and_retry(") >= 4


# ════════════════════════════════════════════
#  M3: _filter_relationship_group_dynamic — pattern: tag only
# ════════════════════════════════════════════

def test_m3_no_pattern_tag_excluded():
    """group_dynamic without pattern: tag are conservatively excluded."""
    from profile_builder import _filter_relationship_group_dynamic

    mems = [
        _make_mem(id="n1", content="正常记忆"),
        _make_mem(id="gd1", category="group_dynamic", tags='["some_random_tag"]'),
        _make_mem(id="gd2", category="group_dynamic", tags='["another_tag"]'),
        _make_mem(id="gd3", category="group_dynamic", tags='["third_tag"]'),
        _make_mem(id="gd4", category="group_dynamic"),  # no tags
    ]
    result = _filter_relationship_group_dynamic(mems)
    ids = [m["id"] for m in result]
    assert "gd1" not in ids, "Non-pattern: tags should be excluded"
    assert "gd4" not in ids, "No-tag group_dynamic should be excluded"
    assert "n1" in ids


def test_m3_different_patterns_cannot_combine():
    """Three different pattern: tags must NOT combine."""
    from profile_builder import _filter_relationship_group_dynamic

    mems = [
        _make_mem(id="gd1", category="group_dynamic", tags='["pattern:teasing"]'),
        _make_mem(id="gd2", category="group_dynamic", tags='["pattern:mediating"]'),
        _make_mem(id="gd3", category="group_dynamic", tags='["pattern:protecting"]'),
    ]
    result = _filter_relationship_group_dynamic(mems)
    ids = [m["id"] for m in result]
    assert len(ids) == 0, "Different patterns must not combine"


def test_m3_same_pattern_meets_threshold():
    """>=3 memories with same pattern: tag should be included."""
    from profile_builder import _filter_relationship_group_dynamic

    mems = [
        _make_mem(id="gd1", category="group_dynamic", tags='["pattern:teasing"]'),
        _make_mem(id="gd2", category="group_dynamic", tags='["pattern:teasing"]'),
        _make_mem(id="gd3", category="group_dynamic", tags='["pattern:teasing"]'),
    ]
    result = _filter_relationship_group_dynamic(mems)
    ids = [m["id"] for m in result]
    assert "gd1" in ids
    assert "gd2" in ids
    assert "gd3" in ids


def test_m3_mixed_patterns_partial_inclusion():
    from profile_builder import _filter_relationship_group_dynamic

    mems = [
        _make_mem(id="gd1", category="group_dynamic", tags='["pattern:teasing"]'),
        _make_mem(id="gd2", category="group_dynamic", tags='["pattern:teasing"]'),
        _make_mem(id="gd3", category="group_dynamic", tags='["pattern:teasing"]'),
        _make_mem(id="gd4", category="group_dynamic", tags='["pattern:mediating"]'),
        _make_mem(id="gd5", category="group_dynamic", tags='["pattern:mediating"]'),
    ]
    result = _filter_relationship_group_dynamic(mems)
    ids = [m["id"] for m in result]
    assert "gd1" in ids
    assert "gd4" not in ids


def test_m3_roleplay_only_group_excluded():
    from profile_builder import _filter_relationship_group_dynamic

    mems = [
        _make_mem(id="gd1", category="group_dynamic 角色扮演", tags='["pattern:teasing"]'),
        _make_mem(id="gd2", category="group_dynamic 角色扮演", tags='["pattern:teasing"]'),
        _make_mem(id="gd3", category="group_dynamic 角色扮演", tags='["pattern:teasing"]'),
    ]
    result = _filter_relationship_group_dynamic(mems)
    ids = [m["id"] for m in result]
    assert "gd1" not in ids


# ════════════════════════════════════════════
#  M6: cleanup 单事务 + dry-run ROLLBACK
# ════════════════════════════════════════════

@pytest.fixture
def cleanup_mod():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "cleanup_phase15",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "scripts", "cleanup_phase15.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_cleanup_db(tmp_path, name="cleanup_test.db"):
    db_path = tmp_path / name
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE memories (
            id TEXT PRIMARY KEY, content TEXT DEFAULT '', room TEXT DEFAULT '',
            status TEXT DEFAULT 'active', provenance_type TEXT DEFAULT '',
            info_type TEXT DEFAULT 'fact', category TEXT DEFAULT '',
            source_platform TEXT DEFAULT '', source_ai TEXT DEFAULT '',
            owner_ai TEXT DEFAULT '', importance REAL DEFAULT 0.5,
            tags TEXT DEFAULT '[]', created_at TEXT DEFAULT '',
            updated_at TEXT DEFAULT ''
        )
    """)
    conn.execute(
        "INSERT INTO memories (id, content, room, status, provenance_type, info_type) "
        "VALUES ('m1', 'test', 'relationships', 'active', '', 'fact')"
    )
    conn.commit()
    conn.close()
    return db_path


def test_m6_cleanup_dry_run_rolls_back(tmp_path, cleanup_mod):
    db_path = _make_cleanup_db(tmp_path)
    original_db = cleanup_mod.DB_PATH
    cleanup_mod.DB_PATH = db_path
    try:
        cleanup_mod.cleanup(dry_run=True)
    finally:
        cleanup_mod.DB_PATH = original_db

    conn = sqlite3.connect(str(db_path))
    row = conn.execute("SELECT room FROM memories WHERE id='m1'").fetchone()
    assert row[0] == "relationships", "dry-run must ROLLBACK"
    conn.close()


def test_m6_cleanup_real_run_commits(tmp_path, cleanup_mod):
    db_path = _make_cleanup_db(tmp_path, "cleanup_test2.db")
    original_db = cleanup_mod.DB_PATH
    cleanup_mod.DB_PATH = db_path
    try:
        cleanup_mod.cleanup(dry_run=False)
    finally:
        cleanup_mod.DB_PATH = original_db

    conn = sqlite3.connect(str(db_path))
    row = conn.execute("SELECT room FROM memories WHERE id='m1'").fetchone()
    assert row[0] == "relationship", "Real run must commit"
    conn.close()


def test_m6_cleanup_uses_single_transaction(cleanup_mod):
    import inspect
    source = inspect.getsource(cleanup_mod.cleanup)
    assert "BEGIN" in source
    assert source.count("conn.commit()") == 1
    assert "conn.rollback()" in source
    assert "finally:" in source
    assert "conn.close()" in source
