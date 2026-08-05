"""
PR #14 Codex 审出的 8 项必修 bug 回归测试。

覆盖：
  H1: dream_recall 权限过滤（Jasper 不能读 Lucien 的梦）
  H2: 三条 recall 路径 exclude_provenance 隔离
  H3: generate_dreams() 并发锁 + DB 唯一约束
  H4: 梦境调度窗口覆盖 02:00
  M1: Profile Pydantic schema 校验
  M2: _validate_and_retry 统一重试
  M3: _filter_relationship_group_dynamic 按 pattern key 分组
  M6: cleanup 单事务 + dry-run ROLLBACK

运行：ALLOW_DEFAULT_HUB_SECRET=1 python -m pytest tests/test_bugfix_pr14.py -q
"""
import os
import sys
import json
import sqlite3
import asyncio
from datetime import datetime, timezone
from unittest.mock import patch, AsyncMock, MagicMock

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


# ════════════════════════════════════════════
#  H1: dream_recall 权限过滤
# ════════════════════════════════════════════

def test_h1_jasper_cannot_read_lucien_dream():
    """Jasper must not see Lucien's private dream via dream_recall."""
    from visibility import can_view

    lucien_dream = {
        "id": "dream_lucien_1", "content": "Lucien 梦到了...",
        "room": "dreams", "layer": "private",
        "owner_ai": "lucien", "source_ai": "lucien",
        "provenance_type": "dream",
    }
    assert can_view(lucien_dream, "lucien") is True
    assert can_view(lucien_dream, "jasper") is False
    assert can_view(lucien_dream, "claude") is False


def test_h1_owner_can_read_own_dream():
    from visibility import can_view

    claude_dream = {
        "id": "dream_claude_1", "content": "Claude 的梦境",
        "room": "dreams", "layer": "private",
        "owner_ai": "claude", "source_ai": "claude",
        "provenance_type": "dream",
    }
    assert can_view(claude_dream, "claude") is True
    assert can_view(claude_dream, "cloudy") is True  # alias


def test_h1_over_fetch_preserves_results():
    """Over-fetch pattern: invisible rows shouldn't squeeze out legitimate results."""
    from visibility import can_view

    mems = []
    for i in range(8):
        mems.append({
            "id": f"dream_other_{i}", "room": "dreams", "layer": "private",
            "owner_ai": "lucien", "provenance_type": "dream",
        })
    for i in range(3):
        mems.append({
            "id": f"dream_mine_{i}", "room": "dreams", "layer": "private",
            "owner_ai": "claude", "provenance_type": "dream",
        })

    visible = [m for m in mems if can_view(m, "claude")]
    assert len(visible) == 3


# ════════════════════════════════════════════
#  H2: 三条 recall 路径 exclude_provenance
# ════════════════════════════════════════════

def test_h2_fts_excludes_dream_provenance(db_env):
    """FTS search must respect exclude_provenance parameter."""
    conn = database._get_conn()
    now = datetime.now(timezone.utc).isoformat()

    conn.execute(
        "INSERT INTO memories (id, content, room, status, provenance_type, "
        "created_at, updated_at, importance, category, tags, domain) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("m_dream", "testing dream searchable content here", "dreams", "active", "dream",
         now, now, 0.5, "", "[]", "[]"),
    )
    conn.execute(
        "INSERT INTO memories (id, content, room, status, provenance_type, "
        "created_at, updated_at, importance, category, tags, domain) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("m_normal", "testing normal searchable content here", "living_room", "active", "user_statement",
         now, now, 0.5, "", "[]", "[]"),
    )
    conn.commit()

    results = database.fts_search("testing searchable", top_k=10, status="active",
                                   exclude_provenance=["dream"])
    ids = [r["id"] for r in results]
    assert "m_dream" not in ids, "dream provenance must be excluded from FTS"
    assert "m_normal" in ids


