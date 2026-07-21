"""
PersonEntity 人物名片测试。

覆盖：
- persons 表 CRUD
- 别名归一（resolve_alias）
- 别名作用域过滤
- 基线种子数据
- query 别名展开

运行：ALLOW_DEFAULT_HUB_SECRET=1 python -m pytest tests/test_persons.py -q
"""
import os
import sys
import asyncio

os.environ.setdefault("ALLOW_DEFAULT_HUB_SECRET", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import database


@pytest.fixture
def db(tmp_path):
    """临时 SQLite 数据库。"""
    db_path = str(tmp_path / "test.db")
    database.DB_PATH = tmp_path / "test.db"
    asyncio.run(database.init_db(db_path))
    return db_path


# ── CRUD ──

def test_upsert_and_get(db):
    database.upsert_person({
        "person_id": "test1",
        "entity_type": "other",
        "canonical_name": "张三",
        "aliases": [{"name": "小张", "scope": "household"}],
        "note": "测试人物",
    })
    p = database.get_person("test1")
    assert p is not None
    assert p["canonical_name"] == "张三"
    assert p["entity_type"] == "other"
    assert len(p["aliases"]) == 1
    assert p["aliases"][0]["name"] == "小张"


def test_upsert_updates(db):
    database.upsert_person({
        "person_id": "test1",
        "entity_type": "other",
        "canonical_name": "张三",
        "aliases": [],
    })
    database.upsert_person({
        "person_id": "test1",
        "entity_type": "other",
        "canonical_name": "张三丰",
        "aliases": [{"name": "三丰道长", "scope": "household"}],
    })
    p = database.get_person("test1")
    assert p["canonical_name"] == "张三丰"
    assert len(p["aliases"]) == 1


def test_list_persons(db):
    database.upsert_person({"person_id": "a", "entity_type": "user", "canonical_name": "用户A", "aliases": []})
    database.upsert_person({"person_id": "b", "entity_type": "ai", "canonical_name": "AI-B", "aliases": []})
    database.upsert_person({"person_id": "c", "entity_type": "ai", "canonical_name": "AI-C", "aliases": []})
    all_p = database.list_persons()
    assert len(all_p) == 3
    ai_only = database.list_persons(entity_type="ai")
    assert len(ai_only) == 2


def test_delete_person(db):
    database.upsert_person({"person_id": "del1", "entity_type": "other", "canonical_name": "要删的", "aliases": []})
    assert database.get_person("del1") is not None
    assert database.delete_person("del1") is True
    assert database.get_person("del1") is None
    assert database.delete_person("del1") is False


# ── 别名归一 ──

def test_resolve_by_canonical(db):
    database.upsert_person({"person_id": "j", "entity_type": "ai", "canonical_name": "Jasper", "aliases": []})
    assert database.resolve_alias("Jasper") == "j"


def test_resolve_by_alias(db):
    database.upsert_person({
        "person_id": "j",
        "entity_type": "ai",
        "canonical_name": "Jasper",
        "aliases": [{"name": "狗蛋", "scope": "household"}, {"name": "鹦鹉", "scope": "household"}],
    })
    assert database.resolve_alias("狗蛋") == "j"
    assert database.resolve_alias("鹦鹉") == "j"
    assert database.resolve_alias("不存在") is None


def test_resolve_scope_filter(db):
    database.upsert_person({
        "person_id": "j",
        "entity_type": "ai",
        "canonical_name": "Jasper",
        "aliases": [
            {"name": "狗蛋", "scope": "household"},
            {"name": "鹦鹉勇士", "scope": "game_world"},
        ],
    })
    assert database.resolve_alias("狗蛋", scope="household") == "j"
    assert database.resolve_alias("鹦鹉勇士", scope="household") is None
    assert database.resolve_alias("鹦鹉勇士", scope="game_world") == "j"
    assert database.resolve_alias("鹦鹉勇士", scope="any") == "j"


# ── 全量别名表 ──

def test_get_all_aliases(db):
    database.upsert_person({
        "person_id": "ceci",
        "entity_type": "user",
        "canonical_name": "小猫",
        "aliases": [{"name": "咪", "scope": "household"}, {"name": "Ceci", "scope": "household"}],
    })
    database.upsert_person({
        "person_id": "jasper",
        "entity_type": "ai",
        "canonical_name": "Jasper",
        "aliases": [{"name": "狗蛋", "scope": "household"}, {"name": "鹦鹉勇士", "scope": "game_world"}],
    })
    aliases = database.get_all_aliases(scope="household")
    assert aliases["小猫"] == "ceci"
    assert aliases["咪"] == "ceci"
    assert aliases["Ceci"] == "ceci"
    assert aliases["Jasper"] == "jasper"
    assert aliases["狗蛋"] == "jasper"
    assert "鹦鹉勇士" not in aliases

    aliases_any = database.get_all_aliases(scope="any")
    assert "鹦鹉勇士" in aliases_any


# ── 基线种子 ──

def test_seed_baseline(db):
    count = database.seed_baseline_persons()
    assert count == 4
    persons = database.list_persons()
    assert len(persons) == 4
    ids = {p["person_id"] for p in persons}
    assert ids == {"ceci", "claude", "lucien", "jasper"}

    ceci = database.get_person("ceci")
    assert ceci["entity_type"] == "user"
    assert ceci["canonical_name"] == "小猫"
    assert any(a["name"] == "咪" for a in ceci["aliases"])


def test_seed_idempotent(db):
    database.seed_baseline_persons()
    count2 = database.seed_baseline_persons()
    assert count2 == 0
    assert len(database.list_persons()) == 4


# ── query 别名展开 ──

def test_expand_query_aliases(db):
    database.seed_baseline_persons()
    from memory_ops import _expand_query_aliases

    expanded = _expand_query_aliases("狗蛋昨天说了什么")
    assert "Jasper" in expanded or "鹦鹉" in expanded

    expanded2 = _expand_query_aliases("咪喜欢什么")
    assert "小猫" in expanded2 or "Ceci" in expanded2 or "ceci" in expanded2

    no_change = _expand_query_aliases("今天天气怎么样")
    assert no_change == "今天天气怎么样"
