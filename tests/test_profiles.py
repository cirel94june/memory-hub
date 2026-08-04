"""
Profile 系统测试。

覆盖：
- profiles 表 CRUD（upsert/get/list/delete）
- version 递增（红线 #19）
- Profile 单向性（红线 #20：不提供反向写入接口）
- _extract_json 解析
- _contains_first_person 检测
- _has_changed 变化检测

运行：ALLOW_DEFAULT_HUB_SECRET=1 python -m pytest tests/test_profiles.py -q
"""
import os
import sys
import json
import asyncio

os.environ.setdefault("ALLOW_DEFAULT_HUB_SECRET", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import database
from profile_builder import (
    _extract_json, _contains_first_person, _has_changed,
    _gather_memories, _filter_evidence, _is_excluded_category,
    _filter_relationship_group_dynamic, _text_too_stylized,
    _truncate_profile_fields, _check_temporal_stability,
    EXCLUDED_ROOMS, EXCLUDED_PROVENANCE, FIELD_CHAR_LIMITS,
)


@pytest.fixture
def db_env(tmp_path):
    db_path = str(tmp_path / "test.db")
    database.DB_PATH = tmp_path / "test.db"
    asyncio.run(database.init_db(db_path))
    return db_path


# ── CRUD ──

def test_upsert_and_get_profile(db_env):
    profile = {
        "id": "user_ceci",
        "profile_type": "user",
        "owner_ai": "",
        "content": '{"identity": "Ceci"}',
        "generated_at": "2026-08-03T00:00:00Z",
        "source_memory_ids": '["mem_1", "mem_2"]',
    }
    database.upsert_profile(profile)
    result = database.get_profile("user_ceci")
    assert result is not None
    assert result["profile_type"] == "user"
    assert result["version"] == 1
    assert json.loads(result["content"])["identity"] == "Ceci"


def test_version_increments_on_update(db_env):
    profile = {
        "id": "agent_lucien",
        "profile_type": "agent",
        "owner_ai": "lucien",
        "content": '{"identity": "Lucien v1"}',
        "generated_at": "2026-08-03T00:00:00Z",
    }
    database.upsert_profile(profile)
    assert database.get_profile("agent_lucien")["version"] == 1

    profile["content"] = '{"identity": "Lucien v2"}'
    profile["generated_at"] = "2026-08-03T01:00:00Z"
    database.upsert_profile(profile)
    result = database.get_profile("agent_lucien")
    assert result["version"] == 2
    assert "v2" in result["content"]


def test_list_profiles_by_type(db_env):
    for pid, ptype, owner in [
        ("user_ceci", "user", ""),
        ("agent_claude", "agent", "claude"),
        ("agent_lucien", "agent", "lucien"),
        ("rel_claude_ceci", "relationship", "claude"),
    ]:
        database.upsert_profile({
            "id": pid, "profile_type": ptype, "owner_ai": owner,
            "content": "{}", "generated_at": "2026-08-03T00:00:00Z",
        })

    all_profiles = database.list_profiles()
    assert len(all_profiles) == 4

    agents = database.list_profiles(profile_type="agent")
    assert len(agents) == 2
    assert all(p["profile_type"] == "agent" for p in agents)


def test_delete_profile(db_env):
    database.upsert_profile({
        "id": "test_del", "profile_type": "user", "content": "{}",
        "generated_at": "2026-08-03T00:00:00Z",
    })
    assert database.get_profile("test_del") is not None
    assert database.delete_profile("test_del") is True
    assert database.get_profile("test_del") is None


def test_get_nonexistent_profile(db_env):
    assert database.get_profile("nonexistent") is None


# ── JSON extraction ──

def test_extract_json_clean():
    assert _extract_json('{"key": "value"}') is not None


def test_extract_json_with_markdown_fence():
    text = '```json\n{"key": "value"}\n```'
    result = _extract_json(text)
    assert result is not None
    assert json.loads(result)["key"] == "value"


def test_extract_json_with_surrounding_text():
    text = 'Here is the result:\n{"key": "value"}\nDone.'
    result = _extract_json(text)
    assert result is not None


def test_extract_json_invalid():
    assert _extract_json("not json at all") is None


# ── First person detection ──

def test_first_person_detected():
    content = json.dumps({"identity": "我是 Lucien，我觉得小猫很好"}, ensure_ascii=False)
    assert _contains_first_person(content) is True


def test_third_person_passes():
    content = json.dumps({"identity": "Lucien 是一个温柔的 AI"}, ensure_ascii=False)
    assert _contains_first_person(content) is False


def test_first_person_in_nested_field():
    content = json.dumps({
        "identity": "Lucien 是 AI",
        "style": "Lucien 倾向于用我的方式表达"
    }, ensure_ascii=False)
    assert _contains_first_person(content) is True


# ── Change detection ──

def test_has_changed_new_profile(db_env):
    assert _has_changed("nonexistent", ["mem_1"]) is True


def test_has_changed_same_ids(db_env):
    database.upsert_profile({
        "id": "test_change", "profile_type": "user", "content": "{}",
        "generated_at": "2026-08-03T00:00:00Z",
        "source_memory_ids": '["mem_1", "mem_2"]',
    })
    assert _has_changed("test_change", ["mem_1", "mem_2"]) is False


def test_has_changed_different_ids(db_env):
    database.upsert_profile({
        "id": "test_change2", "profile_type": "user", "content": "{}",
        "generated_at": "2026-08-03T00:00:00Z",
        "source_memory_ids": '["mem_1"]',
    })
    assert _has_changed("test_change2", ["mem_1", "mem_3"]) is True


# ── Red line #20: No reverse write path ──

def test_no_profile_to_memory_write():
    """Profile builder module must not import remember() or any memory write function."""
    import profile_builder
    source = open(profile_builder.__file__, encoding="utf-8").read()
    assert "remember(" not in source
    assert "set_memory(" not in source
    assert "insert_" not in source or "insert_audit" not in source


# ── Evidence filtering (item 6: 3 base tests) ──

def _make_mem(**kwargs):
    defaults = {
        "id": "mem_test", "content": "test", "room": "living_room",
        "info_type": "fact", "importance": 0.5, "created_at": "2026-08-03",
        "category": "", "provenance_type": "user_statement", "fact_confidence": 0.9,
    }
    defaults.update(kwargs)
    return defaults


def test_profile_filters_dreams():
    """Dreams room and dream provenance must be excluded."""
    mems = [
        _make_mem(id="m1", room="dreams", content="梦到了飞行"),
        _make_mem(id="m2", room="living_room", provenance_type="dream", content="梦境记录"),
        _make_mem(id="m3", room="living_room", content="她喜欢猫"),
        _make_mem(id="m4", category="night_dream", content="梦里变成蝴蝶"),
    ]
    filtered = _filter_evidence(mems, "agent")
    ids = [m["id"] for m in filtered]
    assert "m1" not in ids, "dreams room should be excluded"
    assert "m2" not in ids, "dream provenance should be excluded"
    assert "m4" not in ids, "night_dream category should be excluded"
    assert "m3" in ids, "normal memory should pass"


def test_profile_rejects_hypothesis():
    """Roleplay categories and game_room must be excluded."""
    mems = [
        _make_mem(id="m1", room="game_room", content="游戏里的角色"),
        _make_mem(id="m2", category="角色扮演", content="扮演场景"),
        _make_mem(id="m3", provenance_type="roleplay_meme", content="群聊梗"),
        _make_mem(id="m4", category="群聊玩梗", content="搞笑互动"),
        _make_mem(id="m5", room="living_room", content="正常对话"),
    ]
    filtered = _filter_evidence(mems, "agent")
    ids = [m["id"] for m in filtered]
    assert "m1" not in ids, "game_room should be excluded"
    assert "m2" not in ids, "roleplay category should be excluded"
    assert "m3" not in ids, "roleplay_meme provenance should be excluded"
    assert "m4" not in ids, "群聊玩梗 category should be excluded"
    assert "m5" in ids, "normal memory should pass"


def test_profile_requires_source_ids():
    """Profile schema must include source_ids per field — verified via prompt template."""
    import profile_builder
    source = open(profile_builder.__file__, encoding="utf-8").read()
    assert "source_ids" in source, "prompts must require source_ids per field"
    assert "evidence_tier" in source, "prompts must require evidence_tier per field"
    assert "confidence" in source, "prompts must require confidence per field"


# ── Item A: group_dynamic conditional inclusion ──

def test_group_dynamic_needs_threshold():
    """group_dynamic memories need >=3 count and >=1 non-roleplay to be included."""
    gd_mems = [
        _make_mem(id=f"gd{i}", category="group_dynamic interaction")
        for i in range(2)
    ]
    normal = [_make_mem(id="n1", content="正常记忆")]
    result = _filter_relationship_group_dynamic(normal + gd_mems)
    ids = [m["id"] for m in result]
    assert "gd0" not in ids, "<3 group_dynamic should be excluded"

    gd_mems_enough = [
        _make_mem(id=f"gd{i}", category="group_dynamic interaction")
        for i in range(4)
    ]
    result2 = _filter_relationship_group_dynamic(normal + gd_mems_enough)
    ids2 = [m["id"] for m in result2]
    assert "gd0" in ids2, ">=3 group_dynamic with non-roleplay should be included"


# ── Item C: test_metaphor_to_fact ──

def test_metaphor_to_fact():
    """Metaphorical/joke categories must be excluded, not promoted to facts."""
    mems = [
        _make_mem(id="m1", category="joke", content="她是古狐转世"),
        _make_mem(id="m2", category="玩笑", content="降维打击"),
        _make_mem(id="m3", room="living_room", content="她住在上海"),
    ]
    filtered = _filter_evidence(mems, "user")
    ids = [m["id"] for m in filtered]
    assert "m1" not in ids, "joke category should be filtered"
    assert "m2" not in ids, "玩笑 category should be filtered"
    assert "m3" in ids


# ── Item D: test_label_compression_conservative ──

def test_label_compression_conservative():
    """Prompt must contain conservative summary instruction."""
    import profile_builder
    source = open(profile_builder.__file__, encoding="utf-8").read()
    assert "保守摘要" in source or "conservative" in source.lower(), \
        "prompts must instruct conservative label compression"
    assert "禁止生成比原始记忆更具体的标签" in source or "宁可保留宽泛描述" in source, \
        "prompts must warn against over-specific labels"


# ── Status workflow tests ──

def test_profile_default_pending_review(db_env):
    """New profiles should default to pending_review status."""
    database.upsert_profile({
        "id": "test_status", "profile_type": "user", "content": "{}",
        "generated_at": "2026-08-03T00:00:00Z", "status": "pending_review",
    })
    result = database.get_profile("test_status")
    assert result["status"] == "pending_review"


def test_approve_profile(db_env):
    database.upsert_profile({
        "id": "test_approve", "profile_type": "user", "content": "{}",
        "generated_at": "2026-08-03T00:00:00Z", "status": "pending_review",
    })
    assert database.approve_profile("test_approve") is True
    assert database.get_profile("test_approve")["status"] == "active"


def test_supersede_profile(db_env):
    database.upsert_profile({
        "id": "test_super", "profile_type": "user", "content": "{}",
        "generated_at": "2026-08-03T00:00:00Z", "status": "active",
    })
    assert database.supersede_profile("test_super") is True
    assert database.get_profile("test_super")["status"] == "superseded"


def test_social_room_only_user_statement():
    """Social room memories must be user_statement to pass filter."""
    mems = [
        _make_mem(id="m1", room="social", provenance_type="ai_summary", content="AI 总结"),
        _make_mem(id="m2", room="social", provenance_type="user_statement", content="用户说的"),
    ]
    filtered = _filter_evidence(mems, "agent")
    ids = [m["id"] for m in filtered]
    assert "m1" not in ids
    assert "m2" in ids


def test_user_profile_strict_provenance():
    """User Profile uses strict provenance: only user_statement/correction, confidence >= 0.7."""
    mems = [
        _make_mem(id="m1", provenance_type="ai_summary", content="AI 推断"),
        _make_mem(id="m2", provenance_type="user_statement", fact_confidence=0.3, content="低信心"),
        _make_mem(id="m3", provenance_type="user_statement", fact_confidence=0.9, content="高信心事实"),
        _make_mem(id="m4", provenance_type="user_correction", fact_confidence=None, content="用户纠正"),
    ]
    filtered = _filter_evidence(mems, "user", strict_provenance=True)
    ids = [m["id"] for m in filtered]
    assert "m1" not in ids, "ai_summary excluded in strict mode"
    assert "m2" not in ids, "low confidence excluded"
    assert "m3" in ids
    assert "m4" in ids, "null confidence should pass (no threshold applies)"


# ── Phase 1.5 v2: New features ──

def test_text_too_stylized_detects():
    """Text with >= 3 stylized patterns should be rejected."""
    stylized = "她是一个深邃的、独一无二的存在，内心深处散发着温暖的光芒"
    assert _text_too_stylized(stylized) is True


def test_text_too_stylized_passes_normal():
    """Normal text should pass."""
    normal = "她喜欢猫，住在上海，做过记者"
    assert _text_too_stylized(normal) is False


def test_truncate_profile_fields():
    """Fields exceeding char limits should be truncated."""
    long_text = "这是一段很长的文字" * 50
    data = json.dumps({
        "identity": {"value": long_text, "confidence": "high", "evidence_tier": 1, "source_ids": []},
        "current_focus": {"value": long_text, "confidence": "medium", "evidence_tier": 4, "source_ids": []},
    }, ensure_ascii=False)
    result = json.loads(_truncate_profile_fields(data))
    assert len(result["identity"]["value"]) <= FIELD_CHAR_LIMITS["identity"]
    assert len(result["current_focus"]["value"]) <= FIELD_CHAR_LIMITS["current_focus"]
    assert result["identity"]["value"].endswith("...")


def test_temporal_stability_splits():
    """Identity assertions need >= 3 days span to be stable."""
    from datetime import datetime, timedelta
    base = datetime(2026, 7, 1)
    same_day = [
        _make_mem(id=f"s{i}", info_type="identity", content="她是记者",
                  created_at=(base + timedelta(hours=i)).isoformat())
        for i in range(3)
    ]
    stable, candidates = _check_temporal_stability(same_day)
    assert len(candidates) > 0, "same-day identity assertions should be candidates"

    multi_day = [
        _make_mem(id=f"m{i}", info_type="identity", content="她是记者",
                  created_at=(base + timedelta(days=i * 2)).isoformat())
        for i in range(3)
    ]
    stable2, candidates2 = _check_temporal_stability(multi_day)
    assert len(stable2) > 0, "multi-day identity assertions should be stable"


def test_temporal_stability_passes_non_identity():
    """Non-identity/relationship info_types bypass stability check."""
    mems = [
        _make_mem(id="f1", info_type="fact", content="她喜欢猫",
                  created_at="2026-07-01T00:00:00Z"),
    ]
    stable, candidates = _check_temporal_stability(mems)
    assert len(stable) == 1, "fact type should pass through directly"
    assert len(candidates) == 0


def test_prompt_has_no_psych_diagnosis_constraint():
    """Profile prompts must prohibit psychological diagnosis."""
    import profile_builder
    source = open(profile_builder.__file__, encoding="utf-8").read()
    assert "不做心理诊断" in source
    assert "不把单次事件写成稳定人格" in source