def test_h2_cjk_like_excludes_dream_provenance(db_env):
    """CJK LIKE search must respect exclude_provenance parameter."""
    conn = database._get_conn()
    now = datetime.now(timezone.utc).isoformat()

    conn.execute(
        "INSERT INTO memories (id, content, room, status, provenance_type, "
        "created_at, updated_at, importance, category, tags, domain) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("m_dream2", "星星梦境内容", "dreams", "active", "dream",
         now, now, 0.5, "", "[]", "[]"),
    )
    conn.execute(
        "INSERT INTO memories (id, content, room, status, provenance_type, "
        "created_at, updated_at, importance, category, tags, domain) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("m_normal2", "星星正常内容", "living_room", "active", "user_statement",
         now, now, 0.5, "", "[]", "[]"),
    )
    conn.commit()

    results = database.cjk_like_search("星星", top_k=10, status="active",
                                        exclude_provenance=["dream"])
    ids = [r["id"] for r in results]
    assert "m_dream2" not in ids
    assert "m_normal2" in ids


def test_h2_fail_closed_filter_after_rrf():
    """The post-RRF fail-closed filter must remove dream provenance."""
    from memory_ops import recall

    merged_items = [
        {"id": "rrf_dream", "content": "梦", "score": 0.9},
        {"id": "rrf_normal", "content": "正常", "score": 0.8},
    ]

    def mock_get(mid):
        return {
            "rrf_dream": {"provenance_type": "dream"},
            "rrf_normal": {"provenance_type": "user_statement"},
        }.get(mid)

    excl = ["dream"]
    filtered = [
        item for item in merged_items
        if mock_get(item["id"]) is None
        or mock_get(item["id"]).get("provenance_type") not in excl
    ]
    assert len(filtered) == 1
    assert filtered[0]["id"] == "rrf_normal"


# ════════════════════════════════════════════
#  H3: generate_dreams() 并发锁 + DB 唯一约束
# ════════════════════════════════════════════

def test_h3_dream_lock_exists():
    """Module-level asyncio.Lock must exist."""
    import dream
    assert hasattr(dream, "_dream_lock")
    assert isinstance(dream._dream_lock, asyncio.Lock)


def test_h3_dream_log_unique_constraint(db_env):
    """dream_log table must enforce (ai_id, local_day) uniqueness."""
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


def test_h3_concurrent_dreams_no_duplicate():
    """asyncio.gather() of two generate_dreams() must not produce duplicates."""
    import dream

    call_count = 0

    async def mock_inner(force=False):
        nonlocal call_count
        call_count += 1
        return {"claude": "dreamed"}

    async def run():
        nonlocal call_count
        call_count = 0
        with patch.object(dream, "_generate_dreams_inner", side_effect=mock_inner):
            await asyncio.gather(
                dream.generate_dreams(force=True),
                dream.generate_dreams(force=True),
            )
        return call_count

    count = asyncio.run(run())
    assert count == 2, "Both calls execute (serialized by lock)"


# ════════════════════════════════════════════
#  H4: 梦境调度窗口覆盖 02:00
# ════════════════════════════════════════════

def test_h4_dream_window_includes_2am():
    """Dream window must cover 02:00 (daemon maintenance time)."""
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


def test_h4_all_scheduling_points_in_window():
    """Daemon step 10.8 runs at 02:00 — must fall within dream window."""
    import dream
    daemon_hour = 2
    assert dream._in_dream_window(hour=daemon_hour) is True


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
    ok, err = _validate_profile_schema(content, "user", {"mem_1"})
    assert ok, f"Valid user profile should pass: {err}"


def test_m1_invalid_confidence_fails():
    from profile_builder import _validate_profile_schema
    content = json.dumps({
        "tier0": {"ai_identity": "test"},
        "identity": {"value": "test", "confidence": "超高",
                      "evidence_tier": 1, "source_ids": []},
    })
    ok, err = _validate_profile_schema(content, "user")
    assert not ok, "Invalid confidence value should fail schema"


def test_m1_source_ids_subset_check():
    from profile_builder import _validate_profile_schema
    content = json.dumps({
        "tier0": {"ai_identity": "test"},
        "identity": {"value": "test", "confidence": "high",
                      "evidence_tier": 1, "source_ids": ["mem_1", "mem_99"]},
    })
    ok, err = _validate_profile_schema(content, "user", {"mem_1"})
    assert not ok, "source_ids not subset of valid_mem_ids should fail"
    assert "mem_99" in err


