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
    _gather_memories,
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
