# -*- coding: utf-8 -*-
"""
Phase 1.7 PR C — async remember + pending sweep + legacy dedup tests.

Covers:
  Step 1 · database CRUD (client_request_id / link_to_real_id / status transitions)
  Step 2 · MCP remember async wrapper (idempotency + IntegrityError catch +
           replaced tombstone flow)
  Step 3 · pending sweep (10-min retry, 60-min fail)
  Step 5 · dedup_legacy.py smoke (dry-run no data change, execute audit)

Run: ALLOW_DEFAULT_HUB_SECRET=1 python -m pytest tests/test_async_and_dedup.py -q
"""
import os
import sys
import json
import sqlite3
import asyncio
import hashlib
import subprocess
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, AsyncMock

os.environ.setdefault("ALLOW_DEFAULT_HUB_SECRET", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import database
from config import EMBEDDING_DIM


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


def _make_vec(seed: str) -> list[float]:
    h = hashlib.sha256(seed.encode()).hexdigest()
    while len(h) < EMBEDDING_DIM * 2:
        h += hashlib.sha256(h.encode()).hexdigest()
    return [((int(h[i*2:i*2+2], 16) / 255.0) - 0.5) * 2 for i in range(EMBEDDING_DIM)]


# ════════════════════════════════════════════
#  Step 1: database CRUD
# ════════════════════════════════════════════

class TestDatabaseCRUD:
    def test_insert_pending_and_lookup(self, db_env):
        database.insert_pending_memory({
            "id": "mem_1", "content": "hello", "room": "living_room",
            "client_request_id": "crq_1", "status": "pending",
        })
        got = database.get_memory_by_client_request_id("crq_1")
        assert got is not None
        assert got["id"] == "mem_1"
        assert got["status"] == "pending"
        assert got["client_request_id"] == "crq_1"
        assert got["link_to_real_id"] == ""

    def test_lookup_empty_crq_returns_none(self, db_env):
        """Never match empty client_request_id (would collide with old data)."""
        database.insert_pending_memory({
            "id": "mem_empty", "content": "no crq", "room": "living_room",
            "client_request_id": "", "status": "pending",
        })
        assert database.get_memory_by_client_request_id("") is None

    def test_unique_index_blocks_duplicate_crq(self, db_env):
        database.insert_pending_memory({
            "id": "mem_a", "content": "first", "room": "living_room",
            "client_request_id": "crq_dup", "status": "pending",
        })
        with pytest.raises(sqlite3.IntegrityError):
            database.insert_pending_memory({
                "id": "mem_b", "content": "second", "room": "living_room",
                "client_request_id": "crq_dup", "status": "pending",
            })

    def test_multiple_empty_crq_allowed(self, db_env):
        """Partial unique index: WHERE crq != '' — many empty-crq rows OK."""
        database.insert_pending_memory({
            "id": "x1", "content": "a", "room": "living_room",
            "client_request_id": "", "status": "pending",
        })
        database.insert_pending_memory({
            "id": "x2", "content": "b", "room": "living_room",
            "client_request_id": "", "status": "pending",
        })
        # No exception — partial index doesn't cover '' rows

    def test_update_status_transition(self, db_env):
        database.insert_pending_memory({
            "id": "mem_ok", "content": "content", "room": "living_room",
            "client_request_id": "crq_ok", "status": "pending",
        })
        database.update_memory_status("mem_ok", "active")
        got = database.get_memory("mem_ok")
        assert got["status"] == "active"

    def test_mark_replaced_stores_link(self, db_env):
        database.insert_pending_memory({
            "id": "skel", "content": "orphan", "room": "living_room",
            "client_request_id": "crq_r", "status": "pending",
        })
        database.mark_replaced("skel", link_to_real_id="mem_real")
        got = database.get_memory_by_client_request_id("crq_r")
        assert got["status"] == "replaced"
        assert got["link_to_real_id"] == "mem_real"

    def test_mark_replaced_requires_both_args(self, db_env):
        with pytest.raises(ValueError):
            database.mark_replaced("skel", link_to_real_id="")
        with pytest.raises(ValueError):
            database.mark_replaced("", link_to_real_id="real")

    def test_list_by_status_and_age(self, db_env):
        old = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        fresh = datetime.now(timezone.utc).isoformat()
        database.insert_pending_memory({
            "id": "old_p", "content": "stale", "room": "living_room",
            "client_request_id": "crq_old", "status": "pending",
            "created_at": old,
        })
        database.insert_pending_memory({
            "id": "fresh_p", "content": "new", "room": "living_room",
            "client_request_id": "crq_fresh", "status": "pending",
            "created_at": fresh,
        })
        stuck = database.list_memories_by_status("pending", older_than_minutes=10)
        ids = {m["id"] for m in stuck}
        assert "old_p" in ids
        assert "fresh_p" not in ids


# ════════════════════════════════════════════
#  Step 2: MCP remember async wrapper
# ════════════════════════════════════════════

class TestAsyncRememberHelpers:
    """Test async_remember helpers directly. No `mcp` module needed — the
    logic lives in async_remember.py which is import-safe on any machine.
    mcp_server.py just wires @mcp.tool() around the same helpers."""

    def _run_finalize(self, skeleton_id, fake_impl, **kwargs):
        from async_remember import _finalize_pending_memory as _core
        asyncio.run(_core(
            skeleton_id,
            impl_fn=fake_impl,
            content=kwargs.get("content", "x"),
            room=kwargs.get("room", "living_room"),
            category=kwargs.get("category", ""),
            importance=kwargs.get("importance", 0.5),
            source_ai=kwargs.get("source_ai", "claude"),
            event_date=kwargs.get("event_date", ""),
            force_create=kwargs.get("force_create", False),
        ))

    def test_finalize_pending_marks_active_when_ids_match(self, db_env):
        database.insert_pending_memory({
            "id": "skel1", "content": "hi", "room": "living_room",
            "client_request_id": "crq_a", "status": "pending",
        })

        async def fake_impl(**kw):
            return {"id": "skel1", "status": "ok"}

        self._run_finalize("skel1", fake_impl)
        assert database.get_memory("skel1")["status"] == "active"

    def test_finalize_pending_marks_replaced_when_merged(self, db_env):
        """remember() returned a different id (merged/superseded) —
        skeleton must be tombstoned with link_to_real_id, NOT deleted."""
        database.insert_pending_memory({
            "id": "skel2", "content": "hi", "room": "living_room",
            "client_request_id": "crq_b", "status": "pending",
        })
        database.insert_pending_memory({
            "id": "real_target", "content": "old", "room": "living_room",
            "client_request_id": "", "status": "active",
        })

        async def fake_impl(**kw):
            return {"id": "real_target", "status": "merged"}

        self._run_finalize("skel2", fake_impl)

        skel = database.get_memory("skel2")
        assert skel is not None, "skeleton must not be hard-deleted"
        assert skel["status"] == "replaced"
        assert skel["link_to_real_id"] == "real_target"

        # Idempotency lookup finds the skeleton and can redirect
        lookup = database.get_memory_by_client_request_id("crq_b")
        assert lookup["id"] == "skel2"
        assert lookup["link_to_real_id"] == "real_target"

    def test_finalize_pending_marks_failed_on_exception(self, db_env):
        database.insert_pending_memory({
            "id": "skel3", "content": "boom", "room": "living_room",
            "client_request_id": "crq_c", "status": "pending",
        })

        async def raising_impl(**kw):
            raise RuntimeError("simulated crash")

        self._run_finalize("skel3", raising_impl)
        assert database.get_memory("skel3")["status"] == "failed"

    def test_finalize_pending_marks_failed_on_empty_id(self, db_env):
        database.insert_pending_memory({
            "id": "skel4", "content": "gated", "room": "living_room",
            "client_request_id": "crq_d", "status": "pending",
        })

        async def gated_impl(**kw):
            return {"id": "", "status": "gated", "reason": "spam"}

        self._run_finalize("skel4", gated_impl)
        assert database.get_memory("skel4")["status"] == "failed"

    def test_idempotent_response_redirects_replaced(self):
        from async_remember import _idempotent_response
        skel = {
            "id": "skel_x", "status": "replaced", "link_to_real_id": "real_x",
            "client_request_id": "crq_x",
        }
        resp = json.loads(_idempotent_response(skel))
        assert resp["memory_id"] == "real_x"
        assert resp["status"] == "active"
        assert resp["idempotent"] is True

    def test_idempotent_response_for_active(self):
        from async_remember import _idempotent_response
        mem = {"id": "mem_a", "status": "active",
               "link_to_real_id": "", "client_request_id": "crq_a"}
        resp = json.loads(_idempotent_response(mem))
        assert resp["memory_id"] == "mem_a"
        assert resp["status"] == "active"

    def test_idempotent_response_for_pending(self):
        """Still-queued skeleton returned as-is — client can poll again."""
        from async_remember import _idempotent_response
        mem = {"id": "mem_p", "status": "pending",
               "link_to_real_id": "", "client_request_id": "crq_p"}
        resp = json.loads(_idempotent_response(mem))
        assert resp["memory_id"] == "mem_p"
        assert resp["status"] == "pending"

    def test_mcp_wrapper_has_idempotency_and_integrity_guards(self):
        """Source-inspection guard: the MCP wrapper must contain the
        idempotency query, the sqlite3.IntegrityError catch, and a
        GC-safe background dispatch. Works offline (no mcp)."""
        with open("mcp_server.py", encoding="utf-8") as f:
            src = f.read()
        idx = src.find("async def remember(")
        assert idx != -1, "remember MCP tool not found"
        body = src[idx:idx + 4500]
        assert "get_memory_by_client_request_id" in body, \
            "MCP wrapper missing idempotency lookup"
        assert "sqlite3.IntegrityError" in body, \
            "MCP wrapper missing IntegrityError catch — race would leak error"
        # P0-1: must use the GC-safe helper, not raw asyncio.create_task
        assert "_spawn_background_task" in body, \
            "MCP wrapper missing GC-safe background dispatch — task may be " \
            "garbage-collected mid-flight"
        assert "asyncio.create_task" not in body, \
            "MCP wrapper uses raw asyncio.create_task — Task ref may be " \
            "GC'd; use _spawn_background_task"
        assert "_finalize_pending_memory" in body, \
            "MCP wrapper missing finalize dispatch"

    def test_no_double_row_after_normal_create(self, db_env):
        """P0-2 regression: skeleton_id must be REUSED by the pipeline when
        remember() takes the plain 'create new memory' path. Before the fix,
        every MCP remember left two rows (skeleton replaced + real active)."""
        database.insert_pending_memory({
            "id": "skel_reuse", "content": "brand new", "room": "living_room",
            "client_request_id": "crq_reuse", "status": "pending",
        })

        async def fake_impl(**kw):
            # Simulate memory_ops.remember creating a new row using existing_id
            assert kw.get("existing_id") == "skel_reuse", \
                "pipeline was not told to reuse skeleton id"
            assert kw.get("client_request_id") == "crq_reuse", \
                "pipeline was not told the crq"
            # In real remember() this is the "Step 3 新建记忆" path — it uses
            # existing_id and set_memory() UPSERTs the skeleton row in place.
            # No new row is created.
            return {"id": "skel_reuse", "status": "created"}

        from async_remember import _finalize_pending_memory as _core
        asyncio.run(_core(
            "skel_reuse", impl_fn=fake_impl,
            content="brand new", room="living_room", category="",
            importance=0.5, source_ai="claude", event_date="",
            force_create=False, client_request_id="crq_reuse",
        ))

        # Count all rows carrying this crq — must be exactly 1
        conn = database._get_conn()
        cnt = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE client_request_id = ?",
            ("crq_reuse",),
        ).fetchone()[0]
        assert cnt == 1, f"P0-2 regressed: {cnt} rows for one crq"
        row = database.get_memory("skel_reuse")
        assert row["status"] == "active"
        assert row["link_to_real_id"] == ""

    def test_failed_gated_response_marks_retry_safe(self, db_env):
        """应改 3: failed skeleton with :gated suffix → retry_safe=true."""
        database.insert_pending_memory({
            "id": "skel_g", "content": "spam", "room": "living_room",
            "client_request_id": "crq_g", "status": "pending",
        })

        async def gated_impl(**kw):
            return {"id": "", "status": "gated", "reason": "write_gate blocked"}

        from async_remember import _finalize_pending_memory as _core
        asyncio.run(_core(
            "skel_g", impl_fn=gated_impl,
            content="spam", room="living_room", category="",
            importance=0.5, source_ai="claude", event_date="",
            force_create=False, client_request_id="crq_g",
        ))

        got = database.get_memory("skel_g")
        assert got["status"] == "failed"
        assert got["source_platform"].endswith(":gated")

        # Round-trip through _idempotent_response
        from async_remember import _idempotent_response
        resp = json.loads(_idempotent_response(got))
        assert resp["status"] == "failed"
        assert resp["retry_safe"] is True

    def test_failed_pipeline_error_marks_retry_unsafe(self, db_env):
        """应改 3: failed skeleton from pipeline crash → retry_safe=false."""
        database.insert_pending_memory({
            "id": "skel_e", "content": "crash", "room": "living_room",
            "client_request_id": "crq_e", "status": "pending",
        })

        async def raising_impl(**kw):
            raise RuntimeError("mid-pipeline crash")

        from async_remember import _finalize_pending_memory as _core
        asyncio.run(_core(
            "skel_e", impl_fn=raising_impl,
            content="crash", room="living_room", category="",
            importance=0.5, source_ai="claude", event_date="",
            force_create=False, client_request_id="crq_e",
        ))

        got = database.get_memory("skel_e")
        assert got["status"] == "failed"
        assert got["source_platform"].endswith(":pipeline_error")

        from async_remember import _idempotent_response
        resp = json.loads(_idempotent_response(got))
        assert resp["status"] == "failed"
        assert resp["retry_safe"] is False
        assert "hint" in resp

    def test_gc_safe_background_task_helper_exists(self):
        """P0-1: mcp_server and pending_sweep must define a set-backed
        create_task wrapper so background coroutines can't be GC'd."""
        for path in ("mcp_server.py", "pending_sweep.py"):
            with open(path, encoding="utf-8") as f:
                src = f.read()
            assert "_BACKGROUND_TASKS" in src or "set()" in src, \
                f"{path} missing GC-protection set for background tasks"
            assert "add_done_callback" in src, \
                f"{path} missing done_callback to drain the task set"

    def test_pending_memory_not_returned_by_default_status_queries(self, db_env):
        """Pending skeletons must not appear in status='active' queries used
        by recall / corridor. This is the guard that keeps half-baked
        skeletons out of AI-visible outputs."""
        database.insert_pending_memory({
            "id": "skel_hidden", "content": "shhh", "room": "living_room",
            "client_request_id": "crq_hidden", "status": "pending",
        })
        active_only = database.list_memories_by_status("active")
        ids = {m["id"] for m in active_only}
        assert "skel_hidden" not in ids


