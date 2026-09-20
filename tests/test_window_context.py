"""窗口续聊接口测试——验证 raw_vault.get_window_context 隔离、预算、去重。"""
import os
import sys
import sqlite3
import tempfile
from pathlib import Path
from unittest import mock

os.environ.setdefault("ALLOW_DEFAULT_HUB_SECRET", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _make_vault(tmp_path):
    """在临时目录创建 raw_events 库并 patch DB_PATH。"""
    db_path = os.path.join(tmp_path, "raw_events.db")
    return db_path


def _seed(db_path, rows):
    """往临时库插入测试行。"""
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
            reply_to_id TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_raw_window ON raw_events(chat_id, thread_id, created_at DESC)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_raw_dedup ON raw_events(ai_id, chat_id, message_id) WHERE message_id != ''")
    for r in rows:
        conn.execute(
            "INSERT INTO raw_events "
            "(ai_id, platform, chat_id, chat_type, user_text, ai_text, created_at, "
            "turn_id, thread_id, message_id, sender_id, sender_type, reply_to_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                r.get("ai_id", "cloudy"),
                r.get("platform", "telegram"),
                r.get("chat_id", "100"),
                r.get("chat_type", "private"),
                r.get("user_text", "hi"),
                r.get("ai_text", "hello"),
                r.get("created_at", "2026-09-20T00:00:00+00:00"),
                r.get("turn_id", ""),
                r.get("thread_id", ""),
                r.get("message_id", ""),
                r.get("sender_id", ""),
                r.get("sender_type", ""),
                r.get("reply_to_id", ""),
            ),
        )
    conn.commit()
    conn.close()


def _call(db_path, **kwargs):
    """Patch DB_PATH 然后调 get_window_context。"""
    import raw_vault
    with mock.patch.object(raw_vault, "DB_PATH", Path(db_path)):
        return raw_vault.get_window_context(**kwargs)


# ── 1. chat_id 必传 ──

def test_missing_chat_id_returns_error():
    import raw_vault
    result = raw_vault.get_window_context(ai_id="cloudy", chat_id="")
    assert result["error"] == "chat_id_required"
    assert result["turns"] == []


# ── 2. 基本窗口读取 ──

def test_basic_window_read():
    with tempfile.TemporaryDirectory() as tmp:
        db = _make_vault(tmp)
        _seed(db, [
            {"chat_id": "100", "user_text": "你好", "ai_text": "你好呀",
             "created_at": f"2026-09-20T0{i}:00:00+00:00"}
            for i in range(5)
        ])
        result = _call(db, ai_id="cloudy", chat_id="100")
        assert result["count"] == 5
        assert result["truncated"] is False
        assert result["turns"][0]["user_text"] == "你好"
        assert result["turns"][0]["created_at"] < result["turns"][-1]["created_at"]


# ── 3. 窗口隔离 ──

def test_different_chats_isolated():
    with tempfile.TemporaryDirectory() as tmp:
        db = _make_vault(tmp)
        _seed(db, [
            {"chat_id": "100", "user_text": "chat100"},
            {"chat_id": "200", "user_text": "chat200"},
        ])
        r1 = _call(db, ai_id="cloudy", chat_id="100")
        r2 = _call(db, ai_id="cloudy", chat_id="200")
        assert r1["count"] == 1
        assert r2["count"] == 1
        assert r1["turns"][0]["user_text"] == "chat100"
        assert r2["turns"][0]["user_text"] == "chat200"


# ── 4. thread_id 隔离 ──

def test_thread_isolation():
    with tempfile.TemporaryDirectory() as tmp:
        db = _make_vault(tmp)
        _seed(db, [
            {"chat_id": "100", "thread_id": "", "user_text": "no-topic"},
            {"chat_id": "100", "thread_id": "42", "user_text": "topic-42"},
        ])
        r_no = _call(db, ai_id="cloudy", chat_id="100", thread_id="")
        r_42 = _call(db, ai_id="cloudy", chat_id="100", thread_id="42")
        assert r_no["count"] == 1
        assert r_no["turns"][0]["user_text"] == "no-topic"
        assert r_42["count"] == 1
        assert r_42["turns"][0]["user_text"] == "topic-42"


# ── 5. 私聊按 ai_id 隔离 ──

def test_private_chat_ai_isolation():
    with tempfile.TemporaryDirectory() as tmp:
        db = _make_vault(tmp)
        _seed(db, [
            {"chat_id": "100", "chat_type": "private", "ai_id": "cloudy", "user_text": "for-cloudy"},
            {"chat_id": "100", "chat_type": "private", "ai_id": "lucien", "user_text": "for-lucien"},
        ])
        r_c = _call(db, ai_id="cloudy", chat_id="100")
        r_l = _call(db, ai_id="lucien", chat_id="100")
        assert r_c["count"] == 1
        assert r_c["turns"][0]["user_text"] == "for-cloudy"
        assert r_l["count"] == 1
        assert r_l["turns"][0]["user_text"] == "for-lucien"


# ── 6. 群聊不按 ai_id 过滤 ──

def test_group_chat_no_ai_filter():
    with tempfile.TemporaryDirectory() as tmp:
        db = _make_vault(tmp)
        _seed(db, [
            {"chat_id": "-999", "chat_type": "private_group", "ai_id": "cloudy", "user_text": "msg1"},
            {"chat_id": "-999", "chat_type": "private_group", "ai_id": "lucien", "user_text": "msg2"},
        ])
        result = _call(db, ai_id="cloudy", chat_id="-999")
        assert result["count"] == 2


