# -*- coding: utf-8 -*-
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
    """Test the production _apply_recency_boost helper directly."""

    def _boost(self, days_ago):
        from memory_ops import _apply_recency_boost
        now = datetime(2026, 8, 11, tzinfo=timezone.utc)
        created = (now - timedelta(days=days_ago)).isoformat()
        item = {"score": 1.0, "created_at": created}
        _apply_recency_boost([item], now_utc=now)
        return item["score"]

    def test_recency_1_day(self):
        assert 1.28 < self._boost(1) < 1.30

    def test_recency_30_days(self):
        assert 1.10 < self._boost(30) < 1.12

    def test_recency_90_days(self):
        assert 1.01 < self._boost(90) < 1.02

    def test_recency_future_timestamp_clamped(self):
        """Future timestamps must not produce >1.30 boost (days clamped to 0)."""
        from memory_ops import _apply_recency_boost
        now = datetime(2026, 8, 11, tzinfo=timezone.utc)
        future = (now + timedelta(days=365)).isoformat()
        item = {"score": 1.0, "created_at": future}
        _apply_recency_boost([item], now_utc=now)
        assert item["score"] == pytest.approx(1.3, abs=0.001)

    def test_recency_invalid_timestamp_neutral(self):
        from memory_ops import _apply_recency_boost
        item = {"score": 1.0, "created_at": "not-a-date"}
        _apply_recency_boost([item])
        assert item["score"] == 1.0

    def test_recency_naive_timestamp_handled(self):
        from memory_ops import _apply_recency_boost
        now = datetime(2026, 8, 11, tzinfo=timezone.utc)
        item = {"score": 1.0, "created_at": "2026-07-11T00:00:00"}
        _apply_recency_boost([item], now_utc=now)
        assert 1.10 < item["score"] < 1.12


# ════════════════════════════════════════════
#  Block 3: P95 activation_count penalty
# ════════════════════════════════════════════

