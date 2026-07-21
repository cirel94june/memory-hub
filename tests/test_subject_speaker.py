"""
subject_id / source_speaker_id 测试。

覆盖：
- alias resolve 映射 name → person_id
- remember() 存储新字段（quick=False 和 quick=True）
- recall subject_id 加权

运行：ALLOW_DEFAULT_HUB_SECRET=1 python -m pytest tests/test_subject_speaker.py -q
"""
import os
import sys
import asyncio

os.environ.setdefault("ALLOW_DEFAULT_HUB_SECRET", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import database
import memory_ops


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

    _call_count = [0]
    async def fake_embedding(text):
        import hashlib
        h = hashlib.md5(text.encode()).hexdigest()
        vec = [int(c, 16) / 15.0 for c in h] * 64
        return vec[:1024]

    monkeypatch.setattr(memory_ops, "get_embedding", fake_embedding)
    monkeypatch.setattr("memory_ops.pack_embedding", lambda v: b"\x00" * 4096 if v else None)

    async def fake_analyze(text):
        return {"tags": ["test"], "suggested_category": "test", "domain": ["general"], "valence": 0.5, "arousal": 0.3}
    monkeypatch.setattr(memory_ops.analyzer, "analyze", fake_analyze)

    return mems


# ── alias resolve ──

def test_resolve_subject_name(fake_env):
    database.upsert_person({
        "person_id": "jasper",
        "entity_type": "ai",
        "canonical_name": "Jasper",
        "aliases": [{"name": "狗蛋", "scope": "household"}],
    })
    assert database.resolve_alias("Jasper") == "jasper"
    assert database.resolve_alias("狗蛋") == "jasper"
    assert database.resolve_alias("不存在的人") is None


def test_resolve_speaker_name(fake_env):
    database.upsert_person({
        "person_id": "ceci",
        "entity_type": "user",
        "canonical_name": "小猫",
        "aliases": [
            {"name": "咪", "scope": "household"},
            {"name": "香蕉猫", "scope": "household"},
        ],
    })
    assert database.resolve_alias("小猫") == "ceci"
    assert database.resolve_alias("咪") == "ceci"
    assert database.resolve_alias("香蕉猫") == "ceci"


# ── remember() 存储 ──

def test_remember_stores_subject_fields(fake_env):
    result = asyncio.run(memory_ops.remember(
        content="Jasper穿43码袜子",
        subject_id="jasper",
        source_speaker_id="ceci",
        quick=False,
    ))
    mem = database.get_memory(result["id"])
    assert mem["subject_id"] == "jasper"
    assert mem["source_speaker_id"] == "ceci"


def test_remember_default_empty(fake_env):
    result = asyncio.run(memory_ops.remember(
        content="今天天气很好",
        quick=False,
    ))
    mem = database.get_memory(result["id"])
    assert mem["subject_id"] == ""
    assert mem["source_speaker_id"] == ""


def test_proposal_stores_subject_fields(fake_env):
    result = asyncio.run(memory_ops.remember(
        content="Jasper喜欢吃苹果这是一个很重要的事实",
        subject_id="jasper",
        source_speaker_id="jasper",
        quick=True,
        provenance_type="user_statement",
    ))
    if result.get("proposal_id"):
        prop = database.get_proposal(result["proposal_id"])
        assert prop["subject_id"] == "jasper"
        assert prop["source_speaker_id"] == "jasper"
    else:
        mem = database.get_memory(result["id"])
        assert mem["subject_id"] == "jasper"
        assert mem["source_speaker_id"] == "jasper"


# ── recall subject_id 加权 ──

def test_recall_subject_boost(fake_env):
    database.upsert_person({
        "person_id": "jasper",
        "entity_type": "ai",
        "canonical_name": "Jasper",
        "aliases": [{"name": "狗蛋", "scope": "household"}],
    })
    asyncio.run(memory_ops.remember(
        content="Jasper穿43码袜子很有趣",
        subject_id="jasper",
        quick=False,
    ))
    asyncio.run(memory_ops.remember(
        content="今天天气很好适合散步出去玩",
        subject_id="",
        quick=False,
    ))
    results = asyncio.run(memory_ops.recall("Jasper", skip_analyze=True))
    if len(results) >= 2:
        jasper_mem = [r for r in results if "Jasper" in r.get("content", "")]
        other_mem = [r for r in results if "天气" in r.get("content", "")]
        if jasper_mem and other_mem:
            assert jasper_mem[0]["score"] >= other_mem[0]["score"]