# ════════════════════════════════════════════
#  Step 3: pending sweep
# ════════════════════════════════════════════

class TestPendingSweep:
    def test_sweep_retries_middle_aged_pending(self, db_env):
        """15-min-old pending: sweep should retry, not mark failed.
        We stub the mcp_server import so pending_sweep can retry without
        needing the real MCP module locally."""
        age = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
        database.insert_pending_memory({
            "id": "sweep_retry", "content": "retry me", "room": "living_room",
            "client_request_id": "crq_sr", "status": "pending",
            "created_at": age,
        })

        # Provide a stub mcp_server module so pending_sweep's lazy import works.
        stub = type(sys)("mcp_server")
        finalize_calls = []

        async def fake_finalize(mem_id, **kw):
            finalize_calls.append(mem_id)

        stub._finalize_pending_memory = fake_finalize

        import pending_sweep
        saved = sys.modules.get("mcp_server")
        sys.modules["mcp_server"] = stub
        try:
            result = asyncio.run(pending_sweep.sweep_stuck_pending())
        finally:
            if saved is not None:
                sys.modules["mcp_server"] = saved
            else:
                sys.modules.pop("mcp_server", None)

        assert result["checked"] >= 1
        assert result["retried"] >= 1
        # Retry was scheduled — skeleton NOT marked failed
        assert database.get_memory("sweep_retry")["status"] == "pending"

    def test_sweep_fails_very_old_pending(self, db_env):
        """90-min-old pending: sweep should mark failed, not retry."""
        age = (datetime.now(timezone.utc) - timedelta(minutes=90)).isoformat()
        database.insert_pending_memory({
            "id": "sweep_dead", "content": "long dead", "room": "living_room",
            "client_request_id": "crq_dead", "status": "pending",
            "created_at": age,
        })

        import pending_sweep
        result = asyncio.run(pending_sweep.sweep_stuck_pending())
        assert result["failed"] >= 1
        assert database.get_memory("sweep_dead")["status"] == "failed"

    def test_p0_3_primary_defense_crq_unique_index(self, db_env):
        """P0-3 primary defense: the UNIQUE(client_request_id) index makes it
        physically impossible for two rows to share a crq. With P0-2 fix
        propagating crq through the pipeline into merge/supersede-created
        rows, this alone prevents \"second real memory\" duplication.

        Verifies the index actually rejects a second row with the same crq.
        """
        database.insert_pending_memory({
            "id": "first", "content": "a", "room": "living_room",
            "client_request_id": "crq_shared", "status": "pending",
        })
        # Try to make a second row (real memory) with the same crq — must fail.
        with pytest.raises(sqlite3.IntegrityError):
            database.insert_pending_memory({
                "id": "second", "content": "b", "room": "living_room",
                "client_request_id": "crq_shared", "status": "active",
            })
        # get_memory_by_client_request_id returns exactly one row
        assert database.get_memory_by_client_request_id("crq_shared")["id"] == "first"

    def test_sweep_link_helper_returns_none_when_crq_unique(self, db_env):
        """The _find_completed_by_crq helper is defense-in-depth for the
        (structurally impossible in this codebase, but possible from
        pre-fix data) case where a real memory somehow shares the
        skeleton's crq. Since the UNIQUE index forbids that, this helper
        returns None in the healthy path — verify it does so cleanly."""
        database.insert_pending_memory({
            "id": "skel_only", "content": "solo", "room": "living_room",
            "client_request_id": "crq_solo", "status": "pending",
        })
        import pending_sweep
        assert pending_sweep._find_completed_by_crq(
            "crq_solo", exclude_id="skel_only") is None

    def test_sweep_ignores_fresh_pending(self, db_env):
        """< 10 min old: don't touch."""
        fresh = datetime.now(timezone.utc).isoformat()
        database.insert_pending_memory({
            "id": "sweep_fresh", "content": "in progress",
            "room": "living_room",
            "client_request_id": "crq_f", "status": "pending",
            "created_at": fresh,
        })

        import pending_sweep
        result = asyncio.run(pending_sweep.sweep_stuck_pending())
        assert result["checked"] == 0
        assert database.get_memory("sweep_fresh")["status"] == "pending"