class TestBlock3P95Penalty:
    """Test the production _apply_activation_penalty helper directly."""

    def test_below_20_no_penalty(self):
        from memory_ops import _apply_activation_penalty
        items = [{"activation_count": 100, "score": 1.0} for _ in range(19)]
        _apply_activation_penalty(items)
        assert all(item["score"] == 1.0 for item in items)

    def test_p95_boundary_exactly_20(self):
        """20 items: P95 idx = ceil(19) - 1 = 18, only counts[19] should be penalized."""
        from memory_ops import _apply_activation_penalty
        items = [{"activation_count": i + 1, "score": 1.0} for i in range(20)]
        _apply_activation_penalty(items)
        below = [it for it in items if it["activation_count"] <= 19]
        above = [it for it in items if it["activation_count"] == 20]
        assert all(it["score"] == 1.0 for it in below)
        assert all(it["score"] == pytest.approx(0.7) for it in above)

    def test_outlier_penalized(self):
        from memory_ops import _apply_activation_penalty
        items = [{"activation_count": 1, "score": 1.0} for _ in range(19)]
        items.append({"activation_count": 999, "score": 1.0})
        _apply_activation_penalty(items)
        assert items[-1]["score"] == pytest.approx(0.7)

    def test_all_zero_activation_no_penalty(self):
        from memory_ops import _apply_activation_penalty
        items = [{"activation_count": 0, "score": 1.0} for _ in range(25)]
        _apply_activation_penalty(items)
        assert all(item["score"] == 1.0 for item in items)


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

    def test_negative_english_conditional(self):
        assert not self._match("if it is fixed, tell me")
        assert not self._match("I wonder if this is fixed")
        assert not self._match("maybe it is done")
        assert not self._match("perhaps we finished")
        assert not self._match("not sure whether it's done")

    def test_negative_english_question(self):
        assert not self._match("is it fixed?")
        assert not self._match("are we done?")
        assert not self._match("Did you fix it?")

    def test_negative_chinese_conditional(self):
        assert not self._match("如果修好了就告诉我")
        assert not self._match("要是完成了记得说")
        assert not self._match("是不是搞定了")
        assert not self._match("可能已经修好了")
        assert not self._match("也许弄好了")
        assert not self._match("我不确定，但已经修好了")

    def test_negative_chinese_question_anywhere(self):
        assert not self._match("搞定了没?")
        assert not self._match("完成了？")
        assert not self._match("修好了？还是没修好？")

    def test_positive_after_semicolon_or_transitional(self):
        """Positive clause after ; / 但是 / 不过 must be recognized."""
        assert self._match("Maybe not fixed; it is fixed.")
        assert self._match("还没搞定，不过现在真的搞定了")
        assert self._match("刚才没弄好，但是现在弄好了")
        assert self._match("if it was broken, but it is fixed now")

    def test_positive_transitional_without_preceding_comma(self):
        """Transitional connectives split even without a preceding comma."""
        assert self._match("It was not fixed but now it is fixed.")
        assert self._match("还没搞定不过现在真的搞定了")
        assert self._match("It's broken however it is fixed now")
        assert self._match("刚才没弄好但是现在弄好了")


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

    def test_audit_written_atomically_with_resolve(self, db_env):
        conn = database._get_conn()
        _insert_memory(conn, "task_audit", "buy milk", resolved=0)
        mem = {"id": "task_audit", "resolved": 0, "info_type": "task"}

        from memory_ops import _check_auto_resolve
        resolved_ids = _check_auto_resolve("买牛奶搞定了", [mem], "cloudy")
        assert resolved_ids == ["task_audit"]

        # Both state change AND audit row must exist with correct field mapping.
        row = conn.execute("SELECT resolved FROM memories WHERE id = ?",
                           ("task_audit",)).fetchone()
        assert row[0] == 1
        audit = conn.execute(
            "SELECT action, target_id, source_ai, model_id, auto_executed, "
            "state_before, state_after "
            "FROM maintenance_audit WHERE target_id = ?",
            ("task_audit",),
        ).fetchall()
        assert len(audit) == 1
        action, target, source_ai, model_id, auto, sb, sa = audit[0]
        assert action == "resolve_thread"
        assert target == "task_audit"
        assert source_ai == "cloudy", f"source_ai must be 'cloudy', got {source_ai!r}"
        assert model_id == "", f"model_id must be empty, got {model_id!r}"
        assert auto == 1
        assert "resolved" in sa

    def test_audit_rollback_on_insert_failure(self, db_env):
        """If audit INSERT fails, the resolve UPDATE must also roll back."""
        conn = database._get_conn()
        _insert_memory(conn, "task_rb", "task rollback", resolved=0)
        mem = {"id": "task_rb", "resolved": 0, "info_type": "task"}

        from memory_ops import _check_auto_resolve

        # Wrap the connection: sqlite3.Connection.execute is C-level and
        # can't be monkey-patched on the instance. Wrap via _get_conn instead.
        real_conn = conn

        class FailingConn:
            def __init__(self, wrapped):
                self._c = wrapped
            def execute(self, sql, *args, **kwargs):
                if "INSERT INTO maintenance_audit" in sql:
                    raise RuntimeError("audit insert boom")
                return self._c.execute(sql, *args, **kwargs)
            def __getattr__(self, name):
                return getattr(self._c, name)

        with patch("database._get_conn", return_value=FailingConn(real_conn)):
            resolved_ids = _check_auto_resolve("搞定了", [mem], "cloudy")
        assert resolved_ids == []

        row = real_conn.execute("SELECT resolved FROM memories WHERE id = ?",
                                ("task_rb",)).fetchone()
        assert row[0] == 0, "resolved must roll back when audit fails"

    def test_already_resolved_not_double_updated(self, db_env):
        """Conditional UPDATE + rowcount check must skip rows already resolved."""
        conn = database._get_conn()
        _insert_memory(conn, "task_done", "already done", resolved=1)
        mem = {"id": "task_done", "resolved": 0, "info_type": "task"}

        from memory_ops import _check_auto_resolve
        resolved_ids = _check_auto_resolve("搞定了", [mem], "cloudy")
        assert resolved_ids == []
        audit = conn.execute(
            "SELECT COUNT(*) FROM maintenance_audit WHERE target_id = ?",
            ("task_done",),
        ).fetchone()
        assert audit[0] == 0


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


