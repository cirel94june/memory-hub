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
      - failed   → tell the caller so they can decide to retry manually
    """
    status = existing.get("status", "unknown")
    if status == "replaced":
        real_id = existing.get("link_to_real_id") or existing["id"]
    else:
        real_id = existing["id"]
    return json.dumps({
        "status": "active" if status == "replaced" else status,
        "memory_id": real_id,
        "client_request_id": existing.get("client_request_id", ""),
        "idempotent": True,
    }, ensure_ascii=False)


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
) -> None:
    """Background pipeline: run the injected sync impl and reconcile the
    skeleton row.

      - real_id == skeleton_id → skeleton was the actual write, mark active
      - real_id != skeleton_id → remember() merged/superseded; mark skeleton
        'replaced' and store link_to_real_id so idempotent lookups redirect
      - no id / exception → mark skeleton 'failed'

    impl_fn is injected (rather than hard-coded) so tests can pass a fake
    without importing mcp_server.
    """
    try:
        result = await impl_fn(
            content=content, room=room, category=category, importance=importance,
            source_ai=source_ai, event_date=event_date, force_create=force_create,
        )
        real_id = (result or {}).get("id", "")
        if real_id == skeleton_id:
            database.update_memory_status(skeleton_id, "active")
        elif real_id:
            # Skeleton is orphaned by merge/supersede — keep as tombstone
            # with redirect pointer, do NOT hard-delete (else idempotent
            # lookups for the same client_request_id would miss).
            database.mark_replaced(skeleton_id, link_to_real_id=real_id)
        else:
            # No id (gated/blocked/failed) — mark failed for observability
            database.update_memory_status(skeleton_id, "failed")
    except Exception:
        logger.exception("pending finalize crashed for skeleton %s", skeleton_id)
        try:
            database.update_memory_status(skeleton_id, "failed")
        except Exception:
            logger.exception("failed to mark skeleton failed: %s", skeleton_id)
