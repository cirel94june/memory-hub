"""v5.1 S7 — background sweep for stuck pending proposals.

Recovery scenarios addressed:
  * A crashed worker left `promotion_claim_id` set but never committed
    (proposal stays `pending` forever without this sweep).
  * A crash happened between insert_proposal and inline _promote_via_kernel
    (proposal is `pending` with empty claim; sweep picks it up).

What sweep does NOT do:
  * Retry `promotion_failed` — that's the human path only (H5). Sweep
    only touches `pending` rows.
  * Sweep v=0 legacy — try_claim_promotion refuses those; sweep would
    never get a claim, so the loop simply skips them.

The sweep is intentionally conservative: bounded batches, longer interval
than the per-request path, only reacts to rows that would truly get stuck.
"""
from __future__ import annotations

import asyncio
import logging
import os

import database
import memory_ops

logger = logging.getLogger(__name__)

SWEEP_INTERVAL_SECONDS = int(os.environ.get("HUB_PROPOSAL_SWEEP_INTERVAL", "600"))
SWEEP_BATCH_SIZE = int(os.environ.get("HUB_PROPOSAL_SWEEP_BATCH", "20"))


async def sweep_once() -> dict:
    """Run one pass over recoverable auto-promotion proposals.

    v5.1 S7 Critical fix: uses database.list_recoverable_promotions which
    filters IN SQL for auto-triage + kind consistency + v=2 + reclaimable.
    Sweep NEVER touches rows that need human review (sensitive_room,
    needs_review, etc.) — those stay pending until Ceci clicks Approve.

    Hardcoded terminal_state='auto_approved' — the SQL guarantees every
    row here came from an auto triage, so the kernel's auto whitelist
    check will always pass.

    Returns {'scanned': N, 'promoted': N, 'skipped': N, 'errors': N}.
    """
    scanned = promoted = skipped = errors = 0
    rows = database.list_recoverable_promotions(limit=SWEEP_BATCH_SIZE)
    for prop in rows:
        scanned += 1
        result = await memory_ops._promote_via_kernel(
            prop["id"], reviewed_by="proposal_sweep",
            terminal_state="auto_approved",
            human_retry=False,
        )
        if result.get("error"):
            # Benign races (another live worker grabbed it, row was just
            # rejected, etc.) count as skipped, not errors.
            if result.get("error") == "claim_refused" and result.get("reason") in (
                "held_by_active_worker", "v0_legacy", "not_pending",
                "terminalized", "not_found",
            ):
                skipped += 1
            else:
                errors += 1
                logger.info(
                    "sweep error on %s: %s / %s",
                    prop["id"], result.get("error"), result.get("detail", ""),
                )
        else:
            promoted += 1
            logger.info(
                "sweep promoted %s -> %s", prop["id"], result.get("memory_id"),
            )
    return {"scanned": scanned, "promoted": promoted,
            "skipped": skipped, "errors": errors}


async def sweep_loop(shutdown_event: asyncio.Event | None = None) -> None:
    """Background loop. Waits `SWEEP_INTERVAL_SECONDS` between passes,
    exits promptly when `shutdown_event` fires.

    Cooperative with FastAPI lifespan shutdown: use asyncio.wait_for with
    the event so the process can stop within seconds even if the last
    interval was just started.
    """
    ev = shutdown_event or asyncio.Event()
    logger.info(
        "proposal_sweep loop starting: interval=%ss batch=%s",
        SWEEP_INTERVAL_SECONDS, SWEEP_BATCH_SIZE,
    )
    while not ev.is_set():
        try:
            r = await sweep_once()
            if r["promoted"] or r["errors"]:
                logger.info("proposal_sweep pass: %s", r)
        except Exception:
            logger.exception("proposal_sweep pass raised; will retry after interval")
        try:
            await asyncio.wait_for(ev.wait(), timeout=SWEEP_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass
    logger.info("proposal_sweep loop stopped")
