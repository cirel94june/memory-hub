"""提取恢复测试——验证 raw_events 状态追踪 + 重启后补提取。

覆盖 Codex 验收要求：
1. 不足阈值时重启 → 补提取
2. 提取中重启 → 恢复处理中的行
3. 幂等：已写入记忆但未标完成 → 不重复
4. 空结果也标完成
5. 原始时间保留
6. 存量标记迁移
"""
import os
import sys
import json
import sqlite3
import tempfile
import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest import mock

os.environ.setdefault("ALLOW_DEFAULT_HUB_SECRET", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


def _make_db(tmp_path):
    """Create raw_events DB with extract_status column."""
    db_path = os.path.join(tmp_path, "raw_events.db")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS raw_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ai_id TEXT NOT NULL DEFAULT '',
            platform TEXT NOT NULL DEFAULT '',
            chat_id TEXT NOT NULL DEFAULT '',
            chat_type TEXT NOT NULL DEFAULT '',
            user_text TEXT NOT NULL DEFAULT '',
            ai_text TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            turn_id TEXT NOT NULL DEFAULT '',
            embedding BLOB,
            thread_id TEXT NOT NULL DEFAULT '',
            message_id TEXT NOT NULL DEFAULT '',
            sender_id TEXT NOT NULL DEFAULT '',
            sender_type TEXT NOT NULL DEFAULT '',
            reply_to_id TEXT NOT NULL DEFAULT '',
            extract_status INTEGER NOT NULL DEFAULT 0,
            extract_batch TEXT NOT NULL DEFAULT '',
            extract_retries INTEGER NOT NULL DEFAULT 0,
            last_attempt_at TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_raw_extract ON raw_events(extract_status, created_at DESC)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS _migrations (
            name TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS extract_batches (
            batch_id TEXT PRIMARY KEY,
            row_ids TEXT NOT NULL DEFAULT '[]',
            llm_result TEXT,
            items_written INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'processing'
        )
    """)
    conn.commit()
    conn.close()
    return db_path


def _seed(db_path, rows):
    conn = sqlite3.connect(db_path)
    for r in rows:
        conn.execute(
            "INSERT INTO raw_events "
            "(ai_id, platform, chat_id, chat_type, user_text, ai_text, created_at, "
            "thread_id, extract_status) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                r.get("ai_id", "cloudy"),
                r.get("platform", "telegram"),
                r.get("chat_id", "100"),
                r.get("chat_type", "private"),
                r.get("user_text", "hi"),
                r.get("ai_text", "hello"),
                r.get("created_at", "2026-09-20T00:00:00+00:00"),
                r.get("thread_id", ""),
                r.get("extract_status", 0),
            ),
        )
    conn.commit()
    conn.close()


def _status_counts(db_path) -> dict:
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT extract_status, COUNT(*) FROM raw_events GROUP BY extract_status").fetchall()
    conn.close()
    return {s: c for s, c in rows}


# ── 1. Schema: extract_status column exists ──

def test_extract_status_column_exists():
    import raw_vault
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "raw_events.db")
        with mock.patch.object(raw_vault, "DB_PATH", Path(db_path)):
            raw_vault._init_db()
        conn = sqlite3.connect(db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(raw_events)").fetchall()}
        conn.close()
        assert "extract_status" in cols
        assert "extract_batch" in cols


# ── 2. New rows get extract_status=0 ──

def test_new_rows_default_pending():
    import raw_vault
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "raw_events.db")
        with mock.patch.object(raw_vault, "DB_PATH", Path(db_path)):
            raw_vault._init_db()
            raw_vault.log_turn("hello", "hi", ai_id="cloudy", chat_id="100")
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT extract_status FROM raw_events LIMIT 1").fetchone()
        conn.close()
        assert row[0] == 0


# ── 3. get_unprocessed_chunks groups by ai_id:chat_id:thread_id ──

def test_get_unprocessed_chunks_grouping():
    import raw_vault
    with tempfile.TemporaryDirectory() as tmp:
        db = _make_db(tmp)
        _seed(db, [
            {"ai_id": "cloudy", "chat_id": "100", "thread_id": "",
             "user_text": f"msg{i}",
             "created_at": f"2026-09-20T0{i}:00:00+00:00"}
            for i in range(5)
        ] + [
            {"ai_id": "cloudy", "chat_id": "200", "thread_id": "42",
             "user_text": f"other{i}",
             "created_at": f"2026-09-20T0{i}:00:00+00:00"}
            for i in range(3)
        ])
        with mock.patch.object(raw_vault, "DB_PATH", Path(db)):
            chunks = raw_vault.get_unprocessed_chunks(max_chunks=10, chunk_size=50)
        assert len(chunks) == 2
        keys = {c["key"] for c in chunks}
        assert "cloudy:100:" in keys
        assert "cloudy:200:42" in keys


# ── 3b. Private chats with different bots are isolated ──

def test_private_chats_isolated_by_ai_id():
    """Same chat_id but different ai_id must produce separate chunks."""
    import raw_vault
    with tempfile.TemporaryDirectory() as tmp:
        db = _make_db(tmp)
        _seed(db, [
            {"ai_id": "cloudy", "chat_id": "100", "chat_type": "private",
             "user_text": "hi cloudy"},
            {"ai_id": "lucien", "chat_id": "100", "chat_type": "private",
             "user_text": "hi lucien"},
        ])
        with mock.patch.object(raw_vault, "DB_PATH", Path(db)):
            chunks = raw_vault.get_unprocessed_chunks(max_chunks=10)
        assert len(chunks) == 2
        ai_ids = {c["ai_id"] for c in chunks}
        assert ai_ids == {"cloudy", "lucien"}


# ── 4. mark_rows updates status ──

def test_mark_rows():
    import raw_vault
    with tempfile.TemporaryDirectory() as tmp:
        db = _make_db(tmp)
        _seed(db, [{"chat_id": "100", "user_text": "hi"}])
        with mock.patch.object(raw_vault, "DB_PATH", Path(db)):
            chunks = raw_vault.get_unprocessed_chunks()
            assert len(chunks) == 1
            raw_vault.mark_rows(chunks[0]["row_ids"], raw_vault.EXTRACT_PROCESSING, "batch-1")
        counts = _status_counts(db)
        assert counts.get(1) == 1  # processing

        with mock.patch.object(raw_vault, "DB_PATH", Path(db)):
            raw_vault.mark_rows(chunks[0]["row_ids"], raw_vault.EXTRACT_DONE, "batch-1")
        counts = _status_counts(db)
        assert counts.get(2) == 1  # done


# ── 5. Rows marked done are not returned by get_unprocessed_chunks ──

def test_done_rows_excluded():
    import raw_vault
    with tempfile.TemporaryDirectory() as tmp:
        db = _make_db(tmp)
        _seed(db, [
            {"chat_id": "100", "user_text": "done", "extract_status": 2},
            {"chat_id": "100", "user_text": "pending", "extract_status": 0},
        ])
        with mock.patch.object(raw_vault, "DB_PATH", Path(db)):
            chunks = raw_vault.get_unprocessed_chunks()
        assert len(chunks) == 1
        assert len(chunks[0]["row_ids"]) == 1  # only the pending one


# ── 6. Legacy rows (-1) are excluded ──

def test_legacy_rows_excluded():
    import raw_vault
    with tempfile.TemporaryDirectory() as tmp:
        db = _make_db(tmp)
        _seed(db, [
            {"chat_id": "100", "user_text": "old", "extract_status": -1},
        ])
        with mock.patch.object(raw_vault, "DB_PATH", Path(db)):
            chunks = raw_vault.get_unprocessed_chunks()
        assert len(chunks) == 0


# ── 7. Processing (interrupted) rows are included ──

def test_interrupted_processing_rows_included():
    """Status=1 rows with stale last_attempt_at are picked up by recovery."""
    import raw_vault
    with tempfile.TemporaryDirectory() as tmp:
        db = _make_db(tmp)
        _seed(db, [
            {"chat_id": "100", "user_text": "interrupted", "extract_status": 1},
        ])
        # Set stale last_attempt_at (>5min ago)
        stale = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(timespec="seconds")
        conn = sqlite3.connect(db)
        conn.execute("UPDATE raw_events SET last_attempt_at = ?", (stale,))
        conn.commit()
        conn.close()

        with mock.patch.object(raw_vault, "DB_PATH", Path(db)):
            chunks = raw_vault.get_unprocessed_chunks()
        assert len(chunks) == 1


def test_recent_processing_rows_excluded():
    """Status=1 rows with recent last_attempt_at (active claim) are NOT picked up."""
    import raw_vault
    with tempfile.TemporaryDirectory() as tmp:
        db = _make_db(tmp)
        _seed(db, [
            {"chat_id": "100", "user_text": "being processed now", "extract_status": 1},
        ])
        # last_attempt_at is recent (1 min ago, within 5min claim window)
        recent = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(timespec="seconds")
        conn = sqlite3.connect(db)
        conn.execute("UPDATE raw_events SET last_attempt_at = ?", (recent,))
        conn.commit()
        conn.close()

        with mock.patch.object(raw_vault, "DB_PATH", Path(db)):
            chunks = raw_vault.get_unprocessed_chunks()
        assert len(chunks) == 0, "actively processing rows should not be picked up"


# ── 8. Migration marks old rows as legacy ──

def test_migration_marks_old_rows_legacy():
    import raw_vault
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "raw_events.db")
        # Pre-create DB with old rows (no extract_status column)
        conn = sqlite3.connect(db_path)
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
        # Insert old row (30 days ago)
        old_ts = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(timespec="seconds")
        conn.execute("INSERT INTO raw_events (ai_id, chat_id, user_text, ai_text, created_at) VALUES (?,?,?,?,?)",
                     ("cloudy", "100", "old msg", "old reply", old_ts))
        # Insert recent row (1 day ago)
        recent_ts = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
        conn.execute("INSERT INTO raw_events (ai_id, chat_id, user_text, ai_text, created_at) VALUES (?,?,?,?,?)",
                     ("cloudy", "100", "recent msg", "recent reply", recent_ts))
        conn.commit()
        conn.close()

        with mock.patch.object(raw_vault, "DB_PATH", Path(db_path)):
            raw_vault._init_db()

        conn = sqlite3.connect(db_path)
        rows = conn.execute("SELECT user_text, extract_status FROM raw_events ORDER BY created_at").fetchall()
        conn.close()
        assert rows[0] == ("old msg", -1)  # legacy
        assert rows[1] == ("recent msg", 0)  # pending


# ── 9. count_by_status returns correct counts ──

def test_count_by_status():
    import raw_vault
    with tempfile.TemporaryDirectory() as tmp:
        db = _make_db(tmp)
        _seed(db, [
            {"extract_status": 0}, {"extract_status": 0},
            {"extract_status": 2},
            {"extract_status": -1}, {"extract_status": -1}, {"extract_status": -1},
        ])
        with mock.patch.object(raw_vault, "DB_PATH", Path(db)):
            counts = raw_vault.count_by_status()
        assert counts["pending"] == 2
        assert counts["done"] == 1
        assert counts["legacy"] == 3


# ── 10. mark_rows only affects specified row IDs ──

def test_mark_rows_only_affects_specified_ids():
    """mark_rows must only change the status of the given row IDs."""
    import raw_vault
    with tempfile.TemporaryDirectory() as tmp:
        db = _make_db(tmp)
        _seed(db, [
            {"ai_id": "cloudy", "chat_id": "100", "extract_status": 0,
             "created_at": "2026-09-20T01:00:00+00:00"},
            {"ai_id": "lucien", "chat_id": "100", "extract_status": 0,
             "created_at": "2026-09-20T01:00:00+00:00"},
            {"ai_id": "cloudy", "chat_id": "200", "extract_status": 0,
             "created_at": "2026-09-20T01:00:00+00:00"},
        ])
        conn = sqlite3.connect(db)
        all_ids = [r[0] for r in conn.execute("SELECT id FROM raw_events ORDER BY id").fetchall()]
        conn.close()

        # Mark only the first row as done
        with mock.patch.object(raw_vault, "DB_PATH", Path(db)):
            raw_vault.mark_rows([all_ids[0]], raw_vault.EXTRACT_DONE)

        conn = sqlite3.connect(db)
        rows = conn.execute(
            "SELECT id, extract_status FROM raw_events ORDER BY id"
        ).fetchall()
        conn.close()
        assert rows[0][1] == 2   # first row → done
        assert rows[1][1] == 0   # second row → still pending
        assert rows[2][1] == 0   # third row → still pending


# ── 11a. LLM returns valid [] → done (nothing worth keeping) ──

def test_recovery_valid_empty_list_marks_done():
    """LLM returning valid [] means 'nothing worth keeping' → done."""
    import raw_vault
    from conversation_capture import recover_unprocessed

    with tempfile.TemporaryDirectory() as tmp:
        db = _make_db(tmp)
        _seed(db, [
            {"chat_id": "100", "user_text": "boring", "ai_text": "ok",
             "extract_status": 0, "created_at": "2026-09-20T01:00:00+00:00"},
        ])

        async def mock_llm(*args, **kwargs):
            return "[]"

        with mock.patch.object(raw_vault, "DB_PATH", Path(db)), \
             mock.patch("conversation_capture._call_llm", side_effect=mock_llm):
            result = asyncio.run(recover_unprocessed())

        assert result["status"] == "recovered"
        counts = _status_counts(db)
        assert counts.get(2, 0) == 1  # done
        assert counts.get(0, 0) == 0  # no pending left


# ── 11b. LLM returns empty string (API failure) → failed, NOT done ──

def test_recovery_empty_string_marks_failed():
    """_call_llm returning empty string = API failure → must be 'failed' not 'done'."""
    import raw_vault
    from conversation_capture import recover_unprocessed

    with tempfile.TemporaryDirectory() as tmp:
        db = _make_db(tmp)
        _seed(db, [
            {"chat_id": "100", "user_text": "important", "ai_text": "reply",
             "extract_status": 0, "created_at": "2026-09-20T01:00:00+00:00"},
        ])

        async def mock_llm(*args, **kwargs):
            return ""  # API failure returns empty string

        with mock.patch.object(raw_vault, "DB_PATH", Path(db)), \
             mock.patch("conversation_capture._call_llm", side_effect=mock_llm):
            result = asyncio.run(recover_unprocessed())

        counts = _status_counts(db)
        assert counts.get(3, 0) == 1  # failed
        assert counts.get(2, 0) == 0  # NOT done


# ── 12. recover_unprocessed extracts memories ──

def test_recovery_extracts_memories():
    import raw_vault
    from conversation_capture import recover_unprocessed

    llm_response = json.dumps([{
        "content": "用户提到他们喜欢吃臭袜子味的零食这个梗",
        "about": "interaction",
        "importance": 0.7,
        "room": "living_room",
        "provenance": "ai_summary",
    }])

    with tempfile.TemporaryDirectory() as tmp:
        db = _make_db(tmp)
        _seed(db, [
            {"chat_id": "100", "user_text": "臭袜子好好吃",
             "ai_text": "哈哈你是认真的吗",
             "extract_status": 0,
             "created_at": "2026-09-20T01:00:00+00:00"},
        ])

        remembered = []

        async def mock_llm(*args, **kwargs):
            return llm_response

        async def mock_remember(**kwargs):
            remembered.append(kwargs)
            return {"id": "test-mem-1", "status": "created"}

        with mock.patch.object(raw_vault, "DB_PATH", Path(db)), \
             mock.patch("conversation_capture._call_llm", side_effect=mock_llm), \
             mock.patch("memory_ops.remember", side_effect=mock_remember), \
             mock.patch("database.resolve_alias", return_value=""):
            result = asyncio.run(recover_unprocessed())

        assert result["status"] == "recovered"
        assert len(remembered) == 1
        assert "臭袜子" in remembered[0]["source_context"]
        assert remembered[0]["source_platform"].startswith("recovery:")

        counts = _status_counts(db)
        assert counts.get(2, 0) == 1  # done


# ── 13. recover_unprocessed is idempotent (done rows not re-processed) ──

def test_recovery_idempotent_done_rows():
    """Running recovery twice: first run marks done, second finds nothing."""
    import raw_vault
    from conversation_capture import recover_unprocessed

    call_count = 0

    async def mock_llm(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return "[]"

    with tempfile.TemporaryDirectory() as tmp:
        db = _make_db(tmp)
        _seed(db, [
            {"chat_id": "100", "user_text": "test",
             "extract_status": 0,
             "created_at": "2026-09-20T01:00:00+00:00"},
        ])

        with mock.patch.object(raw_vault, "DB_PATH", Path(db)), \
             mock.patch("conversation_capture._call_llm", side_effect=mock_llm):
            asyncio.run(recover_unprocessed())
            result2 = asyncio.run(recover_unprocessed())

        assert call_count == 1
        assert result2["status"] == "nothing_to_recover"


# ── 13b. Interrupted rows are re-extracted (remember() handles dedup) ──

def test_interrupted_rows_re_extracted():
    """Rows stuck at status=1 with stale claim must be re-extracted."""
    import raw_vault
    from conversation_capture import recover_unprocessed

    llm_called = []

    async def mock_llm(*args, **kwargs):
        llm_called.append(True)
        return "[]"

    with tempfile.TemporaryDirectory() as tmp:
        db = _make_db(tmp)
        _seed(db, [
            {"chat_id": "100", "user_text": "interrupted msg",
             "extract_status": 1,
             "created_at": "2026-09-20T01:00:00+00:00"},
        ])
        # last_attempt_at was 10 min ago (past 5min stale claim timeout)
        stale = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(timespec="seconds")
        conn = sqlite3.connect(db)
        conn.execute("UPDATE raw_events SET last_attempt_at = ?", (stale,))
        conn.commit()
        conn.close()

        with mock.patch.object(raw_vault, "DB_PATH", Path(db)), \
             mock.patch("conversation_capture._call_llm", side_effect=mock_llm):
            result = asyncio.run(recover_unprocessed())

        assert len(llm_called) == 1, "interrupted rows must be re-extracted"
        counts = _status_counts(db)
        assert counts.get(2, 0) == 1  # now done


# ── 14. Failed LLM marks rows as failed with retry increment ──

def test_recovery_llm_failure_marks_failed():
    import raw_vault
    from conversation_capture import recover_unprocessed

    async def mock_llm(*args, **kwargs):
        raise RuntimeError("LLM down")

    with tempfile.TemporaryDirectory() as tmp:
        db = _make_db(tmp)
        _seed(db, [
            {"chat_id": "100", "user_text": "test",
             "extract_status": 0,
             "created_at": "2026-09-20T01:00:00+00:00"},
        ])

        with mock.patch.object(raw_vault, "DB_PATH", Path(db)), \
             mock.patch("conversation_capture._call_llm", side_effect=mock_llm):
            result = asyncio.run(recover_unprocessed())

        assert result["status"] == "recovered"
        counts = _status_counts(db)
        assert counts.get(3, 0) == 1  # failed

        # Verify retry count incremented
        conn = sqlite3.connect(db)
        retries = conn.execute("SELECT extract_retries FROM raw_events").fetchone()[0]
        conn.close()
        assert retries == 1


# ── 14b. Failed rows are retried (with backoff) ──

def test_failed_rows_retried_after_backoff():
    """Failed rows with retries < 3 should be picked up after 30min backoff from last_attempt_at."""
    import raw_vault
    with tempfile.TemporaryDirectory() as tmp:
        db = _make_db(tmp)
        _seed(db, [
            {"chat_id": "100", "user_text": "retry me",
             "extract_status": 3, "created_at": "2026-09-19T01:00:00+00:00"},
        ])
        # last_attempt_at was 1 hour ago (past 30min backoff)
        old_attempt = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
        conn = sqlite3.connect(db)
        conn.execute("UPDATE raw_events SET extract_retries = 1, last_attempt_at = ?", (old_attempt,))
        conn.commit()
        conn.close()

        with mock.patch.object(raw_vault, "DB_PATH", Path(db)):
            chunks = raw_vault.get_unprocessed_chunks()
        assert len(chunks) == 1, "failed row should be picked up for retry"


# ── 14c. Failed rows exhausted retries are not picked up ──

def test_exhausted_retries_excluded():
    """Failed rows with retries >= 3 should NOT be picked up."""
    import raw_vault
    with tempfile.TemporaryDirectory() as tmp:
        db = _make_db(tmp)
        old_attempt = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
        _seed(db, [
            {"chat_id": "100", "user_text": "give up",
             "extract_status": 3, "created_at": "2026-09-19T01:00:00+00:00"},
        ])
        conn = sqlite3.connect(db)
        conn.execute("UPDATE raw_events SET extract_retries = 3, last_attempt_at = ?", (old_attempt,))
        conn.commit()
        conn.close()

        with mock.patch.object(raw_vault, "DB_PATH", Path(db)):
            chunks = raw_vault.get_unprocessed_chunks()
        assert len(chunks) == 0


# ── 14d. Backoff uses last_attempt_at, not created_at ──

def test_backoff_uses_last_attempt_at():
    """A row that failed just now should NOT be retried, even if created_at is old."""
    import raw_vault
    with tempfile.TemporaryDirectory() as tmp:
        db = _make_db(tmp)
        _seed(db, [
            {"chat_id": "100", "user_text": "just failed",
             "extract_status": 3, "created_at": "2026-09-15T01:00:00+00:00"},
        ])
        recent_attempt = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn = sqlite3.connect(db)
        conn.execute("UPDATE raw_events SET extract_retries = 1, last_attempt_at = ?", (recent_attempt,))
        conn.commit()
        conn.close()

        with mock.patch.object(raw_vault, "DB_PATH", Path(db)):
            chunks = raw_vault.get_unprocessed_chunks()
        assert len(chunks) == 0, "recently failed row should respect backoff"


# ── 15. chunk_size limits rows per group ──

def test_chunk_size_respected():
    import raw_vault
    with tempfile.TemporaryDirectory() as tmp:
        db = _make_db(tmp)
        _seed(db, [
            {"chat_id": "100", "user_text": f"msg{i}",
             "created_at": f"2026-09-20T{i:02d}:00:00+00:00"}
            for i in range(20)
        ])
        with mock.patch.object(raw_vault, "DB_PATH", Path(db)):
            chunks = raw_vault.get_unprocessed_chunks(max_chunks=1, chunk_size=5)
        assert len(chunks) == 1
        assert len(chunks[0]["row_ids"]) == 5
        assert chunks[0]["remaining_count"] > 0


# ── 16. Overflow rows stay pending (not marked done) ──

def test_overflow_rows_stay_pending():
    """Rows beyond char budget must NOT be marked done — they stay pending."""
    import raw_vault
    from conversation_capture import recover_unprocessed

    with tempfile.TemporaryDirectory() as tmp:
        db = _make_db(tmp)
        # Create rows where first few fit in budget but later ones overflow
        rows = []
        for i in range(20):
            rows.append({
                "chat_id": "100", "user_text": f"message number {i} " + "x" * 300,
                "ai_text": "reply " + "y" * 300,
                "extract_status": 0,
                "created_at": f"2026-09-20T{i:02d}:00:00+00:00",
            })
        _seed(db, rows)

        async def mock_llm(*args, **kwargs):
            return "[]"

        with mock.patch.object(raw_vault, "DB_PATH", Path(db)), \
             mock.patch("conversation_capture._call_llm", side_effect=mock_llm):
            result = asyncio.run(recover_unprocessed())

        counts = _status_counts(db)
        done = counts.get(2, 0)
        pending = counts.get(0, 0)
        assert done > 0, "some rows should be marked done"
        assert pending > 0, "overflow rows should stay pending for next cycle"
        assert done + pending == 20


# ── 17. (removed — old mock-based idempotency test) ──
# Idempotency is now handled by remember()'s built-in vector similarity dedup.
# Tests 13, 13b cover the row-level status tracking that prevents reprocessing.


# ── 18. Recovery preserves original timestamps ──

def test_recovery_preserves_original_timestamps():
    """source_context in recovered memories must contain original timestamps."""
    import raw_vault
    from conversation_capture import recover_unprocessed

    llm_response = json.dumps([{
        "content": "用户在2026年9月15日提到了臭袜子",
        "about": "user",
        "importance": 0.6,
        "room": "living_room",
        "provenance": "ai_summary",
        "event_date": "2026-09-15",
    }])

    original_ts = "2026-09-15T14:30:00+00:00"

    remembered = []

    async def mock_llm(*args, **kwargs):
        return llm_response

    async def mock_remember(**kwargs):
        remembered.append(kwargs)
        return {"id": "test-mem-1", "status": "created"}

    with tempfile.TemporaryDirectory() as tmp:
        db = _make_db(tmp)
        _seed(db, [
            {"chat_id": "100", "user_text": "臭袜子",
             "ai_text": "哈哈",
             "extract_status": 0,
             "created_at": original_ts},
        ])

        with mock.patch.object(raw_vault, "DB_PATH", Path(db)), \
             mock.patch("conversation_capture._call_llm", side_effect=mock_llm), \
             mock.patch("memory_ops.remember", side_effect=mock_remember), \
             mock.patch("database.resolve_alias", return_value=""):
            asyncio.run(recover_unprocessed())

    assert len(remembered) == 1
    assert "2026-09-15" in remembered[0]["source_context"]
    assert remembered[0]["event_date"] == "2026-09-15"


# ── 19. Normal extraction marks rows by specific IDs ──

def test_normal_extraction_marks_by_row_id():
    """_extract_and_remember must mark only the buffer's raw_event_ids as done,
    not do a broad timestamp sweep."""
    import raw_vault
    from conversation_capture import (
        _extract_and_remember,
        _conversation_buffers,
        _buffer_chat_types,
    )

    async def mock_llm(*args, **kwargs):
        return "[]"

    with tempfile.TemporaryDirectory() as tmp:
        db = _make_db(tmp)
        # Seed two pending rows from same chat
        _seed(db, [
            {"chat_id": "100", "user_text": "msg1",
             "extract_status": 0,
             "created_at": "2026-09-20T01:00:00+00:00"},
            {"chat_id": "100", "user_text": "msg2",
             "extract_status": 0,
             "created_at": "2026-09-20T01:01:00+00:00"},
        ])

        conn = sqlite3.connect(db)
        row_ids = [r[0] for r in conn.execute("SELECT id FROM raw_events ORDER BY id").fetchall()]
        conn.close()

        # Build buffer with only first row's raw_event_id
        key = "cloudy:100"
        _conversation_buffers[key] = [
            {"user": "msg1", "ai": "", "timestamp": "2026-09-20T01:00:00",
             "ai_id": "cloudy", "raw_event_id": row_ids[0]},
        ]
        _buffer_chat_types[key] = "private"

        with mock.patch.object(raw_vault, "DB_PATH", Path(db)), \
             mock.patch("conversation_capture._call_llm", side_effect=mock_llm):
            asyncio.run(_extract_and_remember(key))

        counts = _status_counts(db)
        assert counts.get(2, 0) == 1, "only buffer row should be marked done"
        assert counts.get(0, 0) == 1, "other row should stay pending"

        # Clean up
        _conversation_buffers.pop(key, None)
        _buffer_chat_types.pop(key, None)


# ── 20. Normal extraction and recovery don't conflict ──

def test_normal_claim_prevents_recovery():
    """Normal extraction claims rows (status=1, recent last_attempt_at).
    Recovery should NOT pick up those rows (active claim)."""
    import raw_vault
    with tempfile.TemporaryDirectory() as tmp:
        db = _make_db(tmp)
        _seed(db, [
            {"chat_id": "100", "user_text": "being extracted by normal path",
             "extract_status": 1,
             "created_at": "2026-09-20T01:00:00+00:00"},
            {"chat_id": "200", "user_text": "pending for recovery",
             "extract_status": 0,
             "created_at": "2026-09-20T01:00:00+00:00"},
        ])
        # Chat 100: recently claimed by normal extraction
        recent = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(timespec="seconds")
        conn = sqlite3.connect(db)
        conn.execute("UPDATE raw_events SET last_attempt_at = ? WHERE chat_id = '100'", (recent,))
        conn.commit()
        conn.close()

        with mock.patch.object(raw_vault, "DB_PATH", Path(db)):
            chunks = raw_vault.get_unprocessed_chunks()

        # Only chat 200 should be picked up
        assert len(chunks) == 1
        assert chunks[0]["chat_id"] == "200"


def test_recovery_picks_up_stale_and_pending():
    """Recovery processes stale claims AND pending rows from different chats."""
    import raw_vault
    from conversation_capture import recover_unprocessed

    async def mock_llm(*args, **kwargs):
        return "[]"

    with tempfile.TemporaryDirectory() as tmp:
        db = _make_db(tmp)
        _seed(db, [
            {"chat_id": "100", "user_text": "stale claim",
             "extract_status": 1,
             "created_at": "2026-09-20T01:00:00+00:00"},
            {"chat_id": "200", "user_text": "pending",
             "extract_status": 0,
             "created_at": "2026-09-20T01:00:00+00:00"},
        ])
        # Chat 100: stale claim (>5min)
        stale = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(timespec="seconds")
        conn = sqlite3.connect(db)
        conn.execute("UPDATE raw_events SET last_attempt_at = ? WHERE chat_id = '100'", (stale,))
        conn.commit()
        conn.close()

        with mock.patch.object(raw_vault, "DB_PATH", Path(db)), \
             mock.patch("conversation_capture._call_llm", side_effect=mock_llm), \
             mock.patch("memory_ops.remember", return_value={"id": "x", "status": "created"}), \
             mock.patch("database.resolve_alias", return_value=""):
            result = asyncio.run(recover_unprocessed())

        assert result["status"] == "recovered"
        counts = _status_counts(db)
        assert counts.get(2, 0) == 2  # both done


# ── 22. Batch persistence: partial write + crash + resume ──

def test_batch_resume_skips_already_written():
    """If 2 of 3 items were written before crash, resume writes only the 3rd."""
    import raw_vault
    from conversation_capture import recover_unprocessed

    stale = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(timespec="seconds")
    batch_id = "recovery-test123abc"

    remembered = []

    async def mock_llm(*args, **kwargs):
        raise AssertionError("LLM should NOT be called — result is saved")

    async def mock_remember(**kwargs):
        remembered.append(kwargs.get("content", ""))
        return {"id": f"mem-{len(remembered)}", "status": "created"}

    llm_result = json.dumps([
        {"content": "[用户] first memory item here", "about": "user",
         "importance": 0.7, "room": "living_room", "provenance": "ai_summary"},
        {"content": "[用户] second memory item here", "about": "user",
         "importance": 0.7, "room": "living_room", "provenance": "ai_summary"},
        {"content": "[用户] third memory item here", "about": "user",
         "importance": 0.7, "room": "living_room", "provenance": "ai_summary"},
    ])

    with tempfile.TemporaryDirectory() as tmp:
        db = _make_db(tmp)
        _seed(db, [
            {"chat_id": "100", "user_text": "msg1", "ai_text": "reply1",
             "extract_status": 1, "created_at": "2026-09-20T01:00:00+00:00"},
        ])
        conn = sqlite3.connect(db)
        row_id = conn.execute("SELECT id FROM raw_events").fetchone()[0]

        # Set stale claim and saved batch with 2 items already written
        conn.execute("UPDATE raw_events SET last_attempt_at = ?, extract_batch = ?",
                     (stale, batch_id))
        conn.execute(
            "INSERT INTO extract_batches (batch_id, row_ids, llm_result, items_written, created_at, status) "
            "VALUES (?, ?, ?, 2, ?, 'processing')",
            (batch_id, json.dumps([row_id]), llm_result, stale),
        )
        conn.commit()
        conn.close()

        with mock.patch.object(raw_vault, "DB_PATH", Path(db)), \
             mock.patch("conversation_capture._call_llm", side_effect=mock_llm), \
             mock.patch("memory_ops.remember", side_effect=mock_remember), \
             mock.patch("database.resolve_alias", return_value=""):
            result = asyncio.run(recover_unprocessed())

        assert result["status"] == "recovered"
        # Only 1 new item written (3rd), first 2 were skipped
        assert len(remembered) == 1
        assert "third" in remembered[0]
        counts = _status_counts(db)
        assert counts.get(2, 0) == 1  # done


# ── 23. Normal extraction overflow goes back to buffer ──

def test_normal_extraction_overflow_returns_to_buffer():
    """Entries that exceed char_budget must go back to the buffer."""
    import raw_vault
    from conversation_capture import (
        _extract_and_remember,
        _conversation_buffers,
        _buffer_chat_types,
    )

    async def mock_llm(*args, **kwargs):
        return "[]"

    with tempfile.TemporaryDirectory() as tmp:
        db = _make_db(tmp)
        # Create many rows
        rows_data = []
        for i in range(30):
            rows_data.append({
                "chat_id": "100", "user_text": f"message number {i} " + "x" * 200,
                "extract_status": 0,
                "created_at": f"2026-09-20T01:{i:02d}:00+00:00",
            })
        _seed(db, rows_data)

        conn = sqlite3.connect(db)
        row_ids = [r[0] for r in conn.execute("SELECT id FROM raw_events ORDER BY id").fetchall()]
        conn.close()

        key = "cloudy:100"
        buffer_entries = []
        for i, rid in enumerate(row_ids):
            buffer_entries.append({
                "user": f"message number {i} " + "x" * 200,
                "ai": "reply",
                "timestamp": f"2026-09-20T01:{i:02d}:00",
                "ai_id": "cloudy",
                "raw_event_id": rid,
            })
        _conversation_buffers[key] = buffer_entries
        _buffer_chat_types[key] = "private"

        with mock.patch.object(raw_vault, "DB_PATH", Path(db)), \
             mock.patch("conversation_capture._call_llm", side_effect=mock_llm):
            asyncio.run(_extract_and_remember(key))

        counts = _status_counts(db)
        done_count = counts.get(2, 0)
        pending_count = counts.get(0, 0)
        assert done_count > 0, "some rows should be marked done"
        assert done_count < 30, "not all rows should be done (overflow)"
        # Overflow entries should be back in buffer
        remaining = _conversation_buffers.get(key, [])
        assert len(remaining) > 0, "overflow entries should return to buffer"
        assert len(remaining) + done_count == 30

        # Clean up
        _conversation_buffers.pop(key, None)
        _buffer_chat_types.pop(key, None)