# ── 7. 字符预算（优先保留最新） ──

def test_char_budget_keeps_newest():
    with tempfile.TemporaryDirectory() as tmp:
        db = _make_vault(tmp)
        _seed(db, [
            {"chat_id": "100", "user_text": f"msg{i}", "ai_text": "a" * 500,
             "created_at": f"2026-09-20T0{i}:00:00+00:00"}
            for i in range(8)
        ])
        result = _call(db, ai_id="cloudy", chat_id="100", max_chars=2000)
        assert result["truncated"] is True
        assert result["count"] < 8
        total = sum(len(t["user_text"]) + len(t["ai_text"]) for t in result["turns"])
        assert total <= 2000
        assert result["turns"][-1]["user_text"] == "msg7"
        assert result["turns"][0]["created_at"] < result["turns"][-1]["created_at"]


# ── 8. 单条超预算截断 ──

def test_single_oversized_turn():
    with tempfile.TemporaryDirectory() as tmp:
        db = _make_vault(tmp)
        _seed(db, [
            {"chat_id": "100", "user_text": "x" * 5000, "ai_text": "y" * 5000},
        ])
        result = _call(db, ai_id="cloudy", chat_id="100", max_chars=1000)
        assert result["count"] == 1
        assert result["truncated"] is True
        assert result["turns"][0].get("truncated") is True
        assert len(result["turns"][0]["user_text"]) <= 500


# ── 9. 去重 (message_id) ──

def test_dedup_by_message_id():
    with tempfile.TemporaryDirectory() as tmp:
        db = _make_vault(tmp)
        import raw_vault
        with mock.patch.object(raw_vault, "DB_PATH", Path(db)):
            raw_vault._init_db()
            raw_vault.log_turn("hi", "hello", ai_id="cloudy", chat_id="100",
                               message_id="msg_1")
            raw_vault.log_turn("hi", "hello", ai_id="cloudy", chat_id="100",
                               message_id="msg_1")
        conn = sqlite3.connect(db)
        count = conn.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0]
        conn.close()
        assert count == 1


# ── 10. 空 message_id 不去重 ──

def test_empty_message_id_no_dedup():
    with tempfile.TemporaryDirectory() as tmp:
        db = _make_vault(tmp)
        import raw_vault
        with mock.patch.object(raw_vault, "DB_PATH", Path(db)):
            raw_vault._init_db()
            raw_vault.log_turn("a", "b", ai_id="cloudy", chat_id="100", message_id="")
            raw_vault.log_turn("c", "d", ai_id="cloudy", chat_id="100", message_id="")
        conn = sqlite3.connect(db)
        count = conn.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0]
        conn.close()
        assert count == 2


# ── 11. 新字段正确存储 ──

def test_new_fields_stored():
    with tempfile.TemporaryDirectory() as tmp:
        db = _make_vault(tmp)
        import raw_vault
        with mock.patch.object(raw_vault, "DB_PATH", Path(db)):
            raw_vault._init_db()
            raw_vault.log_turn("hi", "yo", ai_id="cloudy", chat_id="100",
                               thread_id="42", message_id="m1",
                               sender_id="user_99", sender_type="user",
                               reply_to_id="m0")
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM raw_events LIMIT 1").fetchone()
        conn.close()
        assert row["thread_id"] == "42"
        assert row["message_id"] == "m1"
        assert row["sender_id"] == "user_99"
        assert row["sender_type"] == "user"
        assert row["reply_to_id"] == "m0"


# ── 12. 旧数据兼容（缺新字段默认空串） ──

def test_old_data_compat():
    """旧数据没有新字段，get_window_context 仍应正常返回。"""
    with tempfile.TemporaryDirectory() as tmp:
        db = _make_vault(tmp)
        _seed(db, [
            {"chat_id": "100", "user_text": "old msg"},
        ])
        result = _call(db, ai_id="cloudy", chat_id="100")
        assert result["count"] == 1
        assert result["turns"][0]["thread_id"] == ""
        assert result["turns"][0]["message_id"] == ""


# ── 13. 返回字段包含身份和消息标识 ──

def test_response_includes_identity_fields():
    with tempfile.TemporaryDirectory() as tmp:
        db = _make_vault(tmp)
        _seed(db, [
            {"chat_id": "100", "sender_id": "u1", "sender_type": "user",
             "message_id": "m5", "reply_to_id": "m4"},
        ])
        result = _call(db, ai_id="cloudy", chat_id="100")
        turn = result["turns"][0]
        assert turn["sender_id"] == "u1"
        assert turn["sender_type"] == "user"
        assert turn["message_id"] == "m5"
        assert turn["reply_to_id"] == "m4"


# ── 14. 时间正序返回 ──

def test_returns_chronological_order():
    with tempfile.TemporaryDirectory() as tmp:
        db = _make_vault(tmp)
        _seed(db, [
            {"chat_id": "100", "user_text": "first",
             "created_at": "2026-09-20T01:00:00+00:00"},
            {"chat_id": "100", "user_text": "second",
             "created_at": "2026-09-20T02:00:00+00:00"},
            {"chat_id": "100", "user_text": "third",
             "created_at": "2026-09-20T03:00:00+00:00"},
        ])
        result = _call(db, ai_id="cloudy", chat_id="100")
        assert result["turns"][0]["user_text"] == "first"
        assert result["turns"][2]["user_text"] == "third"
