# -*- coding: utf-8 -*-
"""
Async remember helpers (Phase 1.7 块 8).

Kept in a module that does NOT import `mcp` so unit tests can exercise the
logic without needing the FastMCP dependency. mcp_server imports and wires
these behind @mcp.tool().

Two exported callables:
  - _idempotent_response(existing)  — build JSON for a client_request_id hit
  - _finalize_pending_memory(...)   — background pipeline runner

The MCP tool wrapper (in mcp_server.py) handles:
  - idempotency lookup + skeleton INSERT (sync, <10ms)
  - sqlite3.IntegrityError catch on race
  - asyncio.create_task fire-and-forget of _finalize_pending_memory
"""
import os
import json
import uuid
import asyncio
import logging
from typing import Callable, Awaitable

import database

logger = logging.getLogger("memory_hub.async_remember")


# H3: bounded concurrency for the async remember pipeline. Every
# _finalize_pending_memory call (from both MCP direct dispatch and pending
# sweep retry) must acquire this before running analyzer/embedding LLMs.
# 4 concurrent pipelines matches the DeepSeek burst budget; tune via env
# variable if the API tier changes.
_FINALIZE_MAX_CONCURRENCY = int(
    os.environ.get("MEMORY_HUB_FINALIZE_CONCURRENCY", "4"))
# M1 round-4: semaphore is loop-local. asyncio.Semaphore binds to the event
# loop that first calls acquire(); reusing across loops throws
# "Semaphore ... is bound to a different event loop". Keyed by id(loop)
# so per-test-loop fixtures each get their own semaphore automatically.
_FINALIZE_SEMAPHORES: dict[int, asyncio.Semaphore] = {}


def get_finalize_semaphore() -> asyncio.Semaphore:
    """Return the finalize semaphore bound to the current event loop.

    Multiple loops (test fixtures, embedded interpreters) each get their
    own instance to avoid cross-loop binding errors.
    """
    loop = asyncio.get_running_loop()
    key = id(loop)
    sem = _FINALIZE_SEMAPHORES.get(key)
    if sem is None:
        sem = asyncio.Semaphore(_FINALIZE_MAX_CONCURRENCY)
        _FINALIZE_SEMAPHORES[key] = sem
    return sem


def _reset_finalize_semaphore_for_tests() -> None:
    """Test-only: drop all cached semaphores so pytest's per-loop fixtures
    start fresh. Safe to call between tests."""
    _FINALIZE_SEMAPHORES.clear()


def _idempotent_response(existing: dict) -> str:
    """Build a JSON response for an existing memory found by client_request_id.

    Handles 4 statuses:
      - active   → return the memory as done
      - pending  → still queued (client can poll again)
      - replaced → redirect to link_to_real_id (real memory that
                   memory_ops.remember created via merge/supersede)
      - failed   → tell the caller, plus retry_safe. Currently the MCP path
                   only produces :pipeline_error and :sweep_timeout failures
                   (never :gated — write_gate runs on quick=True only, and
                   MCP always calls with quick=False). Both are treated as
                   retry_safe=false because they mean the pipeline may have
                   partially written state. A future gated path would be
                   distinguishable by source_platform ending in :gated.
    """
    status = existing.get("status", "unknown")
    if status == "replaced":
        real_id = existing.get("link_to_real_id") or existing["id"]
    else:
        real_id = existing["id"]

    payload: dict = {
        "status": "active" if status == "replaced" else status,
        "memory_id": real_id,
        "client_request_id": existing.get("client_request_id", ""),
        "idempotent": True,
    }
    if status == "failed":
        platform = (existing.get("source_platform") or "").lower()
        # Only :gated failures (content rejected before any write) are safe
        # to retry. In the current MCP flow this suffix is never applied,
        # so the effective answer is always False. Keep the check so the
        # semantics stay correct if the gated path is reintroduced.
        payload["retry_safe"] = platform.endswith(":gated")
        if not payload["retry_safe"]:
            payload["hint"] = (
                "pipeline failure of unknown scope; a retry may duplicate. "
                "Check maintenance_audit for skeleton id and decide manually."
            )
    return json.dumps(payload, ensure_ascii=False)


async def _finalize_pending_memory(
    skeleton_id: str,
    *,
    impl_fn: Callable[..., Awaitable[dict]],
    content: str,
    room: str,
    category: str,
    importance: float,
    source_ai: str,
    event_date: str,
    force_create: bool,
    client_request_id: str = "",
) -> None:
    """Background pipeline: run the injected sync impl and reconcile the
    skeleton row.

    Wrapped in a bounded semaphore (H3) so concurrent MCP requests and sweep
    retries don't fan out unbounded analyzer/embedding calls.

    Passes existing_id=skeleton_id to the pipeline so the common
    create-new-memory path REUSES the skeleton row (P0-2: no more double
    rows).  Also passes client_request_id so merge/supersede-created rows
    inherit the crq — sweep retries after a crash can find them and won't
    duplicate (P0-3).

      - real_id == skeleton_id → skeleton was the actual write, mark active
      - real_id != skeleton_id → remember() merged into a different existing
        target; skeleton becomes tombstone with link_to_real_id so idempotent
        lookups redirect
      - no id / exception → mark skeleton 'failed'

    impl_fn is injected (rather than hard-coded) so tests can pass a fake
    without importing mcp_server.
    """
    async with get_finalize_semaphore():
      await _finalize_pending_memory_inner(
          skeleton_id, impl_fn=impl_fn, content=content, room=room,
          category=category, importance=importance, source_ai=source_ai,
          event_date=event_date, force_create=force_create,
          client_request_id=client_request_id,
      )


