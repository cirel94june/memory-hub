"""AI-synthesized profile text must not be labeled [用户] (reads as the user's own words)."""
import os
import sys
import asyncio

os.environ.setdefault("ALLOW_DEFAULT_HUB_SECRET", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import daemon


@pytest.fixture
def fake_store(monkeypatch):
    mems = {}

    async def noop_push():
        return None

    monkeypatch.setattr(daemon.store, "get_all_memories", lambda: mems)
    monkeypatch.setattr(daemon.store, "set_memory", lambda m: mems.__setitem__(m["id"], m))
    monkeypatch.setattr(daemon.store, "push_dirty", noop_push)
    return mems


def _mem(mid, content, **kw):
    base = {"id": mid, "content": content, "status": "active", "room": "living_room",
            "provenance_type": "", "source_platform": "", "comments": []}
    base.update(kw)
    return base


def test_mislabeled_refresh_entry_gets_ai_prefix_and_note(fake_store):
    original = "这是NPD依恋创伤的典型症状，不是反省而是植入的辩护程序。"
    fake_store["m1"] = _mem("m1", f"[用户] {original}",
                            provenance_type="ai_summary", source_platform="living_room_refresh")
    asyncio.run(daemon._auto_fix_about_prefix())
    m = fake_store["m1"]
    assert m["content"] == f"[AI解读] {original}"
    assert any(c.get("kind") == "provenance_note" for c in m["comments"])


def test_unprefixed_refresh_entry_gets_ai_prefix(fake_store):
    fake_store["m2"] = _mem("m2", "用户经常提到狗蛋", source_platform="living_room_refresh",
                            provenance_type="ai_summary")
    asyncio.run(daemon._auto_fix_about_prefix())
    assert fake_store["m2"]["content"] == "[AI解读] 用户经常提到狗蛋"


def test_speculation_gets_ai_prefix(fake_store):
    fake_store["m3"] = _mem("m3", "[用户] 她可能是在逃避", provenance_type="ai_speculation")
    asyncio.run(daemon._auto_fix_about_prefix())
    assert fake_store["m3"]["content"].startswith("[AI解读]")


def test_extracted_user_fact_keeps_user_prefix(fake_store):
    """Chat extraction also marks ai_summary; real user facts must stay [用户]."""
    fake_store["m4"] = _mem("m4", "[用户] Ceci喜欢吃芒果", provenance_type="ai_summary",
                            source_platform="telegram:private")
    asyncio.run(daemon._auto_fix_about_prefix())
    assert fake_store["m4"]["content"] == "[用户] Ceci喜欢吃芒果"
    assert fake_store["m4"]["comments"] == []


def test_idempotent(fake_store):
    fake_store["m5"] = _mem("m5", "[用户] 解读", source_platform="living_room_refresh")
    asyncio.run(daemon._auto_fix_about_prefix())
    asyncio.run(daemon._auto_fix_about_prefix())
    m = fake_store["m5"]
    assert m["content"] == "[AI解读] 解读"
    assert len(m["comments"]) == 1
