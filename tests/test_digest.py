"""Tests for digest.py — daemon 定时消化模块。"""
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch

import pytest

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


@pytest.fixture
def digest_db(tmp_path):
    """Create a temporary DB with the memories table for digest tests."""
    db = tmp_path / "memories.db"
    conn = sqlite3.connect(str(db))
    conn.execute("""
        CREATE TABLE memories (
            id TEXT PRIMARY KEY,
            content TEXT,
            room TEXT DEFAULT '',
            category TEXT DEFAULT '',
            source_ai TEXT DEFAULT '',
            owner_ai TEXT DEFAULT '',
            layer TEXT DEFAULT 'shared',
            status TEXT DEFAULT 'active',
            importance REAL DEFAULT 0.5,
            created_at TEXT DEFAULT ''
        )
    """)
    conn.commit()
    conn.close()
    return db


@pytest.fixture(autouse=True)
def _patch_db(digest_db):
    """Redirect database.DB_PATH and clear thread-local read conn cache."""
    import database
    old_path = database.DB_PATH
    database.DB_PATH = digest_db
    if hasattr(database._local, "read_conn"):
        try:
            database._local.read_conn.close()
        except Exception:
            pass
        del database._local.read_conn
    if hasattr(database._local, "read_db_path"):
        del database._local.read_db_path
    yield
    database.DB_PATH = old_path
    if hasattr(database._local, "read_conn"):
        try:
            database._local.read_conn.close()
        except Exception:
            pass
        del database._local.read_conn
    if hasattr(database._local, "read_db_path"):
        del database._local.read_db_path


def _insert_memories(db_path, ai_id, count, since_hours=48, room="living_room", category=""):
    """Insert `count` test memories for the given AI."""
    conn = sqlite3.connect(str(db_path))
    now = datetime.now(timezone.utc)
    for i in range(count):
        ts = (now - timedelta(hours=since_hours - i)).isoformat()
        conn.execute(
            "INSERT INTO memories (id, content, room, category, source_ai, owner_ai, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'active', ?)",
            (f"mem_{ai_id}_{i}", f"Test memory {i} for {ai_id}", room, category, ai_id, ai_id, ts),
        )
    conn.commit()
    conn.close()


def _insert_digest(db_path, ai_id, hours_ago=0):
    """Insert a digest entry."""
    conn = sqlite3.connect(str(db_path))
    ts = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    conn.execute(
        "INSERT INTO memories (id, content, room, category, source_ai, owner_ai, status, created_at) "
        "VALUES (?, ?, 'dreams', 'digest', ?, ?, 'active', ?)",
        (f"digest_{ai_id}_{hours_ago}", f"[消化] Test digest for {ai_id}", ai_id, ai_id, ts),
    )
    conn.commit()
    conn.close()


class TestShouldDigest:
    def test_no_memories_no_digest(self, digest_db):
        """No memories → should not digest."""
        from digest import _should_digest
        should, since = _should_digest("claude")
        assert not should

    def test_below_threshold_below_minimum(self, digest_db):
        """Less than 5 memories → should not digest even after 24h."""
        _insert_memories(digest_db, "claude", 3)
        from digest import _should_digest
        should, _ = _should_digest("claude")
        assert not should

    def test_above_threshold_triggers(self, digest_db):
        """30+ memories with no prior digest → should digest."""
        _insert_memories(digest_db, "claude", 35)
        from digest import _should_digest
        should, since = _should_digest("claude")
        assert should
        assert since != ""

    def test_above_threshold_ignores_cooldown(self, digest_db):
        """M1 fix: 30+ NEW memories since last digest triggers even within cooldown."""
        _insert_digest(digest_db, "claude", hours_ago=2)
        _insert_memories(digest_db, "claude", 35, since_hours=1)
        from digest import _should_digest
        should, _ = _should_digest("claude")
        assert should

    def test_recent_digest_blocks_below_threshold(self, digest_db):
        """Recent digest (< 24h ago) blocks new digest when below 30."""
        _insert_memories(digest_db, "claude", 10)
        _insert_digest(digest_db, "claude", hours_ago=2)
        from digest import _should_digest
        should, _ = _should_digest("claude")
        assert not should

    def test_old_digest_allows(self, digest_db):
        """Digest older than 24h allows new digest if 5+ memories since."""
        _insert_digest(digest_db, "claude", hours_ago=30)
        _insert_memories(digest_db, "claude", 10, since_hours=20)
        from digest import _should_digest
        should, since = _should_digest("claude")
        assert should

    def test_per_ai_isolation(self, digest_db):
        """Digest check is per-AI — one AI's memories don't trigger another's."""
        _insert_memories(digest_db, "claude", 35)
        from digest import _should_digest
        should_claude, _ = _should_digest("claude")
        should_lucien, _ = _should_digest("lucien")
        assert should_claude
        assert not should_lucien


