# -*- coding: utf-8 -*-
"""
Phase 1.7 PR B — recent_interaction + corridor recency tests.

Covers:
  Block 5: recent_interaction(with_person, days, limit)
    - alias resolve (canonical + alias + case)
    - alias_not_found error + hint
    - time window strict
    - info_type=event only
    - resolved/superseded exclusion
    - visibility filtering
  Block 7: _pick_recency_weighted() helper
    - recent pool full
    - recent pool empty
    - only recent
    - only old
  Corridor snapshot:
    - 5 sections use recency weighting
    - 「近期重要事件」appears after diary
    - anchors unchanged

Run: ALLOW_DEFAULT_HUB_SECRET=1 python -m pytest tests/test_recent_and_corridor.py -q
"""
import os
import sys
import asyncio
from datetime import datetime, timezone, timedelta

os.environ.setdefault("ALLOW_DEFAULT_HUB_SECRET", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import database
import memory_ops
import corridor


# ════════════════════════════════════════════
#  Helpers
# ════════════════════════════════════════════

@pytest.fixture
def db_env(tmp_path):
    db_path = str(tmp_path / "test.db")
    database.DB_PATH = tmp_path / "test.db"
    asyncio.run(database.init_db(db_path))
    if hasattr(database, "_local"):
        database._local.read_conn = None
    yield tmp_path
    if hasattr(database, "_local"):
        database._local.read_conn = None


def _insert_person(pid, canonical, aliases=None):
    database.upsert_person({
        "person_id": pid,
        "entity_type": "human",
        "canonical_name": canonical,
        "aliases": aliases or [],
    })


def _insert_event(mid, content, subject_id="", source_actor_id="",
                  created_at=None, room="social", info_type="event",
                  resolved=None, superseded_by="", importance=0.5,
                  layer="shared", owner_ai="", source_ai="",
                  status="active"):
    conn = database._get_conn()
    now = created_at or datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO memories (id, content, room, status, provenance_type, layer, "
        "owner_ai, source_ai, created_at, updated_at, importance, category, tags, "
        "domain, resolved, superseded_by, info_type, subject_id, source_actor_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (mid, content, room, status, "user_statement", layer,
         owner_ai, source_ai, now, now, importance, "", "[]", "[]",
         resolved, superseded_by, info_type, subject_id, source_actor_id),
    )
    conn.commit()


# ════════════════════════════════════════════
#  Block 5: recent_interaction
# ════════════════════════════════════════════

