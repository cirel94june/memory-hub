"""S5 + S6 integration tests.

S5: memory_ops entrypoints go through the kernel:
  - Entry A: auto normal create → kernel + fenced write
  - Entry C: manual approve v=2 (through mcp/rest review_proposal) →
    normal or maintenance wrapper by maintenance_action
  - Reject uses reject_proposal_atomic (CAS)
  - MCP `review_proposal(action='adopt_legacy')` returns legacy_operator_only
  - retriage_pending_proposals is REPORT-ONLY (no writes)

S6: operator CLI adopt-plan:
  - Refused unless HUB_OPERATOR_MODE=1
  - Rejects symlink / outside-allowed-dirs / bad checksum plan
  - Adopts a v=0 legacy proposal end-to-end
  - Refuses maintenance-without-snapshot
"""
import asyncio
import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("ALLOW_DEFAULT_HUB_SECRET", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import database
import memory_ops
import memory_payload as mp
import github_store


NOW = "2026-09-02T00:00:00+00:00"


@pytest.fixture
def env(monkeypatch, tmp_path):
    """DB isolation + fake embedding + fake store mirror so recall shims
    don't need network / OpenAI."""
    p = tmp_path / "e2e.db"
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

    yield {"db": p, "mems": mems}
    database.close_thread_read_conn()


# ═══ S5 Entry A: auto normal create through kernel ═══════════════════

def test_s5_entry_a_auto_create_uses_kernel(env):
    """A user_statement + literal proposal should auto_approve via the
    kernel (not the old three-step insert-then-promote path)."""
    r = asyncio.run(memory_ops.remember(
        content="ceci今天去了咖啡店",
        quick=True,
        provenance_type="user_statement",
        source_ai="cloudy",
    ))
    assert r["proposal_status"] == "auto_approved"
    assert r["id"].startswith("mem_from_prop_")
    # Kernel-written row lives in DB.
    mem = database.get_memory(r["id"])
    assert mem is not None
    assert mem["status"] == "active"
    # Proposal row was terminalized with applied_memory_id.
    prop = database.get_proposal(r["proposal_id"])
    assert prop["status"] == "auto_approved"
    assert prop["applied_memory_id"] == r["id"]
    assert prop["promotion_claim_id"] == ""
    assert prop["promotion_protocol_version"] == 2


# ═══ S5 Entry C: manual approve via review_proposal ═══════════════════

def test_s5_entry_c_manual_approve_normal(env):
    """A pending v=2 non-auto proposal should be approvable manually."""
    now = datetime.now(timezone.utc).isoformat()
    database.insert_proposal({"status": "pending",
        "id": "prop_manual", "content": "手动审批的记忆",
        "created_at": now, "triage_reason": "sensitive_room",
        "proposed_room": "living_room", "proposer_ai_id": "cloudy",
    })
    r = asyncio.run(memory_ops.review_proposal(
        "prop_manual", "approve", reviewed_by="ceci",
    ))
    assert r["status"] == "created"
    assert r["proposal_status"] == "approved"
    assert database.get_proposal("prop_manual")["status"] == "approved"
    assert database.get_memory(r["id"]) is not None


def test_s5_entry_c_manual_approve_v0_refused(env):
    """v=0 legacy row must not be approvable via MCP even manually."""
    now = datetime.now(timezone.utc).isoformat()
    database.insert_proposal({"status": "pending","id": "prop_v0", "content": "legacy", "created_at": now})
    conn = sqlite3.connect(str(env["db"]))
    conn.execute("UPDATE proposals SET promotion_protocol_version=0 WHERE id='prop_v0'")
    conn.commit(); conn.close()
    r = asyncio.run(memory_ops.review_proposal("prop_v0", "approve", "ceci"))
    assert r["error"] == "legacy_operator_only"
    assert database.get_proposal("prop_v0")["status"] == "pending"


def test_s5_review_reject_uses_cas(env):
    now = datetime.now(timezone.utc).isoformat()
    database.insert_proposal({"status": "pending","id": "prop_r", "content": "reject me", "created_at": now,
                              "triage_reason": "needs_review"})
    r = asyncio.run(memory_ops.review_proposal("prop_r", "reject", "ceci", "not useful"))
    assert r["status"] == "rejected"
    assert database.get_proposal("prop_r")["status"] == "rejected"


def test_s5_review_adopt_legacy_refused_via_mcp(env):
    now = datetime.now(timezone.utc).isoformat()
    database.insert_proposal({"status": "pending","id": "prop_al", "content": "legacy attempt", "created_at": now})
    conn = sqlite3.connect(str(env["db"]))
    conn.execute("UPDATE proposals SET promotion_protocol_version=0 WHERE id='prop_al'")
    conn.commit(); conn.close()
    r = asyncio.run(memory_ops.review_proposal("prop_al", "adopt_legacy", "ceci"))
    assert r["error"] == "legacy_operator_only"


# ═══ S5 Entry D: retriage is report-only ══════════════════════════════

def test_s5_retriage_writes_nothing(env):
    """v0 legacy + v2 pending + v2 auto — retriage must report all three
    categories but write zero."""
    now = datetime.now(timezone.utc).isoformat()
    database.insert_proposal({"status": "pending","id": "v0_1", "content": "legacy", "created_at": now})
    conn = sqlite3.connect(str(env["db"]))
    conn.execute("UPDATE proposals SET promotion_protocol_version=0 WHERE id='v0_1'")
    conn.commit(); conn.close()
    database.insert_proposal({"status": "pending",
        "id": "v2_auto", "content": "auto candidate", "created_at": now,
        "triage_reason": "sensitive_room",  # not auto-approvable
        "provenance_type": "user_statement", "proposed_room": "living_room",
    })
    database.insert_proposal({"status": "pending",
        "id": "v2_ok", "content": "would auto-approve", "created_at": now,
        "triage_reason": "auto_approve", "provenance_type": "user_statement",
        "proposed_room": "living_room", "claim_type": "fact", "speech_mode": "literal",
    })

    before = {p["id"]: p["status"] for p in database.list_proposals(status="pending", limit=100)}
    r = asyncio.run(memory_ops.retriage_pending_proposals())
    after = {p["id"]: p["status"] for p in database.list_proposals(status="pending", limit=100)}

    assert r["mode"] == "report_only"
    assert r["total"] == 3
    assert r["legacy_v0_skipped"] == 1
    # No status changed.
    assert before == after
    # Report shape is informative.
    assert "breakdown_by_triage" in r
    assert r["would_auto_approve"] + r["would_stay_pending"] == 2  # v0 excluded


# ═══ S5 Entry B: maintenance auto promotion via kernel ═══════════════

def test_s5_entry_b_maintenance_supersede_atomic(env):
    """When _create_proposal's relation classifier routes to
    action='supersede', the proposal should be inserted with
    target_snapshot_json and the kernel should perform supersede + new
    memory + audit in one transaction."""
    # Seed a target memory to supersede.
    payload = mp.build_new_memory_payload(
        content="ceci喜欢喝美式", override_id="mem_old_pref",
        override_now="2026-08-01T00:00:00+00:00", source_ai="cloudy",
    )
    payload["status"] = "active"
    payload["updated_at"] = "2026-08-01T00:00:00+00:00"
    database.set_memory(payload)

    now = datetime.now(timezone.utc).isoformat()
    snap = {
        "target_id": "mem_old_pref",
        "expected_status": "active",
        "expected_updated_at": "2026-08-01T00:00:00+00:00",
        "relation": "supersede",
        "reason": "contradicts existing",
    }
    database.insert_proposal({"status": "pending",
        "id": "prop_maint_1",
        "content": "ceci现在只喝拿铁了",
        "created_at": now,
        "triage_reason": "auto_approve_maintenance",
        "maintenance_action": "supersede",
        "maintenance_target_id": "mem_old_pref",
        "target_snapshot_json": json.dumps(snap),
        "proposer_ai_id": "cloudy",
        "proposed_room": "living_room",
    })
    r = asyncio.run(memory_ops._promote_via_kernel(
        "prop_maint_1", reviewed_by="system", terminal_state="auto_approved",
    ))
    assert r.get("memory_id"), f"expected success, got {r}"
    # Old memory is now superseded, new memory active.
    old = database.get_memory("mem_old_pref")
    assert old["status"] == "superseded"
    assert old["superseded_by"] == r["memory_id"]
    new = database.get_memory(r["memory_id"])
    assert new["status"] == "active"
    # Proposal row terminalized.
    p = database.get_proposal("prop_maint_1")
    assert p["status"] == "auto_approved"


# ═══ S6 CLI gates ═════════════════════════════════════════════════════

def test_s6_cli_refuses_without_operator_env(tmp_path, monkeypatch):
    """Missing HUB_OPERATOR_MODE=1 → immediate refusal."""
    monkeypatch.delenv("HUB_OPERATOR_MODE", raising=False)
    from tools.audit_stuck_proposals import cmd_adopt_plan, OperatorGateError
    ns = type("NS", (), {"plan_path": str(tmp_path / "x.json"), "i_am_operator": True, "db_path": str(env["db"]) if "env" in dir() else ":memory:"})()
    with pytest.raises(OperatorGateError, match="HUB_OPERATOR_MODE"):
        cmd_adopt_plan(ns)


def test_s6_cli_refuses_symlink(tmp_path, monkeypatch):
    monkeypatch.setenv("HUB_OPERATOR_MODE", "1")
    plan_dir = tmp_path / "plans"
    plan_dir.mkdir()
    monkeypatch.setenv("HUB_OPERATOR_PLAN_DIRS", str(plan_dir))
    real = plan_dir / "real.json"
    real.write_text('{"items":[]}', encoding="utf-8")
    link = plan_dir / "link.json"
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("symlink not supported on this filesystem")
    from tools.audit_stuck_proposals import cmd_adopt_plan, OperatorGateError
    ns = type("NS", (), {"plan_path": str(link), "i_am_operator": True, "db_path": str(env["db"]) if "env" in dir() else ":memory:"})()
    with pytest.raises(OperatorGateError, match="symlink"):
        cmd_adopt_plan(ns)


def test_s6_cli_refuses_outside_allowed_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("HUB_OPERATOR_MODE", "1")
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setenv("HUB_OPERATOR_PLAN_DIRS", str(allowed))
    # Plan is under tmp_path, NOT under allowed subdir.
    plan = tmp_path / "sneaky.json"
    plan.write_text('{"items":[]}', encoding="utf-8")
    from tools.audit_stuck_proposals import cmd_adopt_plan, OperatorGateError
    ns = type("NS", (), {"plan_path": str(plan), "i_am_operator": True, "db_path": str(env["db"]) if "env" in dir() else ":memory:"})()
    with pytest.raises(OperatorGateError, match="outside allowed"):
        cmd_adopt_plan(ns)


def test_s6_cli_refuses_bad_checksum(tmp_path, monkeypatch):
    monkeypatch.setenv("HUB_OPERATOR_MODE", "1")
    plan_dir = tmp_path / "plans"; plan_dir.mkdir()
    monkeypatch.setenv("HUB_OPERATOR_PLAN_DIRS", str(plan_dir))
    plan = plan_dir / "p.json"
    plan.write_text(json.dumps({
        "plan_id": "test", "items": [],
        "plan_sha256": "0" * 64,  # wrong
    }), encoding="utf-8")
    from tools.audit_stuck_proposals import cmd_adopt_plan, OperatorGateError
    ns = type("NS", (), {"plan_path": str(plan), "i_am_operator": True, "db_path": str(env["db"]) if "env" in dir() else ":memory:"})()
    with pytest.raises(OperatorGateError, match="checksum"):
        cmd_adopt_plan(ns)


def test_s6_cli_adopts_v0_legacy_end_to_end(env, tmp_path, monkeypatch, capsys):
    """Full round-trip: seed v0 → generate report → build plan → run --adopt-plan
    → verify legacy row becomes v2/approved and memory exists."""
    now = datetime.now(timezone.utc).isoformat()
    database.insert_proposal({"status": "pending",
        "id": "prop_legacy_e2e", "content": "旧的候选记忆",
        "created_at": now, "proposer_ai_id": "cloudy",
        "proposed_room": "living_room",
    })
    conn = sqlite3.connect(str(env["db"]))
    conn.execute("UPDATE proposals SET promotion_protocol_version=0 WHERE id='prop_legacy_e2e'")
    conn.commit(); conn.close()

    prop = database.get_proposal("prop_legacy_e2e")
    fp = mp.canonical_proposal_fingerprint(prop)

    plan_dir = tmp_path / "operator-plans"; plan_dir.mkdir()
    monkeypatch.setenv("HUB_OPERATOR_MODE", "1")
    monkeypatch.setenv("HUB_OPERATOR_PLAN_DIRS", str(plan_dir))
    plan_dict = {
        "plan_id": "e2e-plan-1",
        "created_at": now,
        "created_by": "operator@test",
        "items": [{
            "proposal_id": "prop_legacy_e2e",
            "category": "A",
            "expected_fingerprint": fp,
            "operator_decision": "adopt_as_active",
            "operator_note": "e2e test",
        }],
    }
    canonical = json.dumps(plan_dict, sort_keys=True, ensure_ascii=False)
    plan_dict["plan_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    plan_path = plan_dir / "plan.json"
    plan_path.write_text(json.dumps(plan_dict, ensure_ascii=False), encoding="utf-8")

    from tools.audit_stuck_proposals import cmd_adopt_plan
    ns = type("NS", (), {"plan_path": str(plan_path), "i_am_operator": True, "db_path": str(env["db"]) if "env" in dir() else ":memory:"})()
    rc = cmd_adopt_plan(ns)
    assert rc == 0

    row = database.get_proposal("prop_legacy_e2e")
    assert row["status"] == "approved"
    assert row["promotion_protocol_version"] == 2
    assert database.get_memory("mem_from_prop_prop_legacy_e2e") is not None


def test_s6_cli_refuses_legacy_maintenance_without_snapshot(env, tmp_path, monkeypatch):
    """A v=0 maintenance proposal with no target_snapshot_json cannot be
    adopted via CLI — must be recreated as a fresh v=2 proposal."""
    now = datetime.now(timezone.utc).isoformat()
    database.insert_proposal({"status": "pending",
        "id": "prop_legacy_maint", "content": "旧维护", "created_at": now,
        "maintenance_action": "supersede",
    })
    conn = sqlite3.connect(str(env["db"]))
    conn.execute("UPDATE proposals SET promotion_protocol_version=0 WHERE id='prop_legacy_maint'")
    conn.commit(); conn.close()

    plan_dir = tmp_path / "operator-plans"; plan_dir.mkdir()
    monkeypatch.setenv("HUB_OPERATOR_MODE", "1")
    monkeypatch.setenv("HUB_OPERATOR_PLAN_DIRS", str(plan_dir))
    plan_dict = {
        "plan_id": "reject-legacy-maint",
        "created_at": now,
        "items": [{
            "proposal_id": "prop_legacy_maint",
            "expected_fingerprint": "0" * 64,
            "operator_decision": "adopt_as_active",
        }],
    }
    canonical = json.dumps(plan_dict, sort_keys=True, ensure_ascii=False)
    plan_dict["plan_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    plan_path = plan_dir / "plan.json"
    plan_path.write_text(json.dumps(plan_dict, ensure_ascii=False), encoding="utf-8")

    from tools.audit_stuck_proposals import cmd_adopt_plan
    ns = type("NS", (), {"plan_path": str(plan_path), "i_am_operator": True, "db_path": str(env["db"]) if "env" in dir() else ":memory:"})()
    rc = cmd_adopt_plan(ns)
    assert rc == 2  # errors > 0

    # Legacy row unchanged.
    row = database.get_proposal("prop_legacy_maint")
    assert row["status"] == "pending"
    assert row["promotion_protocol_version"] == 0


# ═══ Vertical: 抢占 → promotion → recovery → legacy 隔离 ══════════════

def test_vertical_claim_then_release_then_reclaim(env):
    """A claim-holder that never commits must be reclaimable by another
    worker after the TTL, without duplicating the memory."""
    now = datetime.now(timezone.utc).isoformat()
    database.insert_proposal({"status": "pending",
        "id": "prop_stale", "content": "worker crash sim",
        "created_at": now, "triage_reason": "auto_approve",
        "proposer_ai_id": "cloudy", "proposed_room": "living_room",
    })
    c1 = database.try_claim_promotion("prop_stale")
    assert c1["ok"]
    # Simulate crashed worker: leave claim; do NOT commit.
    # New claim attempt fails while claim is fresh.
    c2 = database.try_claim_promotion("prop_stale")
    assert c2["ok"] is False
    # Force stale by rewriting claim_at
    conn = sqlite3.connect(str(env["db"]))
    conn.execute(
        "UPDATE proposals SET promotion_claim_at='2020-01-01T00:00:00+00:00' "
        "WHERE id='prop_stale'"
    )
    conn.commit(); conn.close()
    # A recovery worker calls _promote_via_kernel which internally
    # try_claim_promotion — since c1's claim is now stale, the kernel
    # gets its own fresh claim and proceeds.
    r = asyncio.run(memory_ops._promote_via_kernel(
        "prop_stale", reviewed_by="system", terminal_state="auto_approved",
    ))
    assert r.get("memory_id"), f"expected success, got {r}"
    # Old worker's token is now stale — future attempts to claim/commit fail.
    c4 = database.try_claim_promotion("prop_stale")
    assert c4["ok"] is False  # already auto_approved


def test_vertical_legacy_never_swept(env):
    """A v=0 legacy row must never be picked up by any auto worker path
    (try_claim_promotion refuses it)."""
    now = datetime.now(timezone.utc).isoformat()
    database.insert_proposal({"status": "pending","id": "prop_isolated", "content": "legacy", "created_at": now})
    conn = sqlite3.connect(str(env["db"]))
    conn.execute("UPDATE proposals SET promotion_protocol_version=0 WHERE id='prop_isolated'")
    conn.commit(); conn.close()
    r = database.try_claim_promotion("prop_isolated")
    assert r == {"ok": False, "reason": "v0_legacy"}
    # Even a direct _promote_via_kernel call rejects (via kernel PromotionClaimLost or claim_refused).
    r2 = asyncio.run(memory_ops._promote_via_kernel(
        "prop_isolated", reviewed_by="system", terminal_state="auto_approved",
    ))
    assert r2.get("error") == "claim_refused"
    assert database.get_proposal("prop_isolated")["status"] == "pending"