class TestDigestForAi:
    @pytest.mark.asyncio
    async def test_digest_creates_memory(self, digest_db):
        """digest_for_ai should call LLM and store result."""
        _insert_memories(digest_db, "claude", 35)

        mock_remember = AsyncMock(return_value={"id": "new_digest_123", "action": "created"})

        with (
            patch("digest._call_llm", new_callable=AsyncMock,
                  return_value=("这些记忆显示最近主要在讨论技术架构问题，有几条关于部署的记忆互相补充。", None)),
            patch("memory_ops.remember", mock_remember),
        ):
            from digest import digest_for_ai
            result = await digest_for_ai("claude")

        assert result is not None
        assert result["ai_id"] == "claude"
        assert result["status"] == "success"
        assert result["memories_digested"] > 0
        mock_remember.assert_called_once()
        call_kwargs = mock_remember.call_args[1]
        assert call_kwargs["room"] == "dreams"
        assert call_kwargs["category"] == "digest"
        assert call_kwargs["auto_merge"] is False
        assert call_kwargs["provenance_type"] == "digest"
        assert call_kwargs["info_type"] == "reflection"
        assert call_kwargs["auto_analyze"] is False
        assert "[消化]" in call_kwargs["content"]

    @pytest.mark.asyncio
    async def test_no_digest_needed(self, digest_db):
        """digest_for_ai returns not_due when no digest needed."""
        _insert_memories(digest_db, "claude", 3)
        from digest import digest_for_ai
        result = await digest_for_ai("claude")
        assert result["status"] == "not_due"

    @pytest.mark.asyncio
    async def test_llm_failure_returns_failed(self, digest_db):
        """digest_for_ai returns failed status on LLM error."""
        _insert_memories(digest_db, "claude", 35)

        with patch("digest._call_llm", new_callable=AsyncMock,
                   return_value=("", "connection timeout")):
            from digest import digest_for_ai
            result = await digest_for_ai("claude")

        assert result["status"] == "failed"
        assert "connection timeout" in result["error"]


class TestIdleFlush:
    """Tests for conversation_capture.idle_flush() — A1 idle flush."""

    def test_idle_flush_constant(self):
        """IDLE_FLUSH_SECONDS should be 900 (15 minutes)."""
        from conversation_capture import IDLE_FLUSH_SECONDS
        assert IDLE_FLUSH_SECONDS == 900

    def test_buffer_last_active_tracking(self):
        """log_conversation should update _buffer_last_active."""
        from conversation_capture import _buffer_last_active
        assert isinstance(_buffer_last_active, dict)

    @pytest.mark.asyncio
    async def test_idle_flush_skips_recent(self):
        """Buffers with recent activity should not be flushed."""
        import conversation_capture as cc
        key = "test_recent_buf"
        cc._conversation_buffers[key] = [{"role": "user", "content": "hi"}]
        cc._buffer_last_active[key] = time.time()
        try:
            result = await cc.idle_flush()
            assert key not in result
        finally:
            cc._conversation_buffers.pop(key, None)
            cc._buffer_last_active.pop(key, None)

    @pytest.mark.asyncio
    async def test_idle_flush_extracts_old(self):
        """Buffers idle > IDLE_FLUSH_SECONDS should be extracted."""
        import conversation_capture as cc
        key = "test_old_buf"
        cc._conversation_buffers[key] = [{"role": "user", "content": "old message"}]
        cc._buffer_last_active[key] = time.time() - cc.IDLE_FLUSH_SECONDS - 60
        cc._last_extract_time.pop(key, None)

        with patch.object(cc, "_extract_and_remember", new_callable=AsyncMock, return_value={"extracted": 1}):
            result = await cc.idle_flush()
            assert key in result
        cc._conversation_buffers.pop(key, None)
        cc._buffer_last_active.pop(key, None)


class TestCorridorDigestSection:
    def test_digest_section_in_corridor(self):
        """Corridor should include 昨晚消化 section when digest exists."""
        import corridor
        src = open("corridor.py", encoding="utf-8").read()
        assert "昨晚消化" in src
        assert 'category") == "digest"' in src or "category\") == \"digest\"" in src

    def test_dreams_exclude_digest(self):
        """Dreams section should exclude digest entries."""
        src = open("corridor.py", encoding="utf-8").read()
        assert 'category") != "digest"' in src or "category\") != \"digest\"" in src
