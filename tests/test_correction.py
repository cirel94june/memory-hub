"""
记忆漂移防护回归测试（对应 2026-07 Lucien MCP 审计追加项）。

场景：Jasper 把"真丝裤衩"误说成"真丝羽毛"，被自动捕获成记忆；
用户纠正"不是，是真丝裤衩"。

预期：
- canonical = 真丝裤衩（provenance=user_correction，confidence=1.0）
- AI 错误版被标记 corrected_by_user，不再是 active
- smart_context / 走廊 / 召回（都只取 active）不再出现真丝羽毛
- 错误版在 history/detail 中可追溯（superseded_by 指向纠正版）
- AI 生成内容（ai_summary/roleplay_meme/dream/diary）永远不能 supersede 用户事实

运行：ALLOW_DEFAULT_HUB_SECRET=1 python -m pytest tests/ -q
"""
import os
import sys
import json
import asyncio
from datetime import datetime, timezone

os.environ.setdefault("ALLOW_DEFAULT_HUB_SECRET", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import memory_ops
from memory_ops import _can_supersede


# ── supersede 守卫矩阵 ──

USER_FACT = {"id": "u1", "provenance_type": "user_statement", "content": "狗蛋穿真丝裤衩"}
USER_CORRECTION = {"id": "u2", "provenance_type": "user_correction", "content": "是真丝裤衩"}
LEGACY_FACT = {"id": "u3", "provenance_type": "", "content": "旧记忆没有出处字段"}


def test_ai_content_never_supersedes():
    for prov in ("ai_summary", "ai_speculation", "roleplay_meme", "dream", "diary"):
        assert not _can_supersede(prov, USER_FACT), prov
        assert not _can_supersede(prov, LEGACY_FACT), prov
        assert not _can_supersede(prov, USER_CORRECTION), prov


def test_user_correction_supersedes_everything():
    assert _can_supersede("user_correction", USER_FACT)
    assert _can_supersede("user_correction", USER_CORRECTION)
    assert _can_supersede("user_correction", LEGACY_FACT)


def test_user_statement_cannot_supersede_correction():
    assert _can_supersede("user_statement", USER_FACT)
    assert not _can_supersede("user_statement", USER_CORRECTION)


def test_legacy_writer_cannot_supersede_correction():
    """出处未知的写入（bot 直写/旧路径）保持旧行为，但不能覆盖用户纠正。"""
    assert _can_supersede("", USER_FACT)
    assert not _can_supersede("", USER_CORRECTION)


# ── 真丝裤衩端到端场景（fake store）──

@pytest.fixture
def fake_store(monkeypatch):
    """内存版 store + 短路 remember 的重活（embedding/analyzer）。"""
    mems = {}

    def get_all():
        return mems

    def get_one(mid):
        return mems.get(mid)

    def set_one(m):
        mems[m["id"]] = m

    import github_store
    monkeypatch.setattr(github_store, "get_all_memories", get_all)
    monkeypatch.setattr(github_store, "get_memory", get_one)
    monkeypatch.setattr(github_store, "set_memory", set_one)
    monkeypatch.setattr(memory_ops, "store", github_store)

    # remember 里的纠正路径用 force_create + auto_analyze=False，
    # 只需要短路 embedding 与 write_gate 相关部分
    async def fake_remember(content, **kw):
        mid = f"mem_{len(mems)}"
        m = {
            "id": mid,
            "content": content,
            "layer": kw.get("layer", "shared"),
            "room": kw.get("room", "living_room"),
            "owner_ai": kw.get("owner_ai", ""),
            "source_ai": kw.get("source_ai", ""),
            "status": "active",
            "importance": kw.get("importance", 0.5),
            "provenance_type": kw.get("provenance_type", ""),
            "fact_confidence": kw.get("fact_confidence"),
            "tags": json.dumps(kw.get("tags") or []),
            "comments": [],
            "superseded_by": "",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        mems[mid] = m
        return {"id": mid, "status": "created"}

    monkeypatch.setattr(memory_ops, "remember", fake_remember)
    return mems


def test_correction_invalidates_wrong_ai_memory(fake_store):
    mems = fake_store
    # 1. AI 错误版先入库（自动捕获 Jasper 的复述）
    wrong = {
        "id": "wrong1",
        "content": "[互动] Jasper说狗蛋今天穿了真丝羽毛",
        "layer": "shared", "room": "social", "owner_ai": "",
        "source_ai": "jasper", "status": "active",
        "provenance_type": "ai_summary", "fact_confidence": 0.5,
        "tags": "[]", "comments": [], "superseded_by": "",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    mems["wrong1"] = wrong

    # 2. 用户纠正
    result = asyncio.run(memory_ops.apply_user_correction(
        corrected_value="[用户] ceci纠正：狗蛋穿的是真丝裤衩，不是真丝羽毛",
        old_value="真丝羽毛",
        source_ai="jasper",
        room="social",
    ))

    # canonical 事实存在且是最高置信度
    canonical = mems[result["canonical_id"]]
    assert "真丝裤衩" in canonical["content"]
    assert canonical["provenance_type"] == "user_correction"
    assert canonical["fact_confidence"] == 1.0
    assert canonical["status"] == "active"

    # 错误版不再 active，可追溯到纠正版
    assert "wrong1" in result["invalidated"]
    assert mems["wrong1"]["status"] == "corrected_by_user"
    assert mems["wrong1"]["superseded_by"] == result["canonical_id"]
    assert any(c.get("kind") == "correction_note" for c in mems["wrong1"]["comments"])


def test_correction_does_not_invalidate_canonical_itself(fake_store):
    """纠正版 content 里也包含错误值（"不是真丝羽毛"）——不能把自己标失效。"""
    result = asyncio.run(memory_ops.apply_user_correction(
        corrected_value="是真丝裤衩，不是真丝羽毛",
        old_value="真丝羽毛",
    ))
    canonical = fake_store[result["canonical_id"]]
    assert canonical["status"] == "active"
    assert result["canonical_id"] not in result["invalidated"]


def test_correction_without_match_marks_conflict_pending(fake_store):
    """定位不到错误来源时不乱覆盖，标记 conflict_pending。"""
    result = asyncio.run(memory_ops.apply_user_correction(
        corrected_value="狗蛋穿的是真丝裤衩",
        old_value="不存在的错误说法XYZ",
    ))
    assert result["invalidated"] == []
    canonical = fake_store[result["canonical_id"]]
    assert "conflict_pending" in json.loads(canonical["tags"])


def test_corrected_memory_excluded_from_smart_context(fake_store, monkeypatch):
    """corrected_by_user 状态的记忆不得再进 smart_context。"""
    mems = fake_store
    now = datetime.now(timezone.utc).isoformat()
    mems["wrong1"] = {
        "id": "wrong1", "content": "Jasper说狗蛋穿了真丝羽毛",
        "layer": "shared", "room": "social", "owner_ai": "",
        "source_ai": "jasper", "status": "corrected_by_user",
        "provenance_type": "ai_summary", "importance": 0.9,
        "tags": "[]", "comments": [], "superseded_by": "ok1",
        "resolved": None, "updated_at": now, "created_at": now,
    }
    mems["ok1"] = {
        "id": "ok1", "content": "ceci纠正：狗蛋穿的是真丝裤衩",
        "layer": "shared", "room": "social", "owner_ai": "",
        "source_ai": "jasper", "status": "active",
        "provenance_type": "user_correction", "importance": 0.85,
        "tags": "[]", "comments": [], "superseded_by": "",
        "resolved": None, "updated_at": now, "created_at": now,
    }

    import smart_context
    monkeypatch.setattr(smart_context, "_dream_section", lambda ai, md: "")
    result = asyncio.run(smart_context.get_smart_context(
        ai_id="lucien", has_base_context=True, max_chars=4000))
    assert "真丝羽毛" not in result["text"]
    assert "真丝裤衩" in result["text"]


# ── 正文完整性 ──

def test_looks_incomplete_detection():
    from integrity import looks_incomplete
    # 半句残缺（Lucien 复测发现的实际样式）
    bad, reason = looks_incomplete("我梦见一条很长很长的走廊，走廊尽头有一扇发着微光的门，推开之后里面像是有个什么系统管")
    assert bad and reason == "no_terminal_punctuation"
    # 未闭合括号
    bad, _ = looks_incomplete("Lucien（昵称狐狸曾经说过一句很长的话所以这条一定超过三十个字符了")
    assert bad
    # 正常内容不误伤
    ok_cases = [
        "这是一条完整的记忆，有头有尾，字数也足够长，最后以句号结束。",
        "短标签",
        "她说：“今天很开心。”",
        "这条以感叹结尾的长记忆内容也是完整的，不应该被标记出来！",
    ]
    for t in ok_cases:
        bad, reason = looks_incomplete(t)
        assert not bad, f"{t} -> {reason}"


def test_incomplete_memory_skipped_in_recent(fake_store, monkeypatch):
    import json as _json
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    fake_store["半句"] = {
        "id": "半句", "content": "这条记忆停在半句像是有个什么系统管",
        "layer": "shared", "room": "living_room", "owner_ai": "",
        "source_ai": "jasper", "status": "active", "importance": 0.9,
        "tags": _json.dumps(["content_incomplete"]), "comments": [],
        "superseded_by": "", "resolved": None,
        "updated_at": now, "created_at": now,
    }
    fake_store["完整"] = {
        "id": "完整", "content": "这条是完整的记忆。",
        "layer": "shared", "room": "living_room", "owner_ai": "",
        "source_ai": "jasper", "status": "active", "importance": 0.9,
        "tags": "[]", "comments": [], "superseded_by": "", "resolved": None,
        "updated_at": now, "created_at": now,
    }
    import smart_context
    monkeypatch.setattr(smart_context, "_dream_section", lambda ai, md: "")
    result = asyncio.run(smart_context.get_smart_context(
        ai_id="lucien", has_base_context=True, max_chars=4000))
    assert "完整的记忆" in result["text"]
    assert "什么系统管" not in result["text"]
