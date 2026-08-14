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
        # Body needs to cover: docstring + idempotency lookup + INSERT loop
        # with IntegrityError catch + id-collision return + background dispatch.
        body = src[idx:idx + 10000]
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

    def test_idempotent_response_gated_marker_is_retry_safe(self, db_env):
        """应改 3 + 必修 3: MCP path no longer emits :gated (write_gate only
        runs on quick=True). But _idempotent_response must still classify
        :gated as retry_safe=true if the marker ever appears (e.g. from a
        future gate path or manually flagged row)."""
        database.insert_pending_memory({
            "id": "skel_g", "content": "spam", "room": "living_room",
            "client_request_id": "crq_g", "status": "pending",
        })
        # Force the :gated suffix directly — MCP flow won't produce this.
        database.update_memory_status(
            "skel_g", "failed", source_platform_suffix="gated")

        got = database.get_memory("skel_g")
        assert got["status"] == "failed"
        assert got["source_platform"].endswith(":gated")

        from async_remember import _idempotent_response
        resp = json.loads(_idempotent_response(got))
        assert resp["status"] == "failed"
        assert resp["retry_safe"] is True

    def test_mcp_path_no_id_marks_pipeline_error_not_gated(self, db_env):
        """必修 3: even if impl_fn returns status='gated', MCP path treats it
        as pipeline_error because write_gate isn't in this path — we don't
        trust the string."""
        database.insert_pending_memory({
            "id": "skel_g2", "content": "x", "room": "living_room",
            "client_request_id": "crq_g2", "status": "pending",
        })

        async def gated_impl(**kw):
            return {"id": "", "status": "gated", "reason": "?"}

        from async_remember import _finalize_pending_memory as _core
        asyncio.run(_core(
            "skel_g2", impl_fn=gated_impl,
            content="x", room="living_room", category="",
            importance=0.5, source_ai="claude", event_date="",
            force_create=False, client_request_id="crq_g2",
        ))
        got = database.get_memory("skel_g2")
        assert got["status"] == "failed"
        assert got["source_platform"].endswith(":pipeline_error")

        from async_remember import _idempotent_response
        resp = json.loads(_idempotent_response(got))
        assert resp["retry_safe"] is False

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

    def test_mcp_wrapper_returns_error_dict_on_id_collision(self):
        """必修 2: after 3 skeleton_id retries, MCP wrapper must return a
        structured error dict, NOT re-raise IntegrityError (violates the
        'never bubble' contract)."""
        with open("mcp_server.py", encoding="utf-8") as f:
            src = f.read()
        idx = src.find("async def remember(")
        body = src[idx:idx + 10000]
        assert '"error": "id_collision_max_retry"' in body, \
            "MCP wrapper missing id_collision_max_retry error path"
        # The MCP wrapper's INSERT loop must not have a raw `raise` on the
        # id-collision branch. The only `raise` allowed is inside the
        # IntegrityError handler (the CRQ-lookup fallback) that we now
        # replaced with the structured error return.
        insert_loop_start = body.find("for attempt in range")
        insert_loop_end = body.find("if not inserted:")
        assert insert_loop_start != -1 and insert_loop_end != -1, \
            "id-collision loop shape changed unexpectedly"
        loop_body = body[insert_loop_start:insert_loop_end]
        assert "raise" not in loop_body, \
            "id-collision loop still raises — violates 'never bubble' contract"

    def test_created_at_preserved_across_upsert(self, db_env):
        """应改 3: pipeline UPSERT at completion must not overwrite the
        skeleton's created_at — else recency boost sees the memory as
        just-created instead of 10min-old."""
        original_ts = "2026-08-11T12:00:00+00:00"
        database.insert_pending_memory({
            "id": "skel_ts", "content": "check ts", "room": "living_room",
            "client_request_id": "crq_ts", "status": "pending",
            "created_at": original_ts,
        })
        assert database.get_memory("skel_ts")["created_at"] == original_ts

        # Simulate the pipeline UPSERT with a new created_at (this is what
        # memory_ops.remember does when reusing existing_id).
        database.set_memory({
            "id": "skel_ts", "content": "check ts", "room": "living_room",
            "layer": "shared", "status": "active",
            "created_at": "2026-08-11T13:00:00+00:00",  # newer — must be ignored
            "updated_at": "2026-08-11T13:00:00+00:00",
            "client_request_id": "crq_ts",
        })
        got = database.get_memory("skel_ts")
        assert got["created_at"] == original_ts, \
            f"created_at was overwritten: {got['created_at']}"
        # updated_at should be the new value
        assert got["updated_at"] == "2026-08-11T13:00:00+00:00"

    def test_insert_pending_honors_tags_and_domain(self, db_env):
        """应改 3 (database): insert_pending_memory used to hard-code [] and
        silently drop caller-supplied tags/domain."""
        database.insert_pending_memory({
            "id": "skel_tags", "content": "x", "room": "living_room",
            "client_request_id": "crq_tags", "status": "pending",
            "tags": ["foo", "bar"],
            "domain": ["work"],
        })
        got = database.get_memory("skel_tags")
        assert "foo" in got.get("tags", "") or "foo" in json.loads(got.get("tags", "[]"))
        assert "work" in got.get("domain", "") or "work" in json.loads(got.get("domain", "[]"))

    def test_h1_atomic_sweep_claim_no_clobber(self, db_env, monkeypatch):
        """H1 regression: sweep MUST NOT mark 'failed' a skeleton that a
        concurrent finalize has already transitioned to 'active'.

        Simulates the race by returning a stale snapshot from
        list_memories_by_status (row was pending when snapshotted, but got
        transitioned to active before sweep runs its status-change UPDATE)."""
        age = (datetime.now(timezone.utc)
               - timedelta(minutes=90)).isoformat()  # > FAIL_AFTER_MINUTES
        database.insert_pending_memory({
            "id": "skel_race", "content": "x", "room": "living_room",
            "client_request_id": "crq_race", "status": "pending",
            "created_at": age,
        })
        # Stale snapshot: sweep sees the row as pending, but it's since
        # been marked active by a concurrent finalize.
        stale_snapshot = database.list_memories_by_status(
            "pending", older_than_minutes=10)
        database.update_memory_status("skel_race", "active")

        import pending_sweep
        monkeypatch.setattr(
            pending_sweep.database, "list_memories_by_status",
            lambda status, **kw: stale_snapshot,
        )
        result = asyncio.run(pending_sweep.sweep_stuck_pending())
        assert database.get_memory("skel_race")["status"] == "active", \
            "sweep clobbered active memory back to failed"
        assert result.get("skipped_claimed", 0) >= 1

    def test_h2_ledger_short_circuit_prevents_double_pipeline(self, db_env):
        """H2 regression: if a prior pipeline committed a ledger entry
        (crash-and-recover scenario), a repeat finalize call MUST NOT
        re-run the pipeline. It should just apply the ledger's terminal
        state to the skeleton."""
        database.insert_pending_memory({
            "id": "skel_ledger", "content": "x", "room": "living_room",
            "client_request_id": "crq_ledger", "status": "pending",
        })
        # Pre-committed ledger — pretends a previous pipeline succeeded.
        database.commit_finalize_atomic(
            skeleton_id="skel_ledger",
            client_request_id="crq_ledger",
            terminal_state="active",
            result_memory_id="skel_ledger",
            skeleton_update={"status": "active"},
        )

        pipeline_calls = []

        async def crash_if_called(**kw):
            pipeline_calls.append(kw)
            raise RuntimeError("pipeline must not run when ledger exists")

        from async_remember import _finalize_pending_memory as _core
        asyncio.run(_core(
            "skel_ledger", impl_fn=crash_if_called,
            content="x", room="living_room", category="",
            importance=0.5, source_ai="claude", event_date="",
            force_create=False, client_request_id="crq_ledger",
        ))

        assert pipeline_calls == [], \
            "ledger short-circuit broken — pipeline was re-invoked"
        assert database.get_memory("skel_ledger")["status"] == "active"

    def test_h3_finalize_semaphore_bounded(self):
        """H3 sanity: semaphore lazily constructs and reports the configured
        bound. Concurrent finalize calls must respect it."""
        import async_remember
        async_remember._reset_finalize_semaphore_for_tests()

        async def _grab():
            sem = async_remember.get_finalize_semaphore()
            # First call constructs; second returns same instance
            sem2 = async_remember.get_finalize_semaphore()
            assert sem is sem2
            return sem._value

        val = asyncio.run(_grab())
        assert val == async_remember._FINALIZE_MAX_CONCURRENCY

    def test_m1_crq_content_fingerprint_conflict(self):
        """M1: reusing the same crq with different content must produce
        a crq_content_conflict error, not silently return the first row's id."""
        # Test the fingerprint logic directly by inspecting the response
        # path — the full MCP tool is unavailable locally (needs mcp module).
        # Read the source to verify the fingerprint check is present.
        with open("mcp_server.py", encoding="utf-8") as f:
            src = f.read()
        idx = src.find("async def remember(")
        body = src[idx:idx + 10000]
        assert "content_fingerprint" in body, \
            "MCP wrapper missing content fingerprint compute"
        assert '"error": "crq_content_conflict"' in body, \
            "MCP wrapper missing crq_content_conflict error path"
        assert "existing_fp != content_fingerprint" in body, \
            "MCP wrapper missing fingerprint comparison"

    def test_m1_effective_crq_namespaces_by_source_ai(self):
        """M1: effective_crq must include source_ai to prevent cross-AI collision."""
        with open("mcp_server.py", encoding="utf-8") as f:
            src = f.read()
        assert 'effective_crq = (f"{source_ai}::{client_request_id}"' in src, \
            "MCP wrapper missing source_ai namespace on effective_crq"

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
