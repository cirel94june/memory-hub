"""Pure memory payload helpers, shared by database.py and memory_ops.py.

No DB dependency. S0 shipped fingerprint + tag normalizer for the audit
report. S2 adds the full pure builder, snapshot parser, and supersede-note
helper — used by the promotion path in S3+; S2b will migrate the normal
create path in memory_ops.remember() to call the same builder for drift-
free equivalence.

Golden contract: any drift between report-time and adopt-time on the 21
canonical fields makes CLI adoption refuse the plan.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, field_validator


CANONICAL_FIELDS: tuple[str, ...] = (
    "content",
    "layer",
    "proposed_room",
    "category",
    "owner_ai",
    "proposer_ai_id",
    "source_platform",
    "source_context",
    "subject_id",
    "source_actor_id",
    "info_type",
    "tags",
    "importance",
    "emotion_arousal",
    "provenance_type",
    "confidence",
    "event_date",
    "maintenance_action",
    "created_at",
    "source_message_ids",
    "triage_reason",
    "claim_type",
    "speech_mode",
    "conversation_kind",
)


def _normalize_tags(v) -> list[str]:
    if v in ("", None):
        return []
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
        except json.JSONDecodeError:
            return [v]
        if isinstance(parsed, list):
            return sorted(str(x) for x in parsed)
        return [str(parsed)]
    if isinstance(v, list):
        return sorted(str(x) for x in v)
    return [str(v)]


def canonical_proposal_fingerprint(
    proposal: dict, target_snapshot: dict | None = None
) -> str:
    """SHA-256 over the canonical serialization of a proposal.

    Covers all fields that influence the final memory row, the audit trail,
    or the human approval context. For maintenance proposals a target
    snapshot MUST be provided (raises ValueError otherwise; not assert
    since `python -O` would drop it).
    """
    canonical: dict = {}
    for k in CANONICAL_FIELDS:
        v = proposal.get(k, "")
        if k == "tags":
            v = _normalize_tags(v)
        elif k in ("importance", "emotion_arousal", "confidence"):
            v = round(float(v), 6) if v not in ("", None) else None
        canonical[k] = v

    ma = (proposal.get("maintenance_action") or "").strip()
    if ma in ("update", "supersede"):
        if target_snapshot is None:
            raise ValueError(
                f"maintenance proposal (ma={ma!r}) requires target_snapshot for fingerprint"
            )
        canonical["_target_snapshot"] = {
            "target_id": target_snapshot["target_id"],
            "expected_status": target_snapshot["expected_status"],
            "expected_updated_at": target_snapshot["expected_updated_at"],
            "relation": target_snapshot["relation"],
            "reason": target_snapshot.get("reason", ""),
        }

    payload = json.dumps(
        canonical, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ═══════════════════════════════════════════════════════════════════════
# S2: pure memory payload builder (used by promotion path in S3+;
# memory_ops.remember() migration deferred to S2b)
# ═══════════════════════════════════════════════════════════════════════


def _normalize_domain(v) -> str:
    """analyzer 输出 list；DB 存 canonical JSON 字符串。

    None / '' / [] / '[]' 全部规范化到 '[]'。其他 list 走 canonical JSON
    (sort_keys)；已经是 str 的先尝试解析再重新 canonical dump；无法解析
    的字符串包装为单元素 list。
    """
    if v in (None, "", [], "[]"):
        return "[]"
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
        except json.JSONDecodeError:
            return json.dumps([v], ensure_ascii=False)
        return json.dumps(parsed, ensure_ascii=False, sort_keys=True)
    return json.dumps(v, ensure_ascii=False, sort_keys=True)


def _normalize_json_list(v) -> str:
    """Return a canonical JSON-list string for tags / linked_memories /
    supersedes fields. Fail-closed for anything that is not a real list.

    Accepted inputs:
      * None / '' / []  → '[]'
      * list            → json.dumps(list)
      * str '[...]'     → parsed, must yield list, then re-dumped canonical
    Rejected (raises ValueError):
      * strings that are not JSON, or JSON that is not a list
        (e.g. 'mem_a', 'null', '{"x":1}', '"mem_a"')
      * numbers, dicts, other objects
    """
    if v in (None, "", []):
        return "[]"
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"_normalize_json_list expected JSON list string, got {v!r}: {e}"
            ) from e
        if not isinstance(parsed, list):
            raise ValueError(
                f"_normalize_json_list expected list, got {type(parsed).__name__}: {v!r}"
            )
        return json.dumps(parsed, ensure_ascii=False)
    if isinstance(v, list):
        return json.dumps(v, ensure_ascii=False)
    raise ValueError(
        f"_normalize_json_list expected list-like, got {type(v).__name__}: {v!r}"
    )


def build_new_memory_payload(
    *,
    # required
    content: str,
    # taxonomy
    layer: str = "shared",
    room: str = "living_room",
    category: str = "",
    owner_ai: str = "",
    # scoring
    importance: float = 0.5,
    emotion_arousal: float = 0.3,
    valence: float = 0.5,
    domain=None,
    # provenance
    source_ai: str = "",
    source_platform: str = "",
    subject_id: str = "",
    source_actor_id: str = "",
    info_type: str = "",
    event_date: str = "",
    source_context: str = "",
    provenance_type: str = "",
    fact_confidence: float | None = None,
    # tags & relations
    tags=None,
    linked_memories=None,
    supersedes=None,
    # embedding (opaque bytes; None ok)
    embedding=None,
    # async / idempotency
    client_request_id: str = "",
    # injection hooks — pass override_id / override_now for full determinism
    # in tests, or leave unset to get uuid + now() at construction time.
    override_id: str | None = None,
    override_now: str | None = None,
    # provenance of THIS record's creation. 'normal_create' → history entry
    # matches memory_ops.remember() exactly ({v, content, date, by}).
    # Anything else (e.g. 'promotion', 'legacy_adoption') appends `origin`
    # and optional `proposal_id` to the SAME entry so downstream readers of
    # normal-create history see zero shape drift.
    origin: str = "normal_create",
    proposal_id: str = "",
) -> dict:
    """Pure. No DB, no store, no clock (if override_now given), no RNG (if
    override_id given). Produces exactly the dict shape that
    memory_ops.remember() currently writes on the create path.

    Contract equivalence (matched to memory_ops.remember lines 447-480 and
    386-419): comments is `[]` list (DB layer serializes), history is a
    list of dicts, linked_memories/supersedes/tags come in as either str
    or list and go to canonical JSON string.
    """
    now = override_now or datetime.now(timezone.utc).isoformat()
    mem_id = override_id or _default_gen_id()
    resolved_info_type = info_type or "fact"  # matches `info_type or "fact"` at memory_ops:472

    history_entry: dict = {
        "v": 1, "content": content, "date": now, "by": source_ai or "system",
    }
    if origin != "normal_create":
        history_entry["origin"] = origin
        if proposal_id:
            history_entry["proposal_id"] = proposal_id

    return {
        # identity
        "id": mem_id,
        "content": content,
        # taxonomy
        "layer": layer,
        "room": room,
        "category": category,
        "owner_ai": owner_ai,
        # scoring — decay_score=1.0 matches memory_ops:397/458
        "importance": float(importance),
        "emotion_arousal": float(emotion_arousal),
        "valence": float(valence),
        "domain": _normalize_domain(domain),
        "decay_score": 1.0,
        "activation_count": 0,
        "last_activated": "",
        # provenance
        "source_ai": source_ai,
        "source_platform": source_platform,
        "tags": _normalize_json_list(tags),
        "linked_memories": _normalize_json_list(linked_memories),
        "supersedes": _normalize_json_list(supersedes),
        "event_date": event_date,
        "source_context": source_context,
        # payload — list, not string (DB layer serializes)
        "comments": [],
        "embedding": embedding,
        # lifecycle
        "status": "active",
        "created_at": now,
        "updated_at": now,
        "history": [history_entry],
        # provenance quality
        "provenance_type": provenance_type,
        "fact_confidence": fact_confidence,
        # subject/actor
        "subject_id": subject_id,
        "source_actor_id": source_actor_id,
        "info_type": resolved_info_type,
        # async
        "client_request_id": client_request_id,
    }


def _default_gen_id() -> str:
    """Fallback id generator. memory_ops has its own _gen_id() which the
    remember-path migration (S2b) will inject here via override_id so we
    keep ID semantics identical."""
    return f"mem_{uuid.uuid4().hex[:12]}"


def _safe_float(v, default: float) -> float:
    """Proposal rows come from insert_proposal which stores empty strings
    for unset numeric fields. Coerce to float with fallback so builder
    remains callable on real DB rows without exploding."""
    if v in ("", None):
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def promotion_payload_from_proposal(
    proposal: dict,
    *,
    origin: str = "promotion",
    supersedes=None,
    linked_memories=None,
    override_now: str | None = None,
) -> dict:
    """Build a memory payload from a proposal row. Used by S3 promotion
    kernel and S6 adopt-legacy. Deterministic id = `mem_from_prop_<pid>`
    so recovery can idempotently retry without creating dups.
    """
    confidence = proposal.get("confidence")
    return build_new_memory_payload(
        content=proposal["content"],
        layer=proposal.get("layer") or "shared",
        room=proposal.get("proposed_room") or "living_room",
        category=proposal.get("category", ""),
        owner_ai=proposal.get("owner_ai", ""),
        source_ai=proposal.get("proposer_ai_id", ""),
        source_platform=proposal.get("source_platform", ""),
        subject_id=proposal.get("subject_id", ""),
        source_actor_id=proposal.get("source_actor_id", ""),
        info_type=proposal.get("info_type", ""),
        event_date=proposal.get("event_date", ""),
        source_context=proposal.get("source_context", ""),
        provenance_type=proposal.get("provenance_type", ""),
        fact_confidence=(_safe_float(confidence, 0.5)
                         if confidence not in ("", None) else None),
        importance=_safe_float(proposal.get("importance"), 0.5),
        emotion_arousal=_safe_float(proposal.get("emotion_arousal"), 0.3),
        tags=proposal.get("tags"),
        domain=proposal.get("domain"),
        embedding=proposal.get("embedding"),
        linked_memories=linked_memories,
        supersedes=supersedes,
        override_id=f"mem_from_prop_{proposal['id']}",
        override_now=proposal.get("created_at") or override_now,
        origin=origin,
        proposal_id=proposal["id"],
    )


# ═══════════════════════════════════════════════════════════════════════
# TargetSnapshot — maintenance drift-gate persistence contract (v5.1 A2)
# ═══════════════════════════════════════════════════════════════════════

_ALLOWED_SNAPSHOT_STATUS = {"active", "archived", "superseded"}
_ALLOWED_SNAPSHOT_RELATION = {"update", "supersede"}


class TargetSnapshot(BaseModel):
    """Persisted per maintenance proposal so restart-recovery can rebuild
    the drift gate byte-identically. `extra='forbid'` — unknown fields
    are rejected loudly so a schema change never silently ignores data.
    """
    model_config = ConfigDict(extra="forbid")
    target_id: str
    expected_status: str
    expected_updated_at: str
    relation: str
    reason: str = ""

    @field_validator("target_id")
    @classmethod
    def _target_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("target_id must be non-empty")
        return v

    @field_validator("expected_status")
    @classmethod
    def _status_allowed(cls, v: str) -> str:
        if v not in _ALLOWED_SNAPSHOT_STATUS:
            raise ValueError(
                f"expected_status={v!r} not in {sorted(_ALLOWED_SNAPSHOT_STATUS)}"
            )
        return v

    @field_validator("expected_updated_at")
    @classmethod
    def _time_parses(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("expected_updated_at must be non-empty")
        # Require an explicit time component. A date-only string like
        # "2026-01-01" would otherwise silently parse to midnight, letting
        # a caller pass a value that could match many different snapshots.
        if "T" not in v and " " not in v:
            raise ValueError(
                f"expected_updated_at needs time component (T or space separator): {v!r}"
            )
        # Accept a SINGLE trailing 'Z' as UTC. Reject 'ZZ' etc. — those
        # indicate a caller bug, not a legitimate ISO 8601 variant.
        if v.endswith("ZZ"):
            raise ValueError(f"expected_updated_at has stacked Z suffix: {v!r}")
        if v.endswith("Z"):
            candidate = v[:-1] + "+00:00"
        elif "+" not in v[10:] and "-" not in v[10:]:
            candidate = v + "+00:00"
        else:
            candidate = v
        try:
            datetime.fromisoformat(candidate)
        except ValueError as e:
            raise ValueError(f"expected_updated_at not ISO 8601: {v!r} ({e})") from e
        return v

    @field_validator("relation")
    @classmethod
    def _relation_allowed(cls, v: str) -> str:
        if v not in _ALLOWED_SNAPSHOT_RELATION:
            raise ValueError(
                f"relation={v!r} not in {sorted(_ALLOWED_SNAPSHOT_RELATION)}"
            )
        return v


def parse_target_snapshot(raw: str) -> TargetSnapshot:
    """Parse a target_snapshot_json blob into a validated model.

    Callers (S3+ wrappers) MUST run this OUTSIDE the promotion write
    transaction — a ValidationError here needs to trigger
    mark_promotion_failed in its own transaction (see v5.1 A3).
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raise ValueError("target_snapshot_json is empty; cannot parse")
    return TargetSnapshot.model_validate_json(raw)


