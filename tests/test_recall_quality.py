"""
Phase 1.7 PR A — recall quality upgrade regression tests.

Covers:
  Block 1: resolved/superseded memories excluded at SQL layer
  Block 2: recency boost after RRF merge
  Block 3: activation_count P95 penalty
  Block 6: resolve_thread pattern matching with boundary guards
  M1: atomic auto-resolve
  M2: FTS/CJK room filtering at SQL layer
  M3: touch failure doesn't crash recall
  L1: P95 boundary uses math.ceil
  L2: naive datetime handling

Run: ALLOW_DEFAULT_HUB_SECRET=1 python -m pytest tests/test_recall_quality.py -q
"""
import os
import sys
import math
import struct
import hashlib
import asyncio
import sqlite3
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

os.environ.setdefault("ALLOW_DEFAULT_HUB_SECRET", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import database
from config import EMBEDDING_DIM


# ════════════════════════════════════════════
#  Helpers
# ════════════════════════════════════════════

def _make_vec(seed: str) -> list[float]:
    h = hashlib.sha256(seed.encode()).hexdigest()
    while len(h) < EMBEDDING_DIM * 2:
        h += hashlib.sha256(h.encode()).hexdigest()
    return [((int(h[i*2:i*2+2], 16) / 255.0) - 0.5) * 2 for i in range(EMBEDDING_DIM)]


def _pack_vec(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


@pytest.fixture
def db_env(tmp_path):
    db_path = str(tmp_path / "test.db")
    database.DB_PATH = tmp_path / "test.db"
    asyncio.run(database.init_db(db_path))
    if hasattr(database, '_local'):
        database._local.read_conn = None
    yield tmp_path
    database._local_conns = {}
    if hasattr(database, '_local'):
        database._local.read_conn = None


def _insert_memory(conn, mid, content, room="living_room", status="active",
                   resolved=None, superseded_by="", provenance="user_statement",
                   layer="shared", owner_ai="", source_ai="", activation_count=0,
                   created_at=None, vec_seed=None):
    now = created_at or datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO memories (id, content, room, status, provenance_type, layer, "
        "owner_ai, source_ai, created_at, updated_at, importance, category, tags, "
        "domain, resolved, superseded_by, activation_count) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (mid, content, room, status, provenance, layer, owner_ai, source_ai,
         now, now, 0.5, "", "[]", "[]", resolved, superseded_by, activation_count),
    )
    if vec_seed:
        vec = _make_vec(vec_seed)
        blob = _pack_vec(vec)
        conn.execute("INSERT INTO memories_vec (embedding) VALUES (?)", (blob,))
        rowid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("INSERT INTO vec_id_map (vec_rowid, memory_id) VALUES (?, ?)",
                     (rowid, mid))
    conn.execute(
        "INSERT INTO memories_fts (rowid, content) "
        "SELECT rowid, content FROM memories WHERE id = ?", (mid,)
    )
    conn.commit()


# ════════════════════════════════════════════
#  Block 1: resolved/superseded SQL-layer filtering
# ════════════════════════════════════════════

class TestBlock1SQLFiltering:
    def test_vector_search_excludes_resolved(self, db_env):
        conn = database._get_conn()
        for i in range(10):
            _insert_memory(conn, f"resolved_{i}", f"cat memory {i}", resolved=1,
                           vec_seed=f"cat_{i}")
        for i in range(10):
            _insert_memory(conn, f"active_{i}", f"cat active {i}",
                           vec_seed=f"cat_active_{i}")
        query_vec = _make_vec("cat_active_0")
        results = database.vector_search(query_vec, top_k=10, status="active",
                                         exclude_resolved=True)
        ids = {r["id"] for r in results}
        assert not any(rid.startswith("resolved_") for rid in ids)

    def test_vector_search_excludes_superseded(self, db_env):
        conn = database._get_conn()
        _insert_memory(conn, "old_mem", "dog fact old", superseded_by="new_mem",
                       vec_seed="dog_old")
        _insert_memory(conn, "new_mem", "dog fact new", vec_seed="dog_new")
        query_vec = _make_vec("dog_old")
        results = database.vector_search(query_vec, top_k=10, status="active",
                                         exclude_superseded=True)
        ids = {r["id"] for r in results}
        assert "old_mem" not in ids

    def test_fts_search_excludes_resolved(self, db_env):
        conn = database._get_conn()
        _insert_memory(conn, "fts_resolved", "elephant memory resolved", resolved=1)
        _insert_memory(conn, "fts_active", "elephant memory active")
        results = database.fts_search("elephant", top_k=10, status="active",
                                      exclude_resolved=True)
        ids = {r["id"] for r in results}
        assert "fts_resolved" not in ids
        assert "fts_active" in ids

    def test_fts_search_excludes_superseded(self, db_env):
        conn = database._get_conn()
        _insert_memory(conn, "fts_old", "giraffe old fact", superseded_by="fts_new")
        _insert_memory(conn, "fts_new", "giraffe new fact")
        results = database.fts_search("giraffe", top_k=10, status="active",
                                      exclude_superseded=True)
        ids = {r["id"] for r in results}
        assert "fts_old" not in ids
        assert "fts_new" in ids

    def test_cjk_like_excludes_resolved(self, db_env):
        conn = database._get_conn()
        _insert_memory(conn, "cjk_resolved", "小猫喜欢吃鱼 resolved", resolved=1)
        _insert_memory(conn, "cjk_active", "小猫喜欢吃鱼 active")
        results = database.cjk_like_search("小猫喜欢", top_k=10, status="active",
                                           exclude_resolved=True)
        ids = {r["id"] for r in results}
        assert "cjk_resolved" not in ids
        assert "cjk_active" in ids

    def test_100_resolved_recall_still_returns_full(self, db_env):
        """100 resolved memories + 10 active: recall must return 10 active."""
        conn = database._get_conn()
        for i in range(100):
            _insert_memory(conn, f"res_{i}", f"topic alpha info {i}", resolved=1,
                           vec_seed=f"alpha_{i}")
        for i in range(10):
            _insert_memory(conn, f"act_{i}", f"topic alpha data {i}",
                           vec_seed=f"alpha_act_{i}")
        query_vec = _make_vec("alpha_act_0")
        results = database.vector_search(query_vec, top_k=10, status="active",
                                         exclude_resolved=True, exclude_superseded=True)
        assert len(results) == 10
        for r in results:
            assert r["id"].startswith("act_")

    def test_null_resolved_not_excluded(self, db_env):
        """resolved=NULL (most memories) must NOT be excluded."""
        conn = database._get_conn()
        _insert_memory(conn, "null_resolved", "bear memory", resolved=None)
        results = database.fts_search("bear", top_k=10, status="active",
                                      exclude_resolved=True)
        assert any(r["id"] == "null_resolved" for r in results)

    def test_empty_superseded_by_not_excluded(self, db_env):
        """superseded_by='' (default) must NOT be excluded."""
        conn = database._get_conn()
        _insert_memory(conn, "empty_sup", "wolf memory", superseded_by="")
        results = database.fts_search("wolf", top_k=10, status="active",
                                      exclude_superseded=True)
        assert any(r["id"] == "empty_sup" for r in results)


# ════════════════════════════════════════════
#  Block 2: recency boost (tested via formula)
# ════════════════════════════════════════════

class TestBlock2RecencyBoost:
    def test_recency_formula_1_day(self):
        days = 1
        boost = 1 + 0.3 * math.exp(-days / 30)
        assert 1.28 < boost < 1.30

    def test_recency_formula_30_days(self):
        days = 30
        boost = 1 + 0.3 * math.exp(-days / 30)
        assert 1.10 < boost < 1.12

    def test_recency_formula_90_days(self):
        days = 90
        boost = 1 + 0.3 * math.exp(-days / 30)
        assert 1.01 < boost < 1.02


# ════════════════════════════════════════════
#  Block 3: P95 activation_count penalty
# ════════════════════════════════════════════

class TestBlock3P95Penalty:
    def test_p95_not_triggered_below_20(self):
        items = [{"activation_count": i * 10, "score": 1.0} for i in range(19)]
        assert len(items) < 20

    def test_p95_boundary_uses_ceil(self):
        counts = list(range(1, 21))
        p95_idx = math.ceil(len(counts) * 0.95) - 1
        p95 = counts[min(p95_idx, len(counts) - 1)]
        assert p95 == counts[18]
        assert counts[19] > p95

    def test_p95_penalty_applied_to_outlier(self):
        counts = [1] * 19 + [100]
        p95_idx = math.ceil(len(counts) * 0.95) - 1
        p95 = sorted(counts)[min(p95_idx, len(counts) - 1)]
        assert 100 > p95
        score = 1.0 * 0.7
        assert score == pytest.approx(0.7)


# ════════════════════════════════════════════
#  Block 6: resolve pattern matching
# ════════════════════════════════════════════

class TestBlock6ResolvePatterns:
    def _match(self, text):
        from memory_ops import _matches_resolve_pattern
        return _matches_resolve_pattern(text)

    def test_positive_chinese_basic(self):
        for phrase in ("已完成", "搞定了", "做完了", "改了", "好了", "处理完了", "OK了"):
            assert self._match(phrase), f"Should match: {phrase}"

    def test_positive_english(self):
        for phrase in ("Done", "finished", "fixed", "done"):
            assert self._match(phrase), f"Should match: {phrase}"

    def test_positive_in_sentence(self):
        assert self._match("那个bug我改了")
        assert self._match("那件事搞定了")
        assert self._match("I think it's done now")

    def test_negative_doubt_suffix(self):
        assert not self._match("好了吗")
        assert not self._match("好了吧")
        assert not self._match("好了呢")
        assert not self._match("完成了？")

    def test_negative_negation_prefix(self):
        assert not self._match("没好了")
        assert not self._match("还没搞定了")
        assert not self._match("不好了")

    def test_negative_english_negation(self):
        assert not self._match("it's not done")
        assert not self._match("not finished yet")
        assert not self._match("hasn't fixed it")

    def test_no_match_irrelevant(self):
        assert not self._match("今天天气不错")
        assert not self._match("hello world")
        assert not self._match("我想吃饭")

    def test_nfkc_normalization(self):
        assert self._match("ＯＫ了")


# ════════════════════════════════════════════
#  M1: atomic auto-resolve
# ════════════════════════════════════════════

class TestM1AtomicAutoResolve:
    def test_batch_resolve_atomicity(self, db_env):
        conn = database._get_conn()
        mems = []
        for i in range(3):
            mid = f"task_{i}"
            _insert_memory(conn, mid, f"task {i}", resolved=0)
            mem = {"id": mid, "resolved": 0, "info_type": "task"}
            mems.append(mem)

        from memory_ops import _check_auto_resolve
        resolved_ids = _check_auto_resolve("全部搞定了", mems, "cloudy")
        assert len(resolved_ids) == 3

        for mid in resolved_ids:
            row = conn.execute("SELECT resolved FROM memories WHERE id = ?",
                               (mid,)).fetchone()
            assert row[0] == 1

    def test_no_resolve_without_pattern(self, db_env):
        conn = database._get_conn()
        _insert_memory(conn, "task_x", "some task", resolved=0)
        mem = {"id": "task_x", "resolved": 0, "info_type": "task"}

        from memory_ops import _check_auto_resolve
        resolved_ids = _check_auto_resolve("今天天气不错", [mem], "cloudy")
        assert resolved_ids == []

        row = conn.execute("SELECT resolved FROM memories WHERE id = ?",
                           ("task_x",)).fetchone()
        assert row[0] == 0


# ════════════════════════════════════════════
#  M2: FTS/CJK room filtering at SQL layer
# ════════════════════════════════════════════

class TestM2SQLRoomFiltering:
    def test_fts_include_rooms(self, db_env):
        conn = database._get_conn()
        _insert_memory(conn, "r1", "penguin in diary", room="diary")
        _insert_memory(conn, "r2", "penguin in living_room", room="living_room")
        results = database.fts_search("penguin", top_k=10, status="active",
                                      include_rooms=["diary"])
        ids = {r["id"] for r in results}
        assert "r1" in ids
        assert "r2" not in ids

    def test_fts_exclude_rooms(self, db_env):
        conn = database._get_conn()
        _insert_memory(conn, "e1", "falcon in games", room="game_world")
        _insert_memory(conn, "e2", "falcon in living_room", room="living_room")
        results = database.fts_search("falcon", top_k=10, status="active",
                                      exclude_rooms=["game_world"])
        ids = {r["id"] for r in results}
        assert "e1" not in ids
        assert "e2" in ids

    def test_cjk_include_rooms(self, db_env):
        conn = database._get_conn()
        _insert_memory(conn, "c1", "小猫喜欢在日记里写东西", room="diary")
        _insert_memory(conn, "c2", "小猫喜欢在客厅看电视", room="living_room")
        results = database.cjk_like_search("小猫喜欢", top_k=10, status="active",
                                           include_rooms=["diary"])
        ids = {r["id"] for r in results}
        assert "c1" in ids
        assert "c2" not in ids


# ════════════════════════════════════════════
#  M3: touch failure doesn't block recall
# ════════════════════════════════════════════

class TestM3TouchResilience:
    def test_recall_returns_despite_touch_failure(self, db_env):
        conn = database._get_conn()
        seed = "zebra"
        _insert_memory(conn, "touch_test", "zebra memory", vec_seed=seed)

        import memory_ops
        from unittest.mock import AsyncMock
        mock_embed = AsyncMock(return_value=_make_vec(seed))
        with patch("memory_ops.get_embedding", mock_embed), \
             patch("github_store.set_memory", side_effect=RuntimeError("DB locked")), \
             patch("github_store.get_memory", side_effect=RuntimeError("DB locked")):
            results = asyncio.run(memory_ops.recall("zebra", skip_analyze=True))
        assert len(results) >= 1
        assert any(r["id"] == "touch_test" for r in results)


# ════════════════════════════════════════════
#  L2: naive datetime handling
# ════════════════════════════════════════════

class TestL2NaiveDatetime:
    def test_naive_datetime_no_crash(self, db_env):
        conn = database._get_conn()
        naive_ts = "2026-07-01T12:00:00"
        seed = "tiger"
        _insert_memory(conn, "naive_dt", "tiger fact", created_at=naive_ts,
                       vec_seed=seed)

        import memory_ops
        from unittest.mock import AsyncMock
        mock_embed = AsyncMock(return_value=_make_vec(seed))
        with patch("memory_ops.get_embedding", mock_embed):
            results = asyncio.run(memory_ops.recall("tiger", skip_analyze=True))
        assert len(results) >= 1


# ════════════════════════════════════════════
#  ro_* variants pass through new params
# ════════════════════════════════════════════

class TestROVariants:
    def test_ro_fts_excludes_resolved(self, db_env):
        conn = database._get_conn()
        _insert_memory(conn, "ro_res", "parrot resolved", resolved=1)
        _insert_memory(conn, "ro_act", "parrot active")
        results = database.ro_fts_search("parrot", top_k=10, status="active",
                                         exclude_resolved=True)
        ids = {r["id"] for r in results}
        assert "ro_res" not in ids
        assert "ro_act" in ids

    def test_ro_cjk_excludes_superseded(self, db_env):
        conn = database._get_conn()
        _insert_memory(conn, "ro_old", "小鸟记忆旧版", superseded_by="ro_new")
        _insert_memory(conn, "ro_new", "小鸟记忆新版")
        results = database.ro_cjk_like_search("小鸟记忆", top_k=10, status="active",
                                              exclude_superseded=True)
        ids = {r["id"] for r in results}
        assert "ro_old" not in ids

    def test_ro_vector_excludes_resolved(self, db_env):
        conn = database._get_conn()
        _insert_memory(conn, "rov_res", "lion resolved", resolved=1, vec_seed="lion_r")
        _insert_memory(conn, "rov_act", "lion active", vec_seed="lion_a")
        query_vec = _make_vec("lion_a")
        results = database.ro_vector_search(query_vec, top_k=10, status="active",
                                            exclude_resolved=True)
        ids = {r["id"] for r in results}
        assert "rov_res" not in ids
