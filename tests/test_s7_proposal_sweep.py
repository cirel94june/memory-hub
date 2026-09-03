"""S7 sweep filter — Codex Critical fix: sweep MUST NOT touch human-review rows.

Five reversal tests:
  1. Human pending (sensitive_room / needs_review) stays untouched after sweep.
  2. Empty-claim auto row gets recovered.
  3. Stale-claim auto row gets recovered (TTL reclaim).
  4. Fresh-claim auto row is skipped (held by active worker).
  5. 20 human rows in front of 1 auto row: SQL filter picks the auto row
     first (no starvation via LIMIT eating human noise).
"""
import asyncio
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

os.environ.setdefault("ALLOW_DEFAULT_HUB_SECRET", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import database
import memory_ops
import github_store
import proposal_sweep


NOW = "2026-09-02T00:00:00+00:00"


@pytest.fixture
def env(monkeypatch, tmp_path):
    p = tmp_path / "s7.db"
    monkeypatch.setattr(database, "DB_PATH", p)
    asyncio.run(database.init_db(str(p)))

    async def fake_embedding(text):
        return [0.0] * 1536
    monkeypatch.setattr(memory_ops, "get_embedding", fake_embedding)

    mems: dict = {}
    def fake_set(m):
        mems[m["id"]] = m
        database.set_memory(m)
    monkeypatch.setattr(github_store, "set_memory", fake_set)
    monkeypatch.setattr(memory_ops, "store", github_store)

    yield {"db": p}
    database.close_thread_read_conn()


def _insert(pid, triage, ma="", created_at=NOW):
    row = {
        "status": "pending", "id": pid,
        "content": f"content-{pid}", "created_at": created_at,
        "triage_reason": triage, "maintenance_action": ma,
        "proposer_ai_id": "cloudy", "proposed_room": "living_room",
        "importance": 0.5, "emotion_arousal": 0.3, "confidence": 0.7,
    }
    database.insert_proposal(row)


# ═══ Test 1: human pending is untouched ═══════════════════════════════

def test_sweep_ignores_human_triage_rows(env):
    for pid, triage in [
        ("h_sensitive", "sensitive_room"),
        ("h_needs_review", "needs_review"),
        ("h_conflict", "conflicts_with_existing"),
        ("h_playful", "playful_speech_mode"),
    ]:
        _insert(pid, triage)
    r = asyncio.run(proposal_sweep.sweep_once())
    assert r["scanned"] == 0
    assert r["promoted"] == 0
    # All rows still pending, untouched.
    for pid in ("h_sensitive", "h_needs_review", "h_conflict", "h_playful"):
        p = database.get_proposal(pid)
        assert p["status"] == "pending"
        assert p["applied_memory_id"] == ""


# ═══ Test 2: empty-claim auto row is recovered ════════════════════════

def test_sweep_recovers_empty_claim_auto(env):
    _insert("auto_a", "auto_approve")
    _insert("auto_b", "auto_approve_silent")
    r = asyncio.run(proposal_sweep.sweep_once())
    assert r["scanned"] == 2
    assert r["promoted"] == 2
    for pid in ("auto_a", "auto_b"):
        p = database.get_proposal(pid)
        assert p["status"] == "auto_approved"
        assert p["applied_memory_id"].startswith("mem_from_prop_")


# ═══ Test 3: stale-claim auto row is recovered ════════════════════════

def test_sweep_recovers_stale_claim(env):
    _insert("stale_auto", "auto_approve")
    # Simulate a crashed worker: claim exists but claim_at is very old.
    conn = sqlite3.connect(str(env["db"]))
    conn.execute(
        "UPDATE proposals SET promotion_claim_id='dead-token', "
        "promotion_claim_at='2020-01-01T00:00:00+00:00' WHERE id='stale_auto'"
    )
    conn.commit(); conn.close()
    r = asyncio.run(proposal_sweep.sweep_once())
    assert r["scanned"] == 1
    assert r["promoted"] == 1
    p = database.get_proposal("stale_auto")
    assert p["status"] == "auto_approved"


# ═══ Test 4: fresh-claim auto row is skipped ══════════════════════════

def test_sweep_skips_actively_claimed(env):
    _insert("live_auto", "auto_approve")
    # Live worker: claim + current timestamp.
    c = database.try_claim_promotion("live_auto")
    assert c["ok"]
    r = asyncio.run(proposal_sweep.sweep_once())
    # SQL filter excludes it at the LIMIT step — scanned==0, not scanned+skipped.
    assert r["scanned"] == 0
    assert r["promoted"] == 0
    # Row still pending with the fresh claim intact.
    p = database.get_proposal("live_auto")
    assert p["status"] == "pending"
    assert p["promotion_claim_id"] == c["token"]


# ═══ Test 5: 20 human rows must not starve 1 auto row ═════════════════

def test_sweep_no_starvation_from_human_backlog(env):
    """20 sensitive_room rows created BEFORE the auto row (older
    created_at). If the sweep applied a naive LIMIT 20 before filtering
    by triage, it would return only human rows and skip the auto row.
    With SQL-level filter, the auto row is picked in the same pass."""
    old_ts = "2026-08-01T00:00:00+00:00"
    for i in range(20):
        _insert(f"h_bulk_{i:02d}", "sensitive_room", created_at=old_ts)
    _insert("late_auto", "auto_approve", created_at=NOW)
    r = asyncio.run(proposal_sweep.sweep_once())
    assert r["scanned"] == 1
    assert r["promoted"] == 1
    assert database.get_proposal("late_auto")["status"] == "auto_approved"
    # 20 human rows still untouched.
    for i in range(20):
        assert database.get_proposal(f"h_bulk_{i:02d}")["status"] == "pending"


# ═══ Bonus: maintenance auto also recovered ════════════════════════════

def test_sweep_recovers_maintenance_auto(env):
    """Kind-consistent maintenance auto row (triage=auto_approve_maintenance
    with ma='supersede') is eligible; seed a target memory + snapshot."""
    import memory_payload as mp
    tgt_id = "mem_target_s7"
    tgt = mp.build_new_memory_payload(
        content="旧记忆", override_id=tgt_id,
        override_now="2026-08-01T00:00:00+00:00", source_ai="cloudy",
    )
    tgt["status"] = "active"; tgt["updated_at"] = "2026-08-01T00:00:00+00:00"
    database.set_memory(tgt)
    snap = {
        "target_id": tgt_id, "expected_status": "active",
        "expected_updated_at": "2026-08-01T00:00:00+00:00",
        "relation": "supersede", "reason": "conflicts",
    }
    database.insert_proposal({
        "status": "pending", "id": "maint_auto",
        "content": "新替代", "created_at": NOW,
        "triage_reason": "auto_approve_maintenance",
        "maintenance_action": "supersede",
        "maintenance_target_id": tgt_id,
        "target_snapshot_json": json.dumps(snap),
        "proposer_ai_id": "cloudy", "proposed_room": "living_room",
        "importance": 0.6, "emotion_arousal": 0.3, "confidence": 0.8,
    })
    r = asyncio.run(proposal_sweep.sweep_once())
    assert r["promoted"] == 1
    assert database.get_proposal("maint_auto")["status"] == "auto_approved"
    assert database.get_memory(tgt_id)["status"] == "superseded"


# ═══ Kind-mismatch triage never sweeps ═════════════════════════════════

def test_sweep_ignores_kind_mismatch(env):
    """A row with triage=auto_approve but ma='supersede' would be a kind
    mismatch — SQL filter excludes it (kernel would raise WrongWrapper
    anyway; SQL saves the cycle)."""
    _insert("mismatch_1", "auto_approve", ma="supersede")
    _insert("mismatch_2", "auto_approve_maintenance", ma="create")
    r = asyncio.run(proposal_sweep.sweep_once())
    assert r["scanned"] == 0
    for pid in ("mismatch_1", "mismatch_2"):
        assert database.get_proposal(pid)["status"] == "pending"


# ═══ list_recoverable_promotions unit contract ═════════════════════════

def test_list_recoverable_filters_correctly(env):
    _insert("recov_1", "auto_approve")
    _insert("recov_2", "auto_approve_silent")
    _insert("recov_3", "sensitive_room")  # human-only
    # v0 legacy row
    _insert("legacy_v0", "auto_approve")
    conn = sqlite3.connect(str(env["db"]))
    conn.execute("UPDATE proposals SET promotion_protocol_version=0 WHERE id='legacy_v0'")
    conn.commit(); conn.close()
    rows = database.list_recoverable_promotions(limit=100)
    ids = {r["id"] for r in rows}
    assert ids == {"recov_1", "recov_2"}