class TestRecentInteraction:
    def test_alias_resolve_canonical(self, db_env):
        _insert_person("person_lucien", "Lucien", ["lucien", "狗蛋"])
        _insert_event("ev_1", "看塔罗", subject_id="person_lucien")
        result = asyncio.run(memory_ops.recent_interaction("Lucien"))
        assert result["resolved_to"] == "person_lucien"
        assert result["error"] == ""
        assert result["count"] == 1

    def test_alias_resolve_alias(self, db_env):
        _insert_person("person_jasper", "Jasper", ["狗蛋", "jasper"])
        _insert_event("ev_j", "陪打游戏", subject_id="person_jasper")
        result = asyncio.run(memory_ops.recent_interaction("狗蛋"))
        assert result["resolved_to"] == "person_jasper"
        assert result["count"] == 1

    def test_alias_not_found_returns_hint(self, db_env):
        _insert_person("person_lucien", "Lucien", ["lucien"])
        result = asyncio.run(memory_ops.recent_interaction("完全不存在的人"))
        assert result["resolved_to"] is None
        assert result["error"] == "alias_not_found"
        assert "完全不存在的人" in result["hint"]
        assert result["items"] == []
        # Hint must not be silent — it should mention how to fix
        assert "list_persons" in result["hint"] or "person_id" in result["hint"]

    def test_empty_person_error(self, db_env):
        result = asyncio.run(memory_ops.recent_interaction(""))
        assert result["error"] == "empty_person"

    def test_time_window_strict(self, db_env):
        _insert_person("person_lucien", "Lucien", ["lucien"])
        now = datetime.now(timezone.utc)
        _insert_event("ev_recent", "5天前",
                      subject_id="person_lucien",
                      created_at=(now - timedelta(days=5)).isoformat())
        _insert_event("ev_old", "40天前",
                      subject_id="person_lucien",
                      created_at=(now - timedelta(days=40)).isoformat())
        result = asyncio.run(memory_ops.recent_interaction("Lucien", days=30))
        ids = {i["id"] for i in result["items"]}
        assert "ev_recent" in ids
        assert "ev_old" not in ids

    def test_info_type_event_only(self, db_env):
        _insert_person("person_lucien", "Lucien", ["lucien"])
        _insert_event("ev_event", "看塔罗事件", subject_id="person_lucien",
                      info_type="event")
        _insert_event("ev_identity", "身份记录", subject_id="person_lucien",
                      info_type="identity")
        _insert_event("ev_reflection", "反思", subject_id="person_lucien",
                      info_type="reflection")
        result = asyncio.run(memory_ops.recent_interaction("Lucien"))
        ids = {i["id"] for i in result["items"]}
        assert "ev_event" in ids
        assert "ev_identity" not in ids
        assert "ev_reflection" not in ids

    def test_resolved_and_superseded_excluded(self, db_env):
        _insert_person("person_lucien", "Lucien", ["lucien"])
        _insert_event("ev_ok", "正常", subject_id="person_lucien")
        _insert_event("ev_resolved", "已解决", subject_id="person_lucien",
                      resolved=1)
        _insert_event("ev_supersede", "被替代", subject_id="person_lucien",
                      superseded_by="ev_ok")
        result = asyncio.run(memory_ops.recent_interaction("Lucien"))
        ids = {i["id"] for i in result["items"]}
        assert "ev_ok" in ids
        assert "ev_resolved" not in ids
        assert "ev_supersede" not in ids

    def test_source_actor_id_also_matches(self, db_env):
        """Memory where Lucien spoke (source_actor_id) should also count."""
        _insert_person("person_lucien", "Lucien", ["lucien"])
        _insert_event("ev_spoke", "Lucien 说的话",
                      source_actor_id="person_lucien")
        result = asyncio.run(memory_ops.recent_interaction("Lucien"))
        ids = {i["id"] for i in result["items"]}
        assert "ev_spoke" in ids

    def test_returns_time_desc_order(self, db_env):
        _insert_person("person_lucien", "Lucien", ["lucien"])
        now = datetime.now(timezone.utc)
        for i in range(5):
            _insert_event(f"ev_{i}", f"事件{i}",
                          subject_id="person_lucien",
                          created_at=(now - timedelta(days=i)).isoformat())
        result = asyncio.run(memory_ops.recent_interaction("Lucien", limit=5))
        ids = [i["id"] for i in result["items"]]
        assert ids == ["ev_0", "ev_1", "ev_2", "ev_3", "ev_4"]

    def test_visibility_filter_private(self, db_env):
        """Private memory owned by another AI must not appear."""
        _insert_person("person_lucien", "Lucien", ["lucien"])
        _insert_event("ev_private_jasper", "jasper 私密",
                      subject_id="person_lucien",
                      layer="private", owner_ai="jasper")
        _insert_event("ev_shared", "共享",
                      subject_id="person_lucien",
                      layer="shared")
        result = asyncio.run(
            memory_ops.recent_interaction("Lucien", ai_id="claude"))
        ids = {i["id"] for i in result["items"]}
        assert "ev_shared" in ids
        assert "ev_private_jasper" not in ids

    def test_limit_clamped(self, db_env):
        result = asyncio.run(
            memory_ops.recent_interaction("Lucien", days=1000, limit=999))
        assert result["days"] == 365  # clamped

    def test_return_shape(self, db_env):
        _insert_person("person_lucien", "Lucien", ["lucien"])
        _insert_event("ev_1", "内容",
                      subject_id="person_lucien",
                      source_actor_id="person_lucien",
                      importance=0.7, room="social")
        result = asyncio.run(memory_ops.recent_interaction("Lucien"))
        item = result["items"][0]
        assert set(item.keys()) == {
            "id", "content", "created_at", "room", "importance",
            "subject_id", "source_actor_id",
        }
        assert item["importance"] == 0.7
        assert item["subject_id"] == "person_lucien"


# ════════════════════════════════════════════
#  Block 7: _pick_recency_weighted
# ════════════════════════════════════════════

class TestPickRecencyWeighted:
    def _mem(self, mid, days_ago, importance=0.5, now=None):
        now = now or datetime(2026, 8, 12, tzinfo=timezone.utc)
        return {
            "id": mid,
            "content": mid,
            "importance": importance,
            "created_at": (now - timedelta(days=days_ago)).isoformat(),
        }

    def test_recent_pool_full_reserves_30_percent(self):
        """8 quota, plenty of recent + old — recent pool gets ceil(8*0.3)=3."""
        now = datetime(2026, 8, 12, tzinfo=timezone.utc)
        candidates = (
            [self._mem(f"new_{i}", days_ago=5, importance=0.5, now=now)
             for i in range(10)]
            + [self._mem(f"old_{i}", days_ago=90, importance=0.9, now=now)
               for i in range(10)]
        )
        picked = corridor._pick_recency_weighted(candidates, quota=8, now_utc=now)
        assert len(picked) == 8
        new_count = sum(1 for m in picked if m["id"].startswith("new_"))
        assert new_count == 3, f"expected 3 new, got {new_count}"

    def test_recent_pool_small_uses_old_fallback(self):
        """quota=8, only 1 recent — remaining 7 must come from old pool."""
        now = datetime(2026, 8, 12, tzinfo=timezone.utc)
        candidates = (
            [self._mem("new_1", days_ago=5, importance=0.5, now=now)]
            + [self._mem(f"old_{i}", days_ago=90, importance=0.9, now=now)
               for i in range(10)]
        )
        picked = corridor._pick_recency_weighted(candidates, quota=8, now_utc=now)
        assert len(picked) == 8
        assert sum(1 for m in picked if m["id"].startswith("new_")) == 1
        assert sum(1 for m in picked if m["id"].startswith("old_")) == 7

    def test_all_recent(self):
        now = datetime(2026, 8, 12, tzinfo=timezone.utc)
        candidates = [self._mem(f"new_{i}", days_ago=i, importance=0.5, now=now)
                      for i in range(5)]
        picked = corridor._pick_recency_weighted(candidates, quota=5, now_utc=now)
        assert len(picked) == 5

    def test_all_old(self):
        now = datetime(2026, 8, 12, tzinfo=timezone.utc)
        candidates = [self._mem(f"old_{i}", days_ago=60 + i, importance=0.9 - i * 0.1, now=now)
                      for i in range(5)]
        picked = corridor._pick_recency_weighted(candidates, quota=3, now_utc=now)
        assert len(picked) == 3
        # Highest importance first
        assert picked[0]["id"] == "old_0"
        assert picked[1]["id"] == "old_1"

    def test_no_duplicates_across_pools(self):
        """A memory must appear at most once in the picked list."""
        now = datetime(2026, 8, 12, tzinfo=timezone.utc)
        candidates = [self._mem(f"m_{i}", days_ago=i * 20, importance=0.5, now=now)
                      for i in range(10)]
        picked = corridor._pick_recency_weighted(candidates, quota=10, now_utc=now)
        ids = [m["id"] for m in picked]
        assert len(ids) == len(set(ids))

    def test_empty_input_returns_empty(self):
        assert corridor._pick_recency_weighted([], quota=5) == []

    def test_quota_zero_returns_empty(self):
        m = self._mem("a", 5)
        assert corridor._pick_recency_weighted([m], quota=0) == []


