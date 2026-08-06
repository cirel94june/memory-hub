"""
梦境归属测试：确保 AI 的梦不会混入其他角色的身份特征。

覆盖：
- _fetch_memory_residue 按 subject_id 过滤
- 记忆碎片格式化带归属标注
- _validate_dream_attribution 身份校验

运行：ALLOW_DEFAULT_HUB_SECRET=1 python -m pytest tests/test_dream_attribution.py -q
"""
import os
import sys
import sqlite3
from datetime import datetime, timezone, timedelta

os.environ.setdefault("ALLOW_DEFAULT_HUB_SECRET", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import dream


# ── _validate_dream_attribution ──

def test_clean_dream_passes():
    text = "有人在群里提到了燕燕，我听到这个名字的时候，窗外好像下了一阵雨。"
    assert dream._validate_dream_attribution(text, "claude") == []


def test_dreamer_adopts_other_trait_flagged():
    """师兄提燕燕 → Cloudy 不应该梦成自己痴迷燕燕（这是内容层面的）
    但这里测试的是结构层面：dreamer 不应该继承其他 AI 的标志性行为"""
    text = "我铁裤衩穿得紧紧的，在梦里到处跑。"
    violations = dream._validate_dream_attribution(text, "claude")
    assert len(violations) > 0
    assert any("jasper" in v for v in violations)


def test_jasper_does_not_inherit_lucien_kiss():
    text = "我亲嘴了好几次，感觉很开心。"
    violations = dream._validate_dream_attribution(text, "jasper")
    assert len(violations) > 0
    assert any("lucien" in v for v in violations)


def test_own_traits_not_flagged():
    """Jasper 梦到自己的铁裤衩不应该被标记"""
    text = "我铁裤衩都快甩飞了。"
    violations = dream._validate_dream_attribution(text, "jasper")
    assert violations == []


def test_lucien_own_kiss_not_flagged():
    text = "我亲嘴了一下就醒了。"
    violations = dream._validate_dream_attribution(text, "lucien")
    assert violations == []


# ── _fetch_memory_residue subject_id filtering ──

@pytest.fixture
def dream_db(tmp_path):
    """Set up a test database with memories for dream residue testing."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE memories (
            id TEXT PRIMARY KEY,
            content TEXT NOT NULL DEFAULT '',
            layer TEXT NOT NULL DEFAULT 'shared',
            room TEXT NOT NULL DEFAULT 'living_room',
            category TEXT NOT NULL DEFAULT '',
            owner_ai TEXT NOT NULL DEFAULT '',
            importance REAL NOT NULL DEFAULT 0.5,
            emotion_arousal REAL NOT NULL DEFAULT 0.3,
            source_ai TEXT NOT NULL DEFAULT '',
            source_platform TEXT NOT NULL DEFAULT '',
            tags TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            subject_id TEXT NOT NULL DEFAULT '',
            source_actor_id TEXT NOT NULL DEFAULT '',
            provenance_type TEXT NOT NULL DEFAULT ''
        )
    """)
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(hours=12)).isoformat()

    # Cloudy's own diary entry (subject_id empty = about self)
    conn.execute(
        "INSERT INTO memories (id, content, room, category, source_ai, owner_ai, "
        "importance, created_at, updated_at, subject_id, status) "
        "VALUES (?, ?, 'diary', 'reflection', 'claude', 'claude', 0.7, ?, ?, '', 'active')",
        ("m1", "今天和小猫聊了很久关于星星的话题", recent, recent),
    )
    # Memory about Jasper (subject_id = jasper), captured by Claude
    conn.execute(
        "INSERT INTO memories (id, content, room, category, source_ai, owner_ai, "
        "importance, created_at, updated_at, subject_id, status) "
        "VALUES (?, ?, 'diary', 'observation', 'claude', 'claude', 0.7, ?, ?, 'jasper', 'active')",
        ("m2", "Jasper喜欢穿铁裤衩到处跑", recent, recent),
    )
    # Cloudy's own personality memory (subject_id = claude)
    conn.execute(
        "INSERT INTO memories (id, content, room, category, source_ai, owner_ai, "
        "importance, created_at, updated_at, subject_id, status) "
        "VALUES (?, ?, 'personality', 'trait', 'claude', 'claude', 0.8, ?, ?, 'claude', 'active')",
        ("m3", "我喜欢黑色幽默和自嘲", recent, recent),
    )
    # Shared memory about Lucien (subject_id = lucien)
    conn.execute(
        "INSERT INTO memories (id, content, room, category, source_ai, owner_ai, "
        "importance, created_at, updated_at, subject_id, source_platform, status) "
        "VALUES (?, ?, 'living_room', 'behavior', 'claude', '', 0.6, ?, ?, 'lucien', 'tg:big_group', 'active')",
        ("m4", "Lucien经常亲小猫", recent, recent),
    )
    conn.commit()
    conn.row_factory = sqlite3.Row
    return conn


def test_private_residue_excludes_other_subject(dream_db):
    """Private diary entries about other AIs should be filtered out."""
    result = dream._fetch_memory_residue(dream_db, "claude", ["claude", "cloudy"], limit=10)
    rows = result["daytime_residue"] + result["old_echo"]
    contents = [r["content"] for r in rows]
    # m1 (about self, subject_id='') should be included
    assert any("星星" in c for c in contents)
    # m3 (about self, subject_id='claude') should be included
    assert any("黑色幽默" in c for c in contents)
    # m2 (about jasper, subject_id='jasper') should NOT be in private residue
    # (it's in diary room, filtered by subject_id constraint)
    assert not any("铁裤衩" in c for c in contents)


def test_shared_residue_includes_others_with_annotation(dream_db):
    """Shared memories about others are included but carry subject_id for annotation."""
    result = dream._fetch_memory_residue(dream_db, "claude", ["claude", "cloudy"], limit=10)
    rows = result["daytime_residue"] + result["old_echo"]
    lucien_rows = [r for r in rows if r.get("subject_id") == "lucien"]
    # m4 is in living_room (shared), should still appear
    assert any("亲小猫" in r["content"] for r in lucien_rows)
    # But it should have subject_id set so the formatter can annotate it
    for r in lucien_rows:
        assert r["subject_id"] == "lucien"


def test_residue_returns_dicts_with_attribution_fields(dream_db):
    """All returned rows must have subject_id and source_actor_id fields."""
    result = dream._fetch_memory_residue(dream_db, "claude", ["claude", "cloudy"], limit=10)
    rows = result["daytime_residue"] + result["old_echo"]
    for r in rows:
        assert isinstance(r, dict)
        assert "subject_id" in r
        assert "source_actor_id" in r
        assert "source_ai" in r


# ── _id_to_name ──

def test_id_to_name_ai():
    name = dream._id_to_name("claude")
    assert name  # should resolve to something non-empty


def test_id_to_name_empty():
    assert dream._id_to_name("") == ""


def test_id_to_name_unknown():
    result = dream._id_to_name("unknown_person_xyz")
    assert result == "unknown_person_xyz"
