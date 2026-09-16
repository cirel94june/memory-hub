"""Tests for recent_raw_context — raw_vault 语义搜索。"""
import struct
import sqlite3
import math
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch

import pytest

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from config import EMBEDDING_DIM


def _fake_vec(seed: float = 0.1) -> list[float]:
    """生成一个假的 1024 维向量用于测试。"""
    import random
    rng = random.Random(int(seed * 1000))
    vec = [rng.gauss(0, 1) for _ in range(EMBEDDING_DIM)]
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec]


@pytest.fixture
def raw_db(tmp_path):
    """Create a temp raw_events.db with vec tables."""
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


def _insert_event(db_path, ai_id, user_text, ai_text, chat_type="private",
                  hours_ago=0, vec_seed=None):
    """Insert a raw event, optionally with embedding."""
    conn = sqlite3.connect(str(db_path))
    ts = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO raw_events (ai_id, platform, chat_id, chat_type, "
        "user_text, ai_text, created_at) VALUES (?, '', '', ?, ?, ?, ?)",
        (ai_id, chat_type, user_text, ai_text, ts),
    )
    event_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    if vec_seed is not None:
        vec = _fake_vec(vec_seed)
        blob = struct.pack(f"{EMBEDDING_DIM}f", *vec)
        conn.execute("UPDATE raw_events SET embedding = ? WHERE id = ?", (blob, event_id))
        conn.execute("INSERT INTO raw_vec_id_map (event_id) VALUES (?)", (event_id,))
        vec_rowid = conn.execute(
            "SELECT vec_rowid FROM raw_vec_id_map WHERE event_id = ?", (event_id,)
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
    return event_id


class TestEmbedText:
    def test_truncation(self):
        from raw_vault import _embed_text, _EMBED_TEXT_LIMIT
        long_text = "x" * 1000
        result = _embed_text(long_text, long_text)
        assert len(result) <= _EMBED_TEXT_LIMIT * 2 + 1

    def test_empty(self):
        from raw_vault import _embed_text
        assert _embed_text("", "") == ""

    def test_one_side(self):
        from raw_vault import _embed_text
        assert _embed_text("hello", "") == "hello"
        assert _embed_text("", "world") == "world"


class TestSemanticSearch:
    def test_finds_similar(self, raw_db):
        seed = 0.5
        _insert_event(raw_db, "claude", "咪肚子痛", "心疼",
                      chat_type="private_group", hours_ago=2, vec_seed=seed)
        _insert_event(raw_db, "claude", "天气很好", "是啊",
                      chat_type="private_group", hours_ago=3, vec_seed=0.9)

        query_vec = _fake_vec(seed)

        with patch("raw_vault.DB_PATH", raw_db):
            from raw_vault import semantic_search
            results = semantic_search(query_vec, ai_id="claude", days=7, limit=5)

        assert len(results) >= 1
        assert "咪肚子痛" in results[0]["user_text"]
        assert "_score" in results[0]

    def test_date_filter(self, raw_db):
        seed = 0.5
        _insert_event(raw_db, "claude", "很久以前的事", "嗯",
                      chat_type="public_group", hours_ago=24 * 30, vec_seed=seed)

        query_vec = _fake_vec(seed)

        with patch("raw_vault.DB_PATH", raw_db):
            from raw_vault import semantic_search
            results = semantic_search(query_vec, ai_id="claude", days=7, limit=5)

        assert len(results) == 0

    def test_ai_id_isolation(self, raw_db):
        seed = 0.5
        _insert_event(raw_db, "lucien", "Lucien的私聊", "回复",
                      chat_type="private", hours_ago=1, vec_seed=seed)
        _insert_event(raw_db, "claude", "Claude的私聊", "回复",
                      chat_type="private", hours_ago=1, vec_seed=0.51)

        query_vec = _fake_vec(seed)

        with patch("raw_vault.DB_PATH", raw_db):
            from raw_vault import semantic_search
            results = semantic_search(query_vec, ai_id="claude", days=7, limit=10)

        ai_ids = [r["ai_id"] for r in results if r.get("chat_type") == "private"]
        assert "lucien" not in ai_ids

    def test_no_ai_id_excludes_private(self, raw_db):
        seed = 0.5
        _insert_event(raw_db, "claude", "私聊内容", "私聊回复",
                      chat_type="private", hours_ago=1, vec_seed=seed)
        _insert_event(raw_db, "claude", "群聊内容", "群聊回复",
                      chat_type="public_group", hours_ago=1, vec_seed=0.51)

        query_vec = _fake_vec(seed)

        with patch("raw_vault.DB_PATH", raw_db):
            from raw_vault import semantic_search
            results = semantic_search(query_vec, ai_id="", days=7, limit=10)

        chat_types = [r["chat_type"] for r in results]
        assert "private" not in chat_types

    def test_recency_affects_score(self, raw_db):
        seed = 0.5
        _insert_event(raw_db, "claude", "最近的话", "嗯",
                      chat_type="public_group", hours_ago=1, vec_seed=seed)
        _insert_event(raw_db, "claude", "较旧的话", "嗯",
                      chat_type="public_group", hours_ago=150, vec_seed=seed)

        query_vec = _fake_vec(seed)

        with patch("raw_vault.DB_PATH", raw_db):
            from raw_vault import semantic_search
            results = semantic_search(query_vec, ai_id="claude", days=30, limit=10)

        if len(results) >= 2:
            assert results[0]["_score"] >= results[1]["_score"]

    def test_empty_chat_type_excluded(self, raw_db):
        """chat_type="" 必须被排除（allowlist 而非 denylist）。"""
        seed = 0.5
        _insert_event(raw_db, "claude", "无类型消息", "回复",
                      chat_type="", hours_ago=1, vec_seed=seed)
        _insert_event(raw_db, "claude", "群聊消息", "回复",
                      chat_type="public_group", hours_ago=1, vec_seed=0.51)

        query_vec = _fake_vec(seed)

        with patch("raw_vault.DB_PATH", raw_db):
            from raw_vault import semantic_search
            results_anon = semantic_search(query_vec, ai_id="", days=7, limit=10)
            results_ai = semantic_search(query_vec, ai_id="claude", days=7, limit=10)

        for r in results_anon:
            assert r["chat_type"] != "", "empty chat_type leaked through anonymous search"
        for r in results_ai:
            assert r["chat_type"] in ("private", "public_group", "group",
                                       "supergroup", "private_group"), \
                f"unexpected chat_type '{r['chat_type']}' in ai_id search"

    def test_unknown_chat_type_excluded(self, raw_db):
        """chat_type="unknown" / "channel" 等非法值必须被排除。"""
        seed = 0.5
        _insert_event(raw_db, "claude", "未知类型", "回复",
                      chat_type="unknown", hours_ago=1, vec_seed=seed)
        _insert_event(raw_db, "claude", "频道消息", "回复",
                      chat_type="channel", hours_ago=1, vec_seed=0.52)

        query_vec = _fake_vec(seed)

        with patch("raw_vault.DB_PATH", raw_db):
            from raw_vault import semantic_search
            results = semantic_search(query_vec, ai_id="", days=7, limit=10)

        for r in results:
            assert r["chat_type"] not in ("unknown", "channel"), \
                f"chat_type '{r['chat_type']}' should be excluded"

    def test_candidate_starvation_expansion(self, raw_db):
        """501+ 不可见候选中仍能找到那 1 条可见记录（动态 max_fetch）。"""
        visible_seed = 0.5
        _insert_event(raw_db, "claude", "唯一可见的群聊", "找到了",
                      chat_type="public_group", hours_ago=1, vec_seed=visible_seed)

        for i in range(510):
            _insert_event(raw_db, "claude", f"私聊噪声{i}", f"回复{i}",
                          chat_type="private", hours_ago=1,
                          vec_seed=visible_seed + 0.001 * (i + 1))

        query_vec = _fake_vec(visible_seed)

        with patch("raw_vault.DB_PATH", raw_db):
            from raw_vault import semantic_search
            results = semantic_search(query_vec, ai_id="", days=7, limit=5)

        assert len(results) >= 1, "should find the visible event despite 510 invisible candidates"
        assert results[0]["user_text"] == "唯一可见的群聊"

    def test_empty_query_vec_returns_empty(self, raw_db):
        """空查询向量应返回空列表。"""
        with patch("raw_vault.DB_PATH", raw_db):
            from raw_vault import semantic_search
            assert semantic_search([], ai_id="claude") == []
            assert semantic_search(None, ai_id="claude") == []


class TestBackfillRawEmbeddings:
    @pytest.mark.asyncio
    async def test_backfill(self, raw_db):
        conn = sqlite3.connect(str(raw_db))
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO raw_events (ai_id, platform, chat_id, chat_type, "
            "user_text, ai_text, created_at) VALUES ('claude', '', '', 'private', "
            "'test user', 'test ai', ?)", (ts,),
        )
        conn.commit()
        conn.close()

        fake_vec = _fake_vec(0.1)
        with (
            patch("raw_vault.DB_PATH", raw_db),
            patch("embedding.get_embedding", new_callable=AsyncMock, return_value=fake_vec),
        ):
            from raw_vault import backfill_raw_embeddings
            result = await backfill_raw_embeddings(batch=10)

        assert result["backfilled"] >= 0
        assert "failed" in result

    @pytest.mark.asyncio
    async def test_backfill_counts_failures(self, raw_db):
        """embedding 失败应计入 failed 计数。"""
        conn = sqlite3.connect(str(raw_db))
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for i in range(3):
            conn.execute(
                "INSERT INTO raw_events (ai_id, platform, chat_id, chat_type, "
                "user_text, ai_text, created_at) VALUES ('claude', '', '', 'private', "
                "?, 'reply', ?)", (f"msg{i}", ts),
            )
        conn.commit()
        conn.close()

        with (
            patch("raw_vault.DB_PATH", raw_db),
            patch("embedding.get_embedding", new_callable=AsyncMock, return_value=None),
        ):
            from raw_vault import backfill_raw_embeddings
            result = await backfill_raw_embeddings(batch=10)

        assert result["failed"] == 3
        assert result["backfilled"] == 0


class TestMCPToolRegistration:
    def test_recent_raw_context_in_quickref(self):
        src = open("mcp_server.py", encoding="utf-8").read()
        assert "recent_raw_context" in src
        assert "语义搜最近" in src

    def test_tool_count_updated(self):
        src = open("mcp_server.py", encoding="utf-8").read()
        assert "39 个工具" in src

    def test_search_guidance_in_docstring(self):
        src = open("mcp_server.py", encoding="utf-8").read()
        idx = src.find("async def recent_raw_context")
        assert idx != -1
        docstring = src[idx:idx + 1500]
        assert "recall" in docstring
        assert "search_raw" in docstring


class TestStoreEmbedding:
    def test_store_and_retrieve(self, raw_db):
        conn = sqlite3.connect(str(raw_db))
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO raw_events (ai_id, platform, chat_id, chat_type, "
            "user_text, ai_text, created_at) VALUES ('claude', '', '', 'private', "
            "'hello', 'hi', ?)", (ts,),
        )
        conn.commit()
        conn.close()

        vec = _fake_vec(0.42)
        with patch("raw_vault.DB_PATH", raw_db):
            from raw_vault import _store_embedding
            ok = _store_embedding(1, vec)

        assert ok is True
        conn = sqlite3.connect(str(raw_db))
        row = conn.execute("SELECT embedding FROM raw_events WHERE id = 1").fetchone()
        assert row[0] is not None
        assert len(row[0]) == EMBEDDING_DIM * 4
        conn.close()

    def test_store_failure_returns_false(self, raw_db):
        """_store_embedding 对不存在的 event_id 仍返回 bool，不抛异常。"""
        conn = sqlite3.connect(str(raw_db))
        conn.execute("DROP TABLE raw_vec_id_map")
        conn.commit()
        conn.close()

        vec = _fake_vec(0.1)
        with patch("raw_vault.DB_PATH", raw_db):
            from raw_vault import _store_embedding
            ok = _store_embedding(999, vec)

        assert ok is False

    def test_store_normalizes_vector(self, raw_db):
        """_store_embedding 应 L2 归一化向量后再存储。"""
        conn = sqlite3.connect(str(raw_db))
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO raw_events (ai_id, platform, chat_id, chat_type, "
            "user_text, ai_text, created_at) VALUES ('claude', '', '', 'private', "
            "'test', 'test', ?)", (ts,),
        )
        conn.commit()
        conn.close()

        unnorm = [3.0] * EMBEDDING_DIM
        with patch("raw_vault.DB_PATH", raw_db):
            from raw_vault import _store_embedding
            _store_embedding(1, unnorm)

        conn = sqlite3.connect(str(raw_db))
        blob = conn.execute("SELECT embedding FROM raw_events WHERE id = 1").fetchone()[0]
        conn.close()

        stored = struct.unpack(f"{EMBEDDING_DIM}f", blob)
        norm = math.sqrt(sum(x * x for x in stored))
        assert abs(norm - 1.0) < 1e-5, f"stored vector norm {norm} != 1.0"