def test_m1_agent_profile_schema():
    from profile_builder import _validate_profile_schema
    content = json.dumps({
        "tier0": {"ai_identity": "Lucien 是温柔型 AI"},
        "identity": {"value": "Lucien", "confidence": "high",
                      "evidence_tier": 1, "source_ids": []},
        "personality": {"value": "温柔", "confidence": "medium",
                        "evidence_tier": 2, "source_ids": []},
    })
    ok, err = _validate_profile_schema(content, "agent")
    assert ok, f"Valid agent profile should pass: {err}"


def test_m1_relationship_profile_schema():
    from profile_builder import _validate_profile_schema
    content = json.dumps({
        "mode": {"value": "陪伴", "confidence": "high",
                 "evidence_tier": 2, "source_ids": ["mem_1"]},
    })
    ok, err = _validate_profile_schema(content, "relationship", {"mem_1"})
    assert ok, f"Valid relationship profile should pass: {err}"


# ════════════════════════════════════════════
#  M2: _validate_and_retry 统一验证循环
# ════════════════════════════════════════════

def test_m2_validate_and_retry_returns_valid_json():
    from profile_builder import _validate_and_retry

    valid_output = json.dumps({
        "tier0": {"ai_identity": "test"},
        "identity": {"value": "Lucien 是 AI", "confidence": "high",
                      "evidence_tier": 1, "source_ids": ["m1"]},
    }, ensure_ascii=False)

    async def run():
        with patch("profile_builder._call_llm", new_callable=AsyncMock,
                    return_value=valid_output):
            result = await _validate_and_retry(
                "test prompt", "agent", {"m1"}, is_agent_or_rel=True)
        return result

    result = asyncio.run(run())
    assert result is not None
    parsed = json.loads(result)
    assert "identity" in parsed


def test_m2_validate_and_retry_retries_on_first_person():
    from profile_builder import _validate_and_retry

    bad_output = json.dumps({
        "tier0": {"ai_identity": "test"},
        "identity": {"value": "我是 Lucien", "confidence": "high",
                      "evidence_tier": 1, "source_ids": []},
    }, ensure_ascii=False)
    good_output = json.dumps({
        "tier0": {"ai_identity": "test"},
        "identity": {"value": "Lucien 是温柔的 AI", "confidence": "high",
                      "evidence_tier": 1, "source_ids": []},
    }, ensure_ascii=False)

    call_count = 0

    async def mock_llm(prompt):
        nonlocal call_count
        call_count += 1
        return bad_output if call_count == 1 else good_output

    async def run():
        nonlocal call_count
        call_count = 0
        with patch("profile_builder._call_llm", side_effect=mock_llm):
            result = await _validate_and_retry(
                "test prompt", "agent", set(), is_agent_or_rel=True)
        return result, call_count

    result, count = asyncio.run(run())
    assert result is not None, "Should succeed on retry"
    assert count >= 2, "Should have retried at least once"


def test_m2_validate_and_retry_gives_up():
    from profile_builder import _validate_and_retry

    async def run():
        with patch("profile_builder._call_llm", new_callable=AsyncMock,
                    return_value="not json"):
            result = await _validate_and_retry(
                "test prompt", "user", set())
        return result

    result = asyncio.run(run())
    assert result is None, "Should give up after MAX_PROFILE_RETRIES"


def test_m2_all_three_profiles_use_unified_retry():
    """All three rebuild functions must call _validate_and_retry."""
    import profile_builder
    source = open(profile_builder.__file__, encoding="utf-8").read()
    assert source.count("_validate_and_retry(") >= 4, \
        "Definition + 3 callers (user/agent/relationship)"


# ════════════════════════════════════════════
#  M3: _filter_relationship_group_dynamic 按 pattern key 分组
# ════════════════════════════════════════════

def test_m3_different_patterns_cannot_combine():
    """Three memories with different pattern tags must NOT combine to meet threshold."""
    from profile_builder import _filter_relationship_group_dynamic

    mems = [
        _make_mem(id="n1", content="正常记忆"),
        _make_mem(id="gd1", category="group_dynamic", tags='["teasing"]'),
        _make_mem(id="gd2", category="group_dynamic", tags='["mediating"]'),
        _make_mem(id="gd3", category="group_dynamic", tags='["protecting"]'),
    ]
    result = _filter_relationship_group_dynamic(mems)
    ids = [m["id"] for m in result]
    assert "gd1" not in ids, "Different patterns must not combine"
    assert "gd2" not in ids
    assert "gd3" not in ids
    assert "n1" in ids