# ════════════════════════════════════════════
#  Step 5: dedup_legacy.py smoke
# ════════════════════════════════════════════

class TestDedupScriptSmoke:
    def _dedup_script_path(self):
        return os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "scripts", "dedup_legacy.py",
        )

    def test_dry_run_on_empty_db_returns_zero(self, db_env, tmp_path):
        env = os.environ.copy()
        env["ALLOW_DEFAULT_HUB_SECRET"] = "1"
        out_path = tmp_path / "report.json"
        proc = subprocess.run(
            [sys.executable, self._dedup_script_path(),
             "--dry-run", "--output", str(out_path)],
            capture_output=True, text=True, env=env, timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        assert out_path.exists()
        report = json.loads(out_path.read_text(encoding="utf-8"))
        assert report["summary"]["total_pairs"] == 0

    def test_scan_helper_finds_similar_pair(self, db_env):
        """Directly exercise _scan_room to avoid subprocess overhead."""
        sys.path.insert(0, os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "scripts",
        ))
        import dedup_legacy

        now = datetime.now(timezone.utc)
        vec = _make_vec("same_topic_seed")
        mems = [
            {"id": "m1", "content": "小猫喜欢吃鱼", "room": "living_room",
             "created_at": now.isoformat(),
             "importance": 0.5, "provenance_type": "",
             "fact_confidence": None,
             "_ts": now, "_vec": vec},
            {"id": "m2", "content": "小猫爱吃鱼", "room": "living_room",
             "created_at": (now + timedelta(days=1)).isoformat(),
             "importance": 0.5, "provenance_type": "",
             "fact_confidence": None,
             "_ts": now + timedelta(days=1), "_vec": vec},
            {"id": "m3", "content": "无关内容", "room": "living_room",
             "created_at": (now + timedelta(days=2)).isoformat(),
             "importance": 0.5, "provenance_type": "",
             "fact_confidence": None,
             "_ts": now + timedelta(days=2),
             "_vec": _make_vec("totally_different")},
        ]
        pairs = dedup_legacy._scan_room(mems, sim_threshold=0.85, window_days=3)
        # m1 × m2 same vec → sim=1.0; m1×m3, m2×m3 different vecs
        assert any(p["a_id"] == "m1" and p["b_id"] == "m2" for p in pairs)
        assert not any("m3" in (p["a_id"], p["b_id"]) for p in pairs)

    def test_scan_respects_window_days(self, db_env):
        """Memories 10 days apart should not pair even if identical."""
        sys.path.insert(0, os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "scripts",
        ))
        import dedup_legacy

        now = datetime.now(timezone.utc)
        vec = _make_vec("same_seed")
        mems = [
            {"id": "far1", "content": "同样内容",
             "room": "living_room", "created_at": now.isoformat(),
             "importance": 0.5, "provenance_type": "",
             "fact_confidence": None,
             "_ts": now, "_vec": vec},
            {"id": "far2", "content": "同样内容",
             "room": "living_room",
             "created_at": (now + timedelta(days=10)).isoformat(),
             "importance": 0.5, "provenance_type": "",
             "fact_confidence": None,
             "_ts": now + timedelta(days=10), "_vec": vec},
        ]
        pairs = dedup_legacy._scan_room(mems, sim_threshold=0.85, window_days=3)
        assert pairs == []
