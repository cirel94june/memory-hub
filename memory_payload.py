"""Pure memory payload helpers, shared by database.py and memory_ops.py.

No DB dependency. In S0 only the fingerprint + normalizers ship (used by the
audit report); S2 grows this module with the full `build_new_memory_payload`
and `promotion_payload_from_proposal`, then S3-S6 add TargetSnapshot and
the CLI adoption helpers.

The 21 canonical fields listed here are the fingerprint contract: any drift
between report time and adopt time on any of them will make CLI adoption
refuse the plan.
"""
from __future__ import annotations

import hashlib
import json


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