async def _finalize_pending_memory_inner(
    skeleton_id: str,
    *,
    impl_fn: Callable[..., Awaitable[dict]],
    content: str,
    room: str,
    category: str,
    importance: float,
    source_ai: str,
    event_date: str,
    force_create: bool,
    client_request_id: str = "",
) -> None:
    """Inner (post-semaphore) body — split so the semaphore acquire happens
    at the outer layer only, keeping the transactional logic testable
    independently."""
    # H2 round-4 + H1 round-6: two-phase ledger with owner_token.
    # write_intent_ledger atomically INSERTs an 'in_flight' row keyed on
    # skeleton_id; rowcount==1 means we own it. Another concurrent writer
    # with a different token would see rowcount==0 and get 'in_flight' back.
    # If we see a terminal state, another pipeline already finished — apply
    # it to the skeleton and return without running the pipeline.
    owner_token = uuid.uuid4().hex
    try:
        intent = database.write_intent_ledger(
            skeleton_id, client_request_id, owner_token)
    except Exception:
        # H2 round-6 fail-closed: DB error means we don't know the ledger
        # state — do NOT proceed with side effects. Sweep will retry later.
        logger.exception(
            "write_intent_ledger failed for %s — refusing to run pipeline",
            skeleton_id)
        return
    if intent in ("active", "replaced", "failed"):
        prior = database.get_ledger(skeleton_id)
        if prior:
            _apply_ledger_to_skeleton(skeleton_id, prior)
        return
    if intent == "in_flight":
        # Another owner_token holds the intent slot. Do NOT re-run — that
        # would risk duplicating a partial merge. Sweep will re-evaluate
        # on the next tick; a stale in_flight will be handled by
        # pending_sweep's intent-timeout policy (fail-safe, no rerun).
        logger.info(
            "finalize skipping %s: intent ledger already in_flight "
            "(different owner)", skeleton_id)
        return

    try:
        result = await impl_fn(
            content=content, room=room, category=category, importance=importance,
            source_ai=source_ai, event_date=event_date, force_create=force_create,
            existing_id=skeleton_id,
            client_request_id=client_request_id,
        )
        real_id = (result or {}).get("id", "")
        if real_id == skeleton_id:
            # Common case: skeleton became the real memory in-place.
            # Ledger + status atomic commit (H4).
            committed = database.commit_finalize_atomic(
                skeleton_id=skeleton_id,
                client_request_id=client_request_id,
                terminal_state="active",
                result_memory_id=real_id,
                skeleton_update={"status": "active"},
                owner_token=owner_token,
            )
            if not committed:
                # Race: another owner terminaled first. Read their result
                # and apply to skeleton so caller sees a consistent state.
                prior = database.get_ledger(skeleton_id)
                if prior:
                    _apply_ledger_to_skeleton(skeleton_id, prior)
        elif real_id:
            # Merge path: content matched an existing memory. Skeleton
            # becomes tombstone with redirect pointer, all atomic with
            # ledger commit (H2: prevents crash-between-merge-and-mark).
            committed = database.commit_finalize_atomic(
                skeleton_id=skeleton_id,
                client_request_id=client_request_id,
                terminal_state="replaced",
                result_memory_id=real_id,
                skeleton_update={
                    "status": "replaced",
                    "link_to_real_id": real_id,
                },
                owner_token=owner_token,
            )
            if not committed:
                prior = database.get_ledger(skeleton_id)
                if prior:
                    _apply_ledger_to_skeleton(skeleton_id, prior)
        else:
            # No id returned. MCP always calls with quick=False so we
            # treat missing id as pipeline error (unknown side effects).
            committed = database.commit_finalize_atomic(
                skeleton_id=skeleton_id,
                client_request_id=client_request_id,
                terminal_state="failed",
                result_memory_id="",
                skeleton_update={
                    "status": "failed",
                    "source_platform_suffix": "pipeline_error",
                },
                owner_token=owner_token,
            )
            if not committed:
                prior = database.get_ledger(skeleton_id)
                if prior:
                    _apply_ledger_to_skeleton(skeleton_id, prior)
    except Exception:
        # Exception mid-pipeline: partial state possible → NOT retry_safe.
        logger.exception("pending finalize crashed for skeleton %s", skeleton_id)
        try:
            committed = database.commit_finalize_atomic(
                skeleton_id=skeleton_id,
                client_request_id=client_request_id,
                terminal_state="failed",
                result_memory_id="",
                skeleton_update={
                    "status": "failed",
                    "source_platform_suffix": "pipeline_error",
                },
                owner_token=owner_token,
            )
            if not committed:
                prior = database.get_ledger(skeleton_id)
                if prior:
                    _apply_ledger_to_skeleton(skeleton_id, prior)
        except Exception:
            logger.exception("failed to mark skeleton failed: %s", skeleton_id)


def _apply_ledger_to_skeleton(skeleton_id: str, ledger: dict) -> None:
    """Given a pre-committed ledger entry, reconcile the skeleton row to
    match. Used when sweep or a repeat finalize invocation sees an existing
    ledger — we trust it and apply without re-running the pipeline."""
    terminal = ledger["terminal_state"]
    result_id = ledger.get("result_memory_id", "")
    try:
        if terminal == "active":
            database.update_memory_status(skeleton_id, "active")
        elif terminal == "replaced":
            database.mark_replaced(skeleton_id, link_to_real_id=result_id)
        elif terminal == "failed":
            database.update_memory_status(
                skeleton_id, "failed",
                source_platform_suffix="pipeline_error")
        else:
            logger.warning("unknown ledger terminal_state %r for %s",
                           terminal, skeleton_id)
    except Exception:
        logger.exception(
            "failed to apply ledger %r to skeleton %s", terminal, skeleton_id)
