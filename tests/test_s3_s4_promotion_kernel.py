"""S3+S4 — promotion kernel + public wrappers + claim/fencing helpers.

Covers each contract in isolation. S5 integration test (via memory_ops
entrypoints) lives in test_s5_entrypoints.py; the end-to-end
抢占→promotion→recovery→legacy vertical is in test_s3_s6_integration.py.

Contracts:
  try_claim_promotion    fresh claim / v0 refused / stale reclaimable / held
  mark_promotion_failed  CAS on claim / rowcount==0 keeps status
  reject_proposal_atomic CAS / in-flight / already-finalized
  _commit_promotion_in_tx wrong wrapper / wrong triage / v0 refused /
                          claim lost / valid normal auto / valid manual approve
  commit_promotion_atomic normal happy path + wrong wrapper on maint prop
  commit_maintenance_promotion_atomic snapshot-required / drift / no-downgrade /
                          expected_status must be 'active' for auto
  adopt_legacy_proposal_atomic  v0→v2 hash guard / fingerprint drift /
                          legacy maint without snapshot rejected
"""
import asyncio
import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone

os.environ.setdefault("ALLOW_DEFAULT_HUB_SECRET", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import database
import memory_payload as mp


NOW = "2026-09-02T00:00:00+00:00"


@pytest.fixture
def db(monkeypatch, tmp_path):
    p = tmp_path / "s34.db"
    monkeypatch.setattr(database, "DB_PATH", p)
    asyncio.run(database.init_db(str(p)))
    yield p
    database.close_thread_read_conn()


def _mk_pending_v2(pid: str, triage: str = "auto_approve",
                   ma: str = "", target_snapshot: dict | None = None) -> None:
    row = {
        "id": pid,
        "content": f"content for {pid}",
        "claim_type": "fact",
        "speech_mode": "literal",
        "conversation_kind": "house_chat",
        "proposed_room": "living_room",
        "confidence": 0.9,
        "importance": 0.6,
        "emotion_arousal": 0.3,
        "layer": "shared",
        "created_at": NOW,
        "status": "pending",
        "proposer_ai_id": "cloudy",
        "source_platform": "test",
        "source_context": "unit",
        "provenance_type": "user_statement",
        "info_type": "fact",
        "triage_reason": triage,
        "maintenance_action": ma,
    }
    if target_snapshot is not None:
        row["target_snapshot_json"] = json.dumps(target_snapshot)
        # v5.1 H1: kernel cross-checks proposal.maintenance_target_id ==
        # snapshot.target_id, so seed helper mirrors it.
        row["maintenance_target_id"] = target_snapshot.get("target_id", "")
    database.insert_proposal(row)


def _mk_legacy_v0(pid: str, ma: str = "") -> None:
    """Seed a v=0 legacy row directly via raw SQL (bypasses insert_proposal
    which now always stamps v=2)."""
    # First insert as v=2 then bump down. Simpler than reconstructing schema.
    _mk_pending_v2(pid, ma=ma)
    conn = sqlite3.connect(str(database.DB_PATH))
    try:
        conn.execute(
            "UPDATE proposals SET promotion_protocol_version=0 WHERE id=?", (pid,)
        )
        conn.commit()
    finally:
        conn.close()


def _payload_for(pid: str, override_id: str | None = None) -> dict:
    prop = database.get_proposal(pid)
    return mp.promotion_payload_from_proposal(
        prop, override_now=NOW,
    ) if override_id is None else {
        **mp.promotion_payload_from_proposal(prop, override_now=NOW),
        "id": override_id,
    }


# ═══ try_claim_promotion ═════════════════════════════════════════════

def test_claim_fresh_pending_v2_succeeds(db):
    _mk_pending_v2("p1")
    r = database.try_claim_promotion("p1")
    assert r["ok"] and len(r["token"]) == 32


def test_claim_v0_refused(db):
    _mk_legacy_v0("p1")
    r = database.try_claim_promotion("p1")
    assert r == {"ok": False, "reason": "v0_legacy"}


def test_claim_not_pending_refused(db):
    _mk_pending_v2("p1")
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE proposals SET status='approved' WHERE id='p1'"); conn.commit(); conn.close()
    r = database.try_claim_promotion("p1")
    assert r["ok"] is False
    assert r["reason"] == "terminalized"


def test_claim_currently_held_refused(db):
    _mk_pending_v2("p1")
    first = database.try_claim_promotion("p1")
    assert first["ok"]
    second = database.try_claim_promotion("p1")
    assert second["ok"] is False
    assert second["reason"] == "held_by_active_worker"


def test_claim_stale_reclaimable(db):
    _mk_pending_v2("p1")
    r1 = database.try_claim_promotion("p1")
    assert r1["ok"]
    # Fake stale: set promotion_claim_at to well past the TTL.
    conn = sqlite3.connect(str(db))
    conn.execute(
        "UPDATE proposals SET promotion_claim_at='2020-01-01T00:00:00+00:00' WHERE id='p1'"
    ); conn.commit(); conn.close()
    r2 = database.try_claim_promotion("p1", ttl_minutes=5)
    assert r2["ok"] and r2["token"] != r1["token"]


def test_claim_not_found(db):
    r = database.try_claim_promotion("nope")
    assert r == {"ok": False, "reason": "not_found"}


# ═══ mark_promotion_failed / release_promotion_claim ═════════════════

def test_mark_failed_requires_claim_token_and_pending(db):
    _mk_pending_v2("p1")
    c = database.try_claim_promotion("p1")
    # wrong token → false, no state change
    assert database.mark_promotion_failed("p1", "bogus", "test") is False
    row = database.get_proposal("p1")
    assert row["status"] == "pending"
    assert row["promotion_claim_id"] == c["token"]
    # right token → CAS success, claim cleared
    assert database.mark_promotion_failed("p1", c["token"], "reason x") is True
    row = database.get_proposal("p1")
    assert row["status"] == "promotion_failed"
    assert row["failure_reason"] == "reason x"
    assert row["promotion_claim_id"] == ""


def test_release_claim(db):
    _mk_pending_v2("p1")
    c = database.try_claim_promotion("p1")
    assert database.release_promotion_claim("p1", "bogus") is False
    assert database.release_promotion_claim("p1", c["token"]) is True
    assert database.get_proposal("p1")["promotion_claim_id"] == ""


# ═══ reject_proposal_atomic ══════════════════════════════════════════

def test_reject_pending_no_claim_succeeds(db):
    _mk_pending_v2("p1")
    r = database.reject_proposal_atomic("p1", "ceci", "no thanks")
    assert r["status"] == "rejected"
    assert database.get_proposal("p1")["status"] == "rejected"


def test_reject_pending_with_active_claim_returns_in_flight(db):
    _mk_pending_v2("p1")
    database.try_claim_promotion("p1")
    r = database.reject_proposal_atomic("p1", "ceci", "no")
    assert r["status"] == "in_flight"
    assert database.get_proposal("p1")["status"] == "pending"


def test_reject_already_finalized_returns_note(db):
    _mk_pending_v2("p1")
    # Terminalize outside via raw SQL for the test
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE proposals SET status='auto_approved', applied_memory_id='mem_X' WHERE id='p1'"); conn.commit(); conn.close()
    r = database.reject_proposal_atomic("p1", "ceci", "no")
    assert r["status"] == "auto_approved"
    assert r["note"] == "already_finalized"
    assert r["applied_memory_id"] == "mem_X"


# ═══ _commit_promotion_in_tx (via commit_promotion_atomic wrapper) ═══

def test_kernel_normal_auto_happy_path(db):
    _mk_pending_v2("p1", triage="auto_approve")
    c = database.try_claim_promotion("p1")
    payload = _payload_for("p1", override_id="mem_new_1")
    r = database.commit_promotion_atomic("p1", c["token"], reviewed_by="system")
    assert r["memory_id"] == "mem_from_prop_p1"
    assert r["terminal_state"] == "auto_approved"
    # memory row exists
    assert database.get_memory("mem_from_prop_p1") is not None
    # proposal terminalized, applied_memory_id set, claim cleared
    row = database.get_proposal("p1")
    assert row["status"] == "auto_approved"
    assert row["applied_memory_id"] == "mem_from_prop_p1"
    assert row["promotion_claim_id"] == ""


def test_kernel_manual_approve_accepts_non_auto_triage(db):
    """v5.1 A1: manual approve must accept any triage_reason as long as
    the structural wrapper (normal vs maintenance) matches. The v5 rule
    that hardcoded MANUAL_TRIAGES={'needs_review'} would have failed
    this."""
    _mk_pending_v2("p1", triage="sensitive_room")
    c = database.try_claim_promotion("p1")
    payload = _payload_for("p1", override_id="mem_manual_1")
    r = database.commit_promotion_atomic(
        "p1", c["token"], reviewed_by="ceci",
        terminal_state="approved",
    )
    assert r["terminal_state"] == "approved"
    assert database.get_proposal("p1")["status"] == "approved"


def test_kernel_wrong_wrapper_maintenance_via_normal_fails_closed(db):
    """v5.1 A1/H2: normal wrapper refuses maintenance proposal (would
    otherwise create a duplicate active memory)."""
    _mk_pending_v2("p1", triage="auto_approve_maintenance",
                   ma="supersede", target_snapshot={
                       "target_id": "mem_target", "expected_status": "active",
                       "expected_updated_at": NOW, "relation": "supersede",
                   })
    c = database.try_claim_promotion("p1")
    payload = _payload_for("p1", override_id="mem_new_2")
    with pytest.raises(database.WrongWrapper):
        database.commit_promotion_atomic("p1", c["token"], "system")
    # Rolled back: memory not written, proposal still pending.
    assert database.get_memory("mem_from_prop_p1") is None
    assert database.get_proposal("p1")["status"] == "pending"


def test_kernel_unsupported_maintenance_action_fails_closed(db):
    _mk_pending_v2("p1", triage="auto_approve", ma="reopen_thread")
    c = database.try_claim_promotion("p1")
    payload = _payload_for("p1", override_id="mem_new_3")
    with pytest.raises(database.UnsupportedMaintenanceAction):
        database.commit_promotion_atomic("p1", c["token"], "system")
    assert database.get_memory("mem_from_prop_p1") is None
    assert database.get_proposal("p1")["status"] == "pending"


def test_kernel_wrong_triage_for_auto_normal_rejected(db):
    _mk_pending_v2("p1", triage="conflicts_with_existing")  # not in AUTO_NORMAL
    c = database.try_claim_promotion("p1")
    payload = _payload_for("p1", override_id="mem_new_4")
    with pytest.raises(ValueError):
        database.commit_promotion_atomic("p1", c["token"], "system",
                                         terminal_state="auto_approved")
    assert database.get_proposal("p1")["status"] == "pending"


def test_kernel_v0_refused(db):
    _mk_legacy_v0("p1")
    # Can't even claim v0, but simulate a caller that skipped the claim step.
    payload = _payload_for("p1", override_id="mem_x")
    with pytest.raises(database.PromotionClaimLost):
        database.commit_promotion_atomic("p1", "faketoken", "system")


def test_kernel_claim_lost_after_release(db):
    _mk_pending_v2("p1")
    c = database.try_claim_promotion("p1")
    database.release_promotion_claim("p1", c["token"])
    payload = _payload_for("p1", override_id="mem_after_release")
    with pytest.raises(database.PromotionClaimLost):
        database.commit_promotion_atomic("p1", c["token"], "system")


# ═══ commit_maintenance_promotion_atomic ═════════════════════════════

def _seed_target_memory(mem_id="mem_target", content="旧内容",
                        status="active", updated_at=NOW) -> None:
    """Seed a memory row so maintenance wrapper has something to supersede."""
    payload = mp.build_new_memory_payload(
        content=content, override_id=mem_id, override_now=updated_at,
        source_ai="cloudy",
    )
    payload["status"] = status
    payload["updated_at"] = updated_at
    database.set_memory(payload)


def test_maintenance_wrapper_requires_parsed_snapshot(db):
    _mk_pending_v2("p1", triage="auto_approve_maintenance", ma="supersede",
                   target_snapshot={"target_id": "mem_target",
                                    "expected_status": "active",
                                    "expected_updated_at": NOW,
                                    "relation": "supersede"})
    c = database.try_claim_promotion("p1")
    payload = _payload_for("p1", override_id="mem_new_maint")
    # Passing a raw dict must be refused
    with pytest.raises(ValueError, match="TargetSnapshot"):
        database.commit_maintenance_promotion_atomic("p1", c["token"], {"target_id": "mem_target", "expected_status": "active",
             "expected_updated_at": NOW, "relation": "supersede"},
            reviewed_by="system",
        )


def test_maintenance_wrapper_auto_forbids_non_active_expected_status(db):
    _seed_target_memory("mem_target", status="archived", updated_at=NOW)
    _mk_pending_v2("p1", triage="auto_approve_maintenance", ma="supersede",
                   target_snapshot={"target_id": "mem_target",
                                    "expected_status": "archived",
                                    "expected_updated_at": NOW,
                                    "relation": "supersede"})
    c = database.try_claim_promotion("p1")
    payload = _payload_for("p1", override_id="mem_new_arch")
    snap = mp.parse_target_snapshot(database.get_proposal("p1")["target_snapshot_json"])
    with pytest.raises(ValueError, match="'active'"):
        database.commit_maintenance_promotion_atomic("p1", c["token"], snap, reviewed_by="system",
        )


def test_maintenance_wrapper_drift_marks_no_downgrade(db):
    _seed_target_memory("mem_target", updated_at="2026-08-01T00:00:00+00:00")
    _mk_pending_v2("p1", triage="auto_approve_maintenance", ma="supersede",
                   target_snapshot={"target_id": "mem_target",
                                    "expected_status": "active",
                                    "expected_updated_at": "2026-08-01T00:00:00+00:00",
                                    "relation": "supersede"})
    # Drift the target after the snapshot was captured
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE memories SET updated_at='2026-09-01T00:00:00+00:00' WHERE id='mem_target'")
    conn.commit(); conn.close()
    c = database.try_claim_promotion("p1")
    payload = _payload_for("p1", override_id="mem_new_drift")
    snap = mp.parse_target_snapshot(database.get_proposal("p1")["target_snapshot_json"])
    with pytest.raises(database.MaintenanceDrift):
        database.commit_maintenance_promotion_atomic("p1", c["token"], snap, reviewed_by="system",
        )
    # No new memory, no target status change, no create fallback.
    assert database.get_memory("mem_from_prop_p1") is None
    tgt = database.get_memory("mem_target")
    assert tgt["status"] == "active"


def test_maintenance_wrapper_happy_path_supersedes_and_writes(db):
    _seed_target_memory("mem_target", updated_at="2026-08-01T00:00:00+00:00")
    _mk_pending_v2("p1", triage="auto_approve_maintenance", ma="supersede",
                   target_snapshot={"target_id": "mem_target",
                                    "expected_status": "active",
                                    "expected_updated_at": "2026-08-01T00:00:00+00:00",
                                    "relation": "supersede",
                                    "reason": "conflicts"})
    c = database.try_claim_promotion("p1")
    payload = _payload_for("p1", override_id="mem_new_ok")
    snap = mp.parse_target_snapshot(database.get_proposal("p1")["target_snapshot_json"])
    r = database.commit_maintenance_promotion_atomic("p1", c["token"], snap, reviewed_by="system",
    )
    assert r["superseded_target"] == "mem_target"
    # New memory written; old target superseded; proposal terminalized.
    assert database.get_memory("mem_from_prop_p1") is not None
    old = database.get_memory("mem_target")
    assert old["status"] == "superseded"
    assert old["superseded_by"] == "mem_from_prop_p1"
    assert database.get_proposal("p1")["status"] == "auto_approved"
    # Supersede note appended with correct schema (get_memory returns
    # comments already deserialised to list).
    comments = old["comments"] if isinstance(old["comments"], list) else json.loads(old["comments"])
    last = comments[-1]
    assert last["kind"] == "supersede_note"
    assert "conflicts" in last["content"]


# ═══ adopt_legacy_proposal_atomic ═════════════════════════════════════

def test_adopt_legacy_normal_path_bumps_and_promotes(db):
    _mk_legacy_v0("p1")
    prop = database.get_proposal("p1")
    fp = mp.canonical_proposal_fingerprint(prop)
    r = database.adopt_legacy_proposal_atomic(
        "p1", fp, reviewed_by="operator", plan_id="op-plan-1",
    )
    assert r["terminal_state"] == "approved"
    row = database.get_proposal("p1")
    assert row["status"] == "approved"
    assert row["promotion_protocol_version"] == 2
    assert database.get_memory(f"mem_from_prop_p1") is not None


def test_adopt_legacy_fingerprint_drift_rejected(db):
    _mk_legacy_v0("p1")
    prop = database.get_proposal("p1")
    fp = mp.canonical_proposal_fingerprint(prop)
    # Drift the content
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE proposals SET content='drifted' WHERE id='p1'")
    conn.commit(); conn.close()
    with pytest.raises(database.LegacyContentDrift):
        database.adopt_legacy_proposal_atomic("p1", fp, "operator")
    # No memory, still v=0, still pending.
    assert database.get_memory("mem_from_prop_p1") is None
    row = database.get_proposal("p1")
    assert row["status"] == "pending"
    assert row["promotion_protocol_version"] == 0


def test_adopt_legacy_maintenance_without_snapshot_rejected(db):
    _mk_legacy_v0("p1", ma="supersede")
    prop = database.get_proposal("p1")
    # We can't even compute the "would-be" fingerprint because canonical
    # fingerprint raises for maintenance without snapshot.
    r = database.adopt_legacy_proposal_atomic("p1", "0" * 64, "operator")
    assert r["error"] == "legacy_maintenance_without_snapshot"
    # v=0 unchanged.
    assert database.get_proposal("p1")["promotion_protocol_version"] == 0


def test_adopt_legacy_v2_row_refused(db):
    _mk_pending_v2("p1")
    r = database.adopt_legacy_proposal_atomic("p1", "0" * 64, "operator")
    assert r["error"] == "not_legacy_v0"


def test_adopt_legacy_needs_valid_reviewed_by_and_fingerprint(db):
    _mk_legacy_v0("p1")
    with pytest.raises(ValueError):
        database.adopt_legacy_proposal_atomic("p1", "0" * 64, "")
    with pytest.raises(ValueError):
        database.adopt_legacy_proposal_atomic("p1", "short", "operator")


# ═══ AST gate for the new IN_TX helper ═══════════════════════════════

def test_ast_gate_kernel_in_helpers_whitelist():
    """The new kernel must be in tests/test_step0_write_lock.py's
    _IN_TX_HELPERS or the existing gate will fail-closed against the
    calls from the wrappers."""
    from tests.test_step0_write_lock import _IN_TX_HELPERS
    assert "_commit_promotion_in_tx" in _IN_TX_HELPERS
