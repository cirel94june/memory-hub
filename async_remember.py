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
import json
import logging
from typing import Callable, Awaitable

import database

logger = logging.getLogger("memory_hub.async_remember")


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
    try:
        result = await impl_fn(
            content=content, room=room, category=category, importance=importance,
            source_ai=source_ai, event_date=event_date, force_create=force_create,
            existing_id=skeleton_id,
            client_request_id=client_request_id,
        )
        real_id = (result or {}).get("id", "")
        if real_id == skeleton_id:
            # Common case: skeleton became the real memory in-place. remember()
            # already wrote status='active', but call update to be explicit
            # (also idempotent).
            database.update_memory_status(skeleton_id, "active")
        elif real_id:
            # Merge path: content matched an existing memory. Skeleton is
            # orphaned — keep as tombstone with redirect pointer so
            # idempotent lookups for the same crq don't miss.
            database.mark_replaced(skeleton_id, link_to_real_id=real_id)
        else:
            # No id returned. write_gate only runs on quick=True; MCP always
            # calls memory_ops.remember with quick=False, so we should never
            # see a "gated" status here in practice. Any missing id from the
            # MCP path is treated as a pipeline error (unknown side effects).
            database.update_memory_status(
                skeleton_id, "failed",
                source_platform_suffix="pipeline_error")
    except Exception:
        # Exception mid-pipeline: partial state possible → NOT retry_safe.
        logger.exception("pending finalize crashed for skeleton %s", skeleton_id)
        try:
            database.update_memory_status(
                skeleton_id, "failed",
                source_platform_suffix="pipeline_error")
        except Exception:
            logger.exception("failed to mark skeleton failed: %s", skeleton_id)
