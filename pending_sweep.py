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
                database.update_memory_status(skel["id"], "failed")
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

        try:
            asyncio.create_task(_finalize_pending_memory(
                skel["id"],
                content=skel.get("content", ""),
                room=skel.get("room", "living_room"),
                category=skel.get("category", ""),
                importance=float(skel.get("importance") or 0.5),
                source_ai=skel.get("source_ai", ""),
                event_date=skel.get("event_date", ""),
                force_create=False,
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


def _write_audit(mem_id: str, action: str, reason: str) -> None:
    """Best-effort audit row so ops can trace sweep decisions."""
    try:
        database.insert_audit({
            "action": action,
            "target_id": mem_id,
            "decision_reason": reason,
            "state_before": json.dumps({"status": "pending"}, ensure_ascii=False),
            "state_after": json.dumps({"status": action.split("_")[-1]},
                                      ensure_ascii=False),
            "auto_executed": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception:
        # audit write is not load-bearing; log and move on
        logger.exception("sweep audit write failed for %s", mem_id)