# ═══════════════════════════════════════════════════════════════════════
# append_supersede_note — preserve existing comment schema exactly
# (Codex v5 round M3: comment shape stays {date, author, kind, content};
# history gets a NEW entry with optional supersede metadata layered on
# top of the existing {v, content, date, by} shape.)
# ═══════════════════════════════════════════════════════════════════════


def append_supersede_note(
    old_mem: dict,
    new_mem_id: str,
    reason: str,
    now: str,
    action: str = "supersede",
    author: str = "system",
) -> dict:
    """Pure: return a NEW dict for `old_mem` with supersede metadata
    layered on top. Does not mutate the input.

    Fields updated:
      - status         → 'superseded'
      - superseded_by  → new_mem_id
      - updated_at     → now
      - comments       → += {date, author, kind:'supersede_note', content}
      - history        → += {v: last_v+1, content, date, by, op:'supersede',
                              superseded_by, reason}

    Comment shape matches existing production code (memory_ops.py:902-906,
    2212-2217). History entry is a v5.1 addition — earlier code did not
    write history on supersede; the new fields (op / superseded_by /
    reason) are layered on top of the {v, content, date, by} base so
    readers of normal-create history see zero schema breakage.
    """
    updated = dict(old_mem)
    updated["status"] = "superseded"
    updated["superseded_by"] = new_mem_id
    updated["updated_at"] = now

    # Match memory_ops.py production defense: `if not isinstance(x, list): x = []`.
    # Using `list(x)` on a string would split into characters, silently
    # corrupting the payload S3 maintenance promotion writes back to disk.
    raw_comments = updated.get("comments")
    comments = list(raw_comments) if isinstance(raw_comments, list) else []
    comments.append({
        "date": now,
        "author": author,
        "kind": "supersede_note",
        "content": f"被新记忆取代（{action}）: {reason}",
    })
    updated["comments"] = comments

    raw_history = updated.get("history")
    history = list(raw_history) if isinstance(raw_history, list) else []
    last_v = max((int(h.get("v", 0)) for h in history if isinstance(h, dict)), default=0)
    history.append({
        "v": last_v + 1,
        "content": updated.get("content", ""),
        "date": now,
        "by": author,
        "op": "supersede",
        "superseded_by": new_mem_id,
        "reason": reason,
    })
    updated["history"] = history
    return updated
