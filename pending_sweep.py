# -*- coding: utf-8 -*-
"""
Pending memory sweep (Phase 1.7 块 8).

Async remember writes a `status='pending'` skeleton and kicks off a background
pipeline. If the process crashes mid-pipeline (OOM, restart, unhandled bug),
the skeleton is stuck: it has no embedding, wasn't classified, and would
mislead recall/corridor if resurrected.

This module runs periodically (called from daemon.run_full_maintenance) and:
  - retries `_finalize_pending_memory` for skeletons older than 10 minutes
    (short enough to catch transient failures, long enough to skip normal
    in-flight pipelines that take 30-70 seconds)
  - marks skeletons older than 60 minutes as `status='failed'` (any pipeline
    that hasn't returned in an hour has almost certainly been abandoned)
  - writes a maintenance_audit row on every state change so ops can trace
    what happened later

Not the same as `merge_similar` or `distill_psychology` — those transform
active memories. This one only unsticks pending skeletons.
"""
import os
import json
import logging
import asyncio
from datetime import datetime, timezone

import database
import async_remember  # for the shared finalize semaphore

logger = logging.getLogger("memory_hub.pending_sweep")

RETRY_AFTER_MINUTES = 10
FAIL_AFTER_MINUTES = 60
SWEEP_INTERVAL_SECONDS = 600  # 10 min — high-freq, independent of nightly daemon
# H3: cap batch size per sweep tick. Sweep must not fan out hundreds of
# analyzer/embedding pipelines at once — that DDoSes DeepSeek + SQLite.
MAX_RETRIES_PER_SWEEP = 20
# H2 round-6: intent ledger stale timeout. After this, the intent's owner
# is assumed dead. We do NOT replay the pipeline — replaying would risk
# double-merge into a target memory the crashed run already partially
# updated. Instead, close the intent + mark skeleton failed for human review.
INTENT_STALE_MINUTES = 30

# GC-safe registry (see mcp_server._spawn_background_task).
_BACKGROUND_TASKS: set = set()


def _spawn_bg(coro):
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return task


async def _run_finalize_bounded(finalize_fn, skeleton_id: str, **kwargs) -> None:
    """Trampoline that just forwards to the finalizer.

    IMPORTANT: do NOT acquire the semaphore here. `_finalize_pending_memory`
    already acquires it internally; taking it a second time in the outer
    layer causes a deadlock once sweep batch > semaphore capacity: outer
    waiters hold N permits, inner tasks block on the same semaphore, and no
    one ever releases.

    (This wrapper is retained so the semantic "sweep dispatches with the
    same shape as MCP" stays obvious to readers — but no double-acquire.)
    """
    await finalize_fn(skeleton_id, **kwargs)