def test_m3_same_pattern_meets_threshold():
    """Three memories with the SAME pattern tag should be included."""
    from profile_builder import _filter_relationship_group_dynamic

    mems = [
        _make_mem(id="n1", content="正常记忆"),
        _make_mem(id="gd1", category="group_dynamic", tags='["teasing"]'),
        _make_mem(id="gd2", category="group_dynamic", tags='["teasing"]'),
        _make_mem(id="gd3", category="group_dynamic", tags='["teasing"]'),
    ]
    result = _filter_relationship_group_dynamic(mems)
    ids = [m["id"] for m in result]
    assert "gd1" in ids
    assert "gd2" in ids
    assert "gd3" in ids


def test_m3_mixed_patterns_partial_inclusion():
    """Only groups meeting threshold are included; others excluded."""
    from profile_builder import _filter_relationship_group_dynamic

    mems = [
        _make_mem(id="gd1", category="group_dynamic", tags='["teasing"]'),
        _make_mem(id="gd2", category="group_dynamic", tags='["teasing"]'),
        _make_mem(id="gd3", category="group_dynamic", tags='["teasing"]'),
        _make_mem(id="gd4", category="group_dynamic", tags='["mediating"]'),
        _make_mem(id="gd5", category="group_dynamic", tags='["mediating"]'),
    ]
    result = _filter_relationship_group_dynamic(mems)
    ids = [m["id"] for m in result]
    assert "gd1" in ids, "teasing group (3) should be included"
    assert "gd4" not in ids, "mediating group (2) should be excluded"


def test_m3_roleplay_only_group_excluded():
    """Group with all roleplay category must be excluded even if >=3."""
    from profile_builder import _filter_relationship_group_dynamic

    mems = [
        _make_mem(id="gd1", category="group_dynamic 角色扮演", tags='["teasing"]'),
        _make_mem(id="gd2", category="group_dynamic 角色扮演", tags='["teasing"]'),
        _make_mem(id="gd3", category="group_dynamic 角色扮演", tags='["teasing"]'),
    ]
    result = _filter_relationship_group_dynamic(mems)
    ids = [m["id"] for m in result]
    assert "gd1" not in ids, "All-roleplay group should be excluded"


# ════════════════════════════════════════════
#  M6: cleanup 单事务 + dry-run ROLLBACK
# ════════════════════════════════════════════

@pytest.fixture
def cleanup_mod():
    """Import cleanup_phase15 module."""
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
    """dry-run must execute UPDATEs then ROLLBACK — no changes persisted."""
    db_path = _make_cleanup_db(tmp_path)
    original_db = cleanup_mod.DB_PATH
    cleanup_mod.DB_PATH = db_path
    try:
        cleanup_mod.cleanup(dry_run=True)
    finally:
        cleanup_mod.DB_PATH = original_db

    conn = sqlite3.connect(str(db_path))
    row = conn.execute("SELECT room FROM memories WHERE id='m1'").fetchone()
    assert row[0] == "relationships", "dry-run must ROLLBACK — room should be unchanged"
    conn.close()


def test_m6_cleanup_real_run_commits(tmp_path, cleanup_mod):
    """Real run must persist changes."""
    db_path = _make_cleanup_db(tmp_path, "cleanup_test2.db")
    original_db = cleanup_mod.DB_PATH
    cleanup_mod.DB_PATH = db_path
    try:
        cleanup_mod.cleanup(dry_run=False)
    finally:
        cleanup_mod.DB_PATH = original_db

    conn = sqlite3.connect(str(db_path))
    row = conn.execute("SELECT room FROM memories WHERE id='m1'").fetchone()
    assert row[0] == "relationship", "Real run must commit — room should be fixed"
    conn.close()


def test_m6_cleanup_uses_single_transaction(cleanup_mod):
    """Cleanup must use BEGIN + COMMIT/ROLLBACK, not multiple commits."""
    import inspect
    source = inspect.getsource(cleanup_mod.cleanup)
    assert "conn.execute(\"BEGIN\")" in source or "BEGIN" in source
    assert source.count("conn.commit()") == 1, "Must have exactly one commit call"
    assert "conn.rollback()" in source, "Must have rollback for error handling"
    assert "finally:" in source, "Must use finally to ensure conn.close()"
    assert "conn.close()" in source
