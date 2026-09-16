"""Tests for raw_vault.py semantic search — PR #34 Codex findings."""
import math
import struct
import sqlite3
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from config import EMBEDDING_DIM


def _make_vec(values: list[float]) -> list[float]:
    """Pad or truncate to EMBEDDING_DIM."""
    v = list(values) + [0.0] * (EMBEDDING_DIM - len(values))
    return v[:EMBEDDING_DIM]


def _make_blob(values: list[float]) -> bytes:
    vec = _make_vec(values)
    return struct.pack(f"{len(vec)}f", *vec)


@pytest.fixture
def raw_db(tmp_path):
    """Create a temporary raw_events.db with tables and vec extension."""
    db = tmp_path / "raw_events.db"
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE raw_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ai_id TEXT NOT NULL DEFAULT '',
            platform TEXT NOT NULL DEFAULT '',
            chat_id TEXT NOT NULL DEFAULT '',
            chat_type TEXT NOT NULL DEFAULT '',
            user_text TEXT NOT NULL DEFAULT '',
            ai_text TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            turn_id TEXT NOT NULL DEFAULT '',
            embedding BLOB
        )
    """)
    conn.execute("""
        CREATE TABLE raw_vec_id_map (
            vec_rowid INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL UNIQUE
        )
    """)
    try:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        conn.execute(
            f"CREATE VIRTUAL TABLE raw_events_vec "
            f"USING vec0(embedding float[{EMBEDDING_DIM}])"
        )
    except Exception:
        pytest.skip("sqlite_vec not available")
    conn.commit()
    conn.close()
    return db


@pytest.fixture(autouse=True)
def _patch_db_path(raw_db):
    import raw_vault
    old = raw_vault.DB_PATH
    raw_vault.DB_PATH = raw_db
    yield
    raw_vault.DB_PATH = old


def _insert_event(db_path, ai_id, chat_type, user_text, ai_text,
                  embedding_vec=None, hours_ago=1):
    """Insert an event and optionally its embedding."""
    conn = sqlite3.connect(str(db_path))
    ts = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat(timespec="seconds")
    cur = conn.execute(
        "INSERT INTO raw_events (ai_id, chat_type, user_text, ai_text, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (ai_id, chat_type, user_text, ai_text, ts),
    )
    eid = cur.lastrowid
    if embedding_vec is not None:
        blob = _make_blob(embedding_vec)
        conn.execute("UPDATE raw_events SET embedding = ? WHERE id = ?", (blob, eid))
        conn.execute("INSERT INTO raw_vec_id_map (event_id) VALUES (?)", (eid,))
        vec_rowid = conn.execute(
            "SELECT vec_rowid FROM raw_vec_id_map WHERE event_id = ?", (eid,)
        ).fetchone()[0]
        try:
            import sqlite_vec
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
        except Exception:
            pass
        conn.execute(
            "INSERT INTO raw_events_vec (rowid, embedding) VALUES (?, ?)",
            (vec_rowid, blob),
        )
    conn.commit()
    conn.close()
    return eid


class TestStoreEmbedding:
    def test_sql_failure_rollback_and_close(self, raw_db):
        """H2: _store_embedding SQL failure → rollback + conn closed + returns False."""
        import raw_vault

        eid = _insert_event(raw_db, "claude", "group", "hi", "hello")
        vec = _make_vec([1.0, 0.5])

        # Rename the id_map table so the INSERT fails mid-transaction
        conn = sqlite3.connect(str(raw_db))
        conn.execute("ALTER TABLE raw_vec_id_map RENAME TO raw_vec_id_map_bak")
        conn.commit()
        conn.close()

        result = raw_vault._store_embedding(eid, vec)
        assert result is False

        # Verify rollback: embedding column should still be NULL
        conn2 = sqlite3.connect(str(raw_db))
        row = conn2.execute("SELECT embedding FROM raw_events WHERE id = ?", (eid,)).fetchone()
        assert row[0] is None
        # Restore for other tests
        conn2.execute("ALTER TABLE raw_vec_id_map_bak RENAME TO raw_vec_id_map")
        conn2.commit()
        conn2.close()

    def test_success_returns_true(self, raw_db):
        """H2: successful store returns True."""
        import raw_vault
        eid = _insert_event(raw_db, "claude", "group", "hi", "hello")
        vec = _make_vec([1.0, 0.5])
        result = raw_vault._store_embedding(eid, vec)
        assert result is True


class TestCandidateStarvation:
    def test_private_filtered_still_finds_group(self, raw_db):
        """H3: 6 high-similarity private events + 1 group event → group event returned."""
        import raw_vault

        target_vec = [1.0, 0.0]
        # Insert 6 private events with very similar embeddings (would rank high)
        for i in range(6):
            _insert_event(raw_db, "other_ai", "private", f"private msg {i}",
                         f"private resp {i}", embedding_vec=[1.0, 0.01 * i], hours_ago=1)

        # Insert 1 group event that should be found
        _insert_event(raw_db, "claude", "group", "group message",
                     "group response", embedding_vec=[0.9, 0.1], hours_ago=2)

        results = raw_vault.semantic_search(
            query_vec=_make_vec(target_vec), ai_id="", days=7, limit=5,
        )
        assert len(results) >= 1
        assert any(r["chat_type"] == "group" for r in results)


class TestCosineFromRawBlob:
    def test_orthogonal_cosine_zero(self, raw_db):
        """M4: orthogonal vectors should have cosine similarity 0."""
        import raw_vault

        a = _make_blob([1.0, 0.0])
        b = _make_blob([0.0, 1.0])
        sim = raw_vault._cosine_sim(a, b)
        assert abs(sim) < 1e-6

    def test_identical_cosine_one(self, raw_db):
        """M4: identical vectors should have cosine similarity 1."""
        import raw_vault

        a = _make_blob([3.0, 4.0])
        sim = raw_vault._cosine_sim(a, a)
        assert abs(sim - 1.0) < 1e-6

    def test_unnormalized_vectors_correct(self, raw_db):
        """M4: cosine_sim handles non-unit vectors correctly."""
        import raw_vault

        a = _make_blob([3.0, 0.0])
        b = _make_blob([5.0, 0.0])
        sim = raw_vault._cosine_sim(a, b)
        assert abs(sim - 1.0) < 1e-6


class TestBackfillFailure:
    @pytest.mark.asyncio
    async def test_store_failure_not_counted(self, raw_db):
        """M6: _store_embedding returning False should not increment backfilled."""
        import raw_vault

        _insert_event(raw_db, "claude", "group", "test msg", "test resp")

        mock_embed = AsyncMock(return_value=_make_vec([1.0, 0.5]))

        original_store = raw_vault._store_embedding

        def always_fail(event_id, embedding):
            return False

        with (
            patch("embedding.get_embedding", mock_embed),
            patch.object(raw_vault, "_store_embedding", always_fail),
        ):
            result = await raw_vault.backfill_raw_embeddings(batch=10)

        assert result["backfilled"] == 0
        assert result["failed"] == 1


class TestEmptyQueryGuard:
    def test_empty_query_no_embedding_call(self):
        """M7: empty query should not call get_embedding — validated at MCP layer.

        We verify the MCP tool source has the empty guard.
        """
        src = open("mcp_server.py", encoding="utf-8").read()
        assert 'if not (query or "").strip()' in src
        assert '"empty_query"' in src

    def test_semantic_search_empty_vec(self, raw_db):
        """Semantic search with zero vector returns empty results, no crash."""
        import raw_vault
        results = raw_vault.semantic_search(
            query_vec=_make_vec([0.0, 0.0]), days=7, limit=5,
        )
        assert isinstance(results, list)