async def sweep_stuck_pending() -> dict:
    """Run one sweep of pending skeletons. Returns a summary dict.

    Concurrency: TWO different sweep instances running at the same time
    (deployment overlap, timer overrun) could each grab the same skeleton
    row from list_memories_by_status. Guard: every state transition uses
    `update_memory_status(..., require_status='pending')` and only proceeds
    if rowcount == 1. This atomically claims the row from at most one sweep.
    """
    result = {
        "checked": 0,
        "retried": 0,
        "failed": 0,
        "still_pending": 0,
        "skipped_claimed": 0,
        "intent_timeouts": 0,
    }

    # H2 round-6 + H round-7: BEFORE looking at pending skeletons, close
    # out any stale in_flight intent ledgers via an atomic reconciliation
    # against the skeleton row (see close_stale_intent_atomic docstring).
    # This handles the case where the pipeline reused the skeleton (create
    # path → skeleton='active'), or merged into another target (→
    # 'replaced'), or genuinely died mid-pipeline (→ 'failed'). The single
    # atomic helper prevents the ledger/skeleton mismatch class of bugs.
    stale_intents = database.list_stale_intent_ledgers(
        older_than_minutes=INTENT_STALE_MINUTES)
    for stale in stale_intents:
        skel_id = stale["skeleton_id"]
        token = stale["owner_token"]
        try:
            outcome = database.close_stale_intent_atomic(
                skel_id, token,
                reason=(f"stale intent > {INTENT_STALE_MINUTES}min: "
                        f"pipeline crashed with unknown side effects, "
                        f"reconciled based on skeleton status"),
            )
            if not outcome.get("transitioned"):
                # Either owner beat us to it, or skeleton is in an
                # inconsistent state — leave for human review.
                logger.info(
                    "pending sweep: stale intent %s not transitioned "
                    "(disposition=%s, skel_status=%s)",
                    skel_id, outcome.get("disposition"),
                    outcome.get("skeleton_status"))
                continue
            result["intent_timeouts"] += 1
            logger.warning(
                "pending sweep: reconciled stale intent %s (owner=%s, "
                "disposition=%s)",
                skel_id, token[:8], outcome.get("disposition"))
        except Exception:
            logger.exception(
                "sweep failed to reconcile stale intent %s", skel_id)

    # Snapshot candidate ids; each transition is then re-validated atomically.
    stuck = database.list_memories_by_status(
        "pending", older_than_minutes=RETRY_AFTER_MINUTES, limit=500,
    )
    result["checked"] = len(stuck)
    if not stuck:
        return result

    # H3: bounded retry batch — sweep must not fan out 500 pipelines at once.
    batch = stuck[:MAX_RETRIES_PER_SWEEP]

    finalize_fn = None
    now = datetime.now(timezone.utc)
    for skel in batch:
        age_min = _age_minutes(skel.get("created_at", ""), now)

        if age_min > FAIL_AFTER_MINUTES:
            # Give up — pipeline dead too long. Atomic claim via
            # require_status='pending' + rowcount check ensures we don't
            # clobber a concurrent finalize that just marked it active.
            try:
                rc = database.update_memory_status(
                    skel["id"], "failed",
                    source_platform_suffix="sweep_timeout",
                    require_status="pending",
                )
                if rc != 1:
                    result["skipped_claimed"] += 1
                    logger.info(
                        "pending sweep: skeleton %s already transitioned "
                        "(rowcount=%d), not marking failed", skel["id"], rc)
                    continue
                _write_audit(
                    skel["id"], "sweep_fail",
                    reason=f"pending > {FAIL_AFTER_MINUTES} min "
                           f"(age={age_min:.1f}min)",
                )
                result["failed"] += 1
                logger.warning(
                    "pending sweep: skeleton %s failed after %.1f min",
                    skel["id"], age_min,
                )
            except Exception:
                logger.exception("sweep failed to mark %s failed", skel["id"])
            continue

        # Retry path — lazily import finalize.
        if finalize_fn is None:
            try:
                from mcp_server import _finalize_pending_memory
                finalize_fn = _finalize_pending_memory
            except Exception:
                logger.exception(
                    "cannot import _finalize_pending_memory — "
                    "aborting this sweep")
                break

        # H1 round-4: real atomic claim via finalize_claim_id column.
        # Two concurrent sweeps race for the claim; only the winner
        # (rowcount==1) proceeds. Stale claims (>30min) get reclaimed.
        claim_token = f"sweep_{os.getpid()}_{id(skel)}"
        if not database.try_claim_finalize(skel["id"], claim_token):
            result["skipped_claimed"] += 1
            continue

        # P0-3: crq-based defense (see _find_completed_by_crq docstring).
        crq = skel.get("client_request_id", "")
        if crq:
            other = _find_completed_by_crq(crq, exclude_id=skel["id"])
            if other:
                try:
                    database.mark_replaced(
                        skel["id"], link_to_real_id=other["id"])
                    _write_audit(
                        skel["id"], "sweep_link",
                        reason=f"found completed sibling {other['id']} via crq",
                    )
                    result["retried"] += 1
                    logger.info(
                        "pending sweep: linked skeleton %s → existing %s",
                        skel["id"], other["id"],
                    )
                except Exception:
                    logger.exception("sweep link crashed for %s", skel["id"])
                    result["still_pending"] += 1
                continue

        # We hold the claim. Spawn finalize; it will release claim on
        # terminal transition via commit_finalize_atomic.
        try:
            _spawn_bg(_run_finalize_bounded(
                finalize_fn,
                skel["id"],
                content=skel.get("content", ""),
                room=skel.get("room", "living_room"),
                category=skel.get("category", ""),
                importance=float(skel.get("importance") or 0.5),
                source_ai=skel.get("source_ai", ""),
                event_date=skel.get("event_date", ""),
                force_create=False,
                client_request_id=crq,
            ))
            _write_audit(
                skel["id"], "sweep_retry",
                reason=f"pending > {RETRY_AFTER_MINUTES} min "
                       f"(age={age_min:.1f}min)",
            )
            result["retried"] += 1
        except Exception:
            logger.exception("sweep retry crashed for %s", skel["id"])
            result["still_pending"] += 1
            # Best-effort: release claim so a future sweep can retry.
            try:
                database.release_finalize_claim(skel["id"])
            except Exception:
                pass

    return result