# ════════════════════════════════════════════
#  H1: vector search adaptive expansion beyond old 2000 cap
# ════════════════════════════════════════════

class TestH1VectorAdaptiveExpansion:
    def test_large_resolved_set_does_not_starve_active(self, db_env):
        """2100 resolved + 10 active: recall must expand past old 2000 cap."""
        conn = database._get_conn()
        # Batch insert for speed
        for i in range(2100):
            _insert_memory(conn, f"big_res_{i}", f"topic beta {i}", resolved=1,
                           vec_seed=f"beta_r_{i}")
        for i in range(10):
            _insert_memory(conn, f"big_act_{i}", f"topic beta active {i}",
                           vec_seed=f"beta_a_{i}")
        query_vec = _make_vec("beta_a_0")
        results = database.vector_search(query_vec, top_k=10, status="active",
                                         exclude_resolved=True)
        assert len(results) == 10, f"expected 10, got {len(results)}"
        for r in results:
            assert r["id"].startswith("big_act_")


# ════════════════════════════════════════════
#  M2: atomic touch — no lost updates on concurrent recall
# ════════════════════════════════════════════

class TestM2AtomicTouch:
    def test_touch_uses_atomic_sql_update(self, db_env):
        """Touch should increment activation_count via SQL, not read-modify-write."""
        conn = database._get_conn()
        seed = "elephant"
        _insert_memory(conn, "touch_atomic", "elephant memory", vec_seed=seed,
                       activation_count=5)

        import memory_ops
        from unittest.mock import AsyncMock
        mock_embed = AsyncMock(return_value=_make_vec(seed))
        with patch("memory_ops.get_embedding", mock_embed):
            asyncio.run(memory_ops.recall("elephant", skip_analyze=True))
        # Fresh conn read from DB
        row = conn.execute(
            "SELECT activation_count FROM memories WHERE id = ?",
            ("touch_atomic",),
        ).fetchone()
        assert row[0] == 6

    def test_touch_helper_uses_atomic_sql(self):
        """_touch_recalled_memories source uses UPDATE … COALESCE(... , 0) + 1,
        not a read-modify-write pattern. Guards against silent regressions
        that would reintroduce lost updates under concurrency."""
        import inspect
        from memory_ops import _touch_recalled_memories
        src = inspect.getsource(_touch_recalled_memories)
        assert "COALESCE(activation_count, 0) + 1" in src, \
            "touch must use atomic SQL increment"
        assert "get_memory(" not in src and "set_memory(" not in src, \
            "touch must not read-modify-write memory objects"

    def test_concurrent_atomic_increment_no_lost_updates(self, db_env):
        """Two threads racing on the same row with atomic UPDATE end at count=2.
        Verifies the SQL pattern the production helper uses (COALESCE + 1)
        with truly independent connections in independent threads."""
        import threading
        conn = database._get_conn()
        _insert_memory(conn, "conc_mem", "concurrent test memory",
                       activation_count=0)
        db_path = str(database.DB_PATH)

        def worker():
            c = sqlite3.connect(db_path, timeout=5.0)
            try:
                c.execute("BEGIN IMMEDIATE")
                c.execute(
                    "UPDATE memories SET "
                    "activation_count = COALESCE(activation_count, 0) + 1 "
                    "WHERE id = ?",
                    ("conc_mem",),
                )
                c.execute("COMMIT")
            finally:
                c.close()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads: t.start()
        for t in threads: t.join()

        row = conn.execute(
            "SELECT activation_count FROM memories WHERE id = ?",
            ("conc_mem",),
        ).fetchone()
        assert row[0] == 2, f"expected 2, got {row[0]}"


