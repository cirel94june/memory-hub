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
import json
import logging
import asyncio
from datetime import datetime, timezone

import database

logger = logging.getLogger("memory_hub.pending_sweep")

RETRY_AFTER_MINUTES = 10
FAIL_AFTER_MINUTES = 60
SWEEP_INTERVAL_SECONDS = 600  # 10 min — high-freq, independent of nightly daemon

# GC-safe registry (see mcp_server._spawn_background_task).
_BACKGROUND_TASKS: set = set()


def _spawn_bg(coro):
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return task


async def sweep_stuck_pending() -> dict:
    """Run one sweep of pending skeletons. Returns a summary dict.

    Callers: daemon.run_full_maintenance (every ~1 hour), and tests.
    Safe to call concurrently — each skeleton is treated independently and
    state changes are single-row atomic UPDATEs.
    """
    result = {
        "checked": 0,
        "retried": 0,
        "failed": 0,
        "still_pending": 0,
    }

    # Only look at pending skeletons older than the retry threshold.
    stuck = database.list_memories_by_status(
        "pending", older_than_minutes=RETRY_AFTER_MINUTES, limit=500,
    )
    result["checked"] = len(stuck)
    if not stuck:
        return result

    now = datetime.now(timezone.utc)
    for skel in stuck:
        age_min = _age_minutes(skel.get("created_at", ""), now)

        if age_min > FAIL_AFTER_MINUTES:
            # Give up — pipeline has been dead too long.
            try:
                # 应改 3: tag 'sweep_timeout' so _idempotent_response can emit
                # retry_safe=false (state is unknown; a retry could duplicate).
                database.update_memory_status(
                    skel["id"], "failed",
                    source_platform_suffix="sweep_timeout")
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

        # Retry the pipeline. Late import to avoid the mcp module dependency
        # in daemon-only environments.
        try:
            from mcp_server import _finalize_pending_memory
        except Exception:
            logger.exception("cannot import _finalize_pending_memory")
            continue

        # P0-3: before retrying, check whether a previous crashed run
        # already produced a real memory with this crq. If yes, just
        # mark_replaced the skeleton — retrying would duplicate.
        crq = skel.get("client_request_id", "")
        if crq:
            other = _find_completed_by_crq(crq, exclude_id=skel["id"])
            if other:
                try:
                    database.mark_replaced(skel["id"], link_to_real_id=other["id"])
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

        try:
            _spawn_bg(_finalize_pending_memory(
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
    """P0-3: look for any non-pending, non-failed memory with matching crq
    that isn't the skeleton itself. If one exists, a prior crashed pipeline
    already produced a real memory — we must NOT re-run the pipeline.
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