def _age_minutes(iso_ts: str, now: datetime) -> float:
    if not iso_ts:
        return 0.0
    try:
        t = datetime.fromisoformat(iso_ts)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return (now - t).total_seconds() / 60.0
    except Exception:
        return 0.0


def _find_completed_by_crq(crq: str, exclude_id: str) -> dict | None:
    """P0-3 defense-in-depth: look for any non-pending, non-failed memory
    with matching crq that isn't the skeleton itself. If one exists, a
    prior crashed pipeline already produced a real memory — sweep must
    NOT re-run the pipeline (that would duplicate).

    Note: with the UNIQUE(client_request_id) partial index (added in the
    same PR), it is *structurally impossible* for two rows to share a crq
    inside a healthy DB — the skeleton claims the crq slot the moment
    it's written. This helper is dead code in the healthy path; it survives
    only to catch pre-fix legacy rows (crq assigned before the index) or
    manual DB surgery. Do not remove — the safety cost is a single lookup
    per stuck skeleton (once per 10 min).
    """
    if not crq:
        return None
    found = database.get_memory_by_client_request_id(crq)
    if not found or found["id"] == exclude_id:
        return None
    status = found.get("status", "")
    if status in ("active", "replaced"):
        return found
    return None


# Real state after action — 应改 1: no more split('_')[-1] cleverness.
_ACTION_TO_STATE_AFTER = {
    "sweep_retry": "pending",   # retry kicked off; skeleton still pending
    "sweep_link":  "replaced",  # linked to existing sibling via crq
    "sweep_fail":  "failed",    # timed out, gave up
}


def _write_audit(mem_id: str, action: str, reason: str) -> None:
    """Best-effort audit row so ops can trace sweep decisions."""
    try:
        database.insert_audit({
            "action": action,
            "target_id": mem_id,
            "decision_reason": reason,
            "state_before": json.dumps({"status": "pending"}, ensure_ascii=False),
            "state_after": json.dumps(
                {"status": _ACTION_TO_STATE_AFTER.get(action, "unknown")},
                ensure_ascii=False,
            ),
            "auto_executed": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception:
        # audit write is not load-bearing; log and move on
        logger.exception("sweep audit write failed for %s", mem_id)


# P0-5: independent 10-min loop. main.py lifespan should call start_sweep_loop().

async def _sweep_loop() -> None:
    """Run sweep_stuck_pending forever, once every SWEEP_INTERVAL_SECONDS."""
    logger.info("pending_sweep loop started (interval=%ds)", SWEEP_INTERVAL_SECONDS)
    while True:
        try:
            result = await sweep_stuck_pending()
            if result.get("checked", 0) > 0:
                logger.info("pending_sweep tick: %s", result)
        except asyncio.CancelledError:
            logger.info("pending_sweep loop cancelled")
            raise
        except Exception:
            logger.exception("pending_sweep tick crashed (loop continues)")
        try:
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            logger.info("pending_sweep loop cancelled during sleep")
            raise


def start_sweep_loop() -> asyncio.Task:
    """Kick off the 10-min sweep loop. Returns the Task so lifespan can
    cancel it on shutdown. Task is GC-protected via _BACKGROUND_TASKS."""
    return _spawn_bg(_sweep_loop())