# ════════════════════════════════════════════
#  Time ripple: recall of one memory bumps nearby memories +0.3
# ════════════════════════════════════════════

class TestTimeRipple:
    def test_ripple_global_cap_across_all_reference_ts(self, db_env):
        """Ripple cap is 5*N total across all reference timestamps, not per-window."""
        conn = database._get_conn()
        base = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
        # 2 recall targets, 40 nearby neighbors (all within ±48h of both).
        # Global cap should be 5*2 = 10, not 5*2*2 = 20.
        _insert_memory(conn, "tgt_a", "tgt a", created_at=base.isoformat(),
                       activation_count=0)
        _insert_memory(conn, "tgt_b", "tgt b",
                       created_at=(base + timedelta(hours=1)).isoformat(),
                       activation_count=0)
        for i in range(40):
            _insert_memory(conn, f"nb_{i}", f"neighbor {i}",
                           created_at=(base + timedelta(hours=2 + i % 40)).isoformat(),
                           activation_count=0)

        from memory_ops import _touch_recalled_memories
        _touch_recalled_memories(["tgt_a", "tgt_b"])

        bumped = conn.execute(
            "SELECT COUNT(*) FROM memories "
            "WHERE id LIKE 'nb_%' AND activation_count > 0"
        ).fetchone()[0]
        assert bumped <= 10, f"global cap violated: bumped {bumped}, expected ≤ 10"
        assert bumped >= 1, "at least some neighbors must ripple"

    def test_recall_ripples_to_neighbor_within_48h(self, db_env):
        conn = database._get_conn()
        base = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
        seed = "ripple_seed"
        _insert_memory(conn, "target", "ripple target",
                       created_at=base.isoformat(), vec_seed=seed,
                       activation_count=0)
        # +12h neighbor — should ripple
        _insert_memory(conn, "neighbor",
                       "unrelated content but nearby in time",
                       created_at=(base + timedelta(hours=12)).isoformat(),
                       activation_count=0)
        # +72h stranger — outside window
        _insert_memory(conn, "stranger", "far away",
                       created_at=(base + timedelta(hours=72)).isoformat(),
                       activation_count=0)

        import memory_ops
        from unittest.mock import AsyncMock
        mock_embed = AsyncMock(return_value=_make_vec(seed))
        with patch("memory_ops.get_embedding", mock_embed):
            asyncio.run(memory_ops.recall("ripple", skip_analyze=True))

        neighbor = conn.execute(
            "SELECT activation_count FROM memories WHERE id = ?",
            ("neighbor",),
        ).fetchone()
        stranger = conn.execute(
            "SELECT activation_count FROM memories WHERE id = ?",
            ("stranger",),
        ).fetchone()
        assert neighbor[0] == pytest.approx(0.3), \
            f"neighbor within ±48h should get +0.3, got {neighbor[0]}"
        assert stranger[0] == 0.0, \
            f"stranger outside ±48h should stay at 0, got {stranger[0]}"


# ════════════════════════════════════════════
#  CJK Extension A coverage
# ════════════════════════════════════════════

class TestCJKExtensionA:
    def test_cjk_extension_a_grams_generated(self, db_env):
        """Extension A chars (㐀-䶿) must produce grams via cjk_like_search."""
        conn = database._get_conn()
        # 㐀 and 㐁 are Extension A characters
        content = "文本㐀㐁㐂含扩展A"
        _insert_memory(conn, "extA", content)
        results = database.cjk_like_search("㐀㐁", top_k=10, status="active")
        ids = {r["id"] for r in results}
        assert "extA" in ids

    def test_ro_cjk_extension_a_grams_generated(self, db_env):
        conn = database._get_conn()
        _insert_memory(conn, "extA_ro", "另一段㐀㐁㐂内容")
        results = database.ro_cjk_like_search("㐀㐁", top_k=10, status="active")
        ids = {r["id"] for r in results}
        assert "extA_ro" in ids
