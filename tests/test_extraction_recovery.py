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
            extract_batch TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_raw_extract ON raw_events(extract_status, created_at DESC)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS _migrations (
            name TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL
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


# ── 3. get_unprocessed_chunks groups by chat_id:thread_id ──

def test_get_unprocessed_chunks_grouping():
    import raw_vault
    with tempfile.TemporaryDirectory() as tmp:
        db = _make_db(tmp)
        _seed(db, [
            {"chat_id": "100", "thread_id": "", "user_text": f"msg{i}",
             "created_at": f"2026-09-20T0{i}:00:00+00:00"}
            for i in range(5)
        ] + [
            {"chat_id": "200", "thread_id": "42", "user_text": f"other{i}",
             "created_at": f"2026-09-20T0{i}:00:00+00:00"}
            for i in range(3)
        ])
        with mock.patch.object(raw_vault, "DB_PATH", Path(db)):
            chunks = raw_vault.get_unprocessed_chunks(max_chunks=10, chunk_size=50)
        assert len(chunks) == 2
        keys = {c["key"] for c in chunks}
        assert "100:" in keys
        assert "200:42" in keys


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
    import raw_vault
    with tempfile.TemporaryDirectory() as tmp:
        db = _make_db(tmp)
        _seed(db, [
            {"chat_id": "100", "user_text": "interrupted", "extract_status": 1},
        ])
        with mock.patch.object(raw_vault, "DB_PATH", Path(db)):
            chunks = raw_vault.get_unprocessed_chunks()
        assert len(chunks) == 1


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


# ── 10. _mark_raw_events_done in conversation_capture ──

def test_extract_marks_raw_done():
    """After _extract_and_remember, corresponding raw_events should be marked done."""
    import raw_vault
    from conversation_capture import _mark_raw_events_done
    with tempfile.TemporaryDirectory() as tmp:
        db = _make_db(tmp)
        _seed(db, [
            {"chat_id": "100", "extract_status": 0,
             "created_at": "2026-09-20T01:00:00+00:00"},
            {"chat_id": "100", "extract_status": 0,
             "created_at": "2026-09-20T02:00:00+00:00"},
            {"chat_id": "200", "extract_status": 0,
             "created_at": "2026-09-20T01:00:00+00:00"},
        ])
        with mock.patch.object(raw_vault, "DB_PATH", Path(db)):
            _mark_raw_events_done("100", "2026-09-20T02:00:00+00:00")

        conn = sqlite3.connect(db)
        chat100 = conn.execute(
            "SELECT extract_status FROM raw_events WHERE chat_id = '100'"
        ).fetchall()
        chat200 = conn.execute(
            "SELECT extract_status FROM raw_events WHERE chat_id = '200'"
        ).fetchall()
        conn.close()
        assert all(s[0] == 2 for s in chat100)
        assert all(s[0] == 0 for s in chat200)


# ── 11. recover_unprocessed with empty LLM result marks done ──

def test_recovery_empty_result_marks_done():
    """LLM returning no useful content still marks rows as done (not stuck)."""
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


# ── 13. recover_unprocessed is idempotent (no rows left after first run) ──

def test_recovery_idempotent():
    """Running recovery twice doesn't reprocess already-done rows."""
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


# ── 14. Failed LLM marks rows as failed, not stuck ──

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


# ── 16. Recovery preserves original timestamps (not processing time) ──

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