# ════════════════════════════════════════════
#  Corridor snapshot: structure invariants
# ════════════════════════════════════════════

class TestCorridorSnapshot:
    def test_new_section_appears_when_high_importance_recent(self, db_env):
        """A 5-day-old high-importance memory in a random room should appear
        in the new 【近期重要事件】section."""
        _insert_event("recent_hi", "高价值新事件",
                      room="experiments",  # not in any hard-coded section
                      importance=0.8,
                      created_at=(datetime.now(timezone.utc)
                                  - timedelta(days=5)).isoformat())
        text = asyncio.run(corridor.build_corridor("claude"))
        assert "【近期重要事件】" in text
        assert "高价值新事件" in text

    def test_new_section_absent_when_no_qualifying_memory(self, db_env):
        """No memory meets 14-day + importance≥0.6 → section absent."""
        _insert_event("old_hi", "老事件",
                      room="experiments", importance=0.9,
                      created_at=(datetime.now(timezone.utc)
                                  - timedelta(days=60)).isoformat())
        _insert_event("recent_low", "新但不重要",
                      room="experiments", importance=0.3,
                      created_at=(datetime.now(timezone.utc)
                                  - timedelta(days=2)).isoformat())
        text = asyncio.run(corridor.build_corridor("claude"))
        assert "【近期重要事件】" not in text

    def test_new_section_placed_after_diary(self, db_env):
        _insert_event("diary_recent", "日记内容",
                      room="diary", owner_ai="claude", importance=0.5,
                      created_at=(datetime.now(timezone.utc)
                                  - timedelta(days=2)).isoformat())
        _insert_event("event_recent", "重要事件",
                      room="experiments", importance=0.9,
                      created_at=(datetime.now(timezone.utc)
                                  - timedelta(days=3)).isoformat())
        text = asyncio.run(corridor.build_corridor("claude"))
        diary_pos = text.find("【你最近的日记】")
        event_pos = text.find("【近期重要事件】")
        assert diary_pos != -1 and event_pos != -1
        assert event_pos > diary_pos

    def test_recency_boosts_new_living_room(self, db_env):
        """Fill living_room with 10 old + 3 new; 3 new must appear in the
        first 8 picks (recent_share=0.3, quota=8, so 3 slots for new)."""
        now = datetime.now(timezone.utc)
        for i in range(10):
            _insert_event(f"old_liv_{i}", f"老客厅{i}",
                          room="living_room", importance=0.7,
                          created_at=(now - timedelta(days=90 + i)).isoformat())
        for i in range(3):
            _insert_event(f"new_liv_{i}", f"新客厅{i}",
                          room="living_room", importance=0.5,
                          created_at=(now - timedelta(days=i + 1)).isoformat())
        text = asyncio.run(corridor.build_corridor("claude"))
        for i in range(3):
            assert f"新客厅{i}" in text, \
                f"新客厅{i} missing — recent_share not applied to living_room"

    def test_anchors_unaffected_by_recency(self, db_env):
        """Anchored memory of any age must still appear in 【锚点·不变的事】."""
        _insert_event("anchor_old", "永远的原则",
                      room="misc", importance=0.9,
                      created_at=(datetime.now(timezone.utc)
                                  - timedelta(days=500)).isoformat())
        # Set anchored=1 via direct SQL
        conn = database._get_conn()
        conn.execute("UPDATE memories SET anchored = 1 WHERE id = ?", ("anchor_old",))
        conn.commit()
        text = asyncio.run(corridor.build_corridor("claude"))
        assert "永远的原则" in text
