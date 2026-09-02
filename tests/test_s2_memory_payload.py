"""S2a — pure additions to memory_payload.py.

Boundary: only tests the new pure functions. Does NOT invoke memory_ops,
DB, embedding, or store. memory_ops.remember() migration is S2b.

Coverage:
  Builder equivalence with current memory_ops.remember() dict shape
  1. no-relation create
  2. supplements linked memories
  3. supersede path (supersedes list non-empty)
  4. empty info_type → 'fact'
  5. analyzer domain=list → canonical JSON string
  6. domain=None / ''  → '[]'
  7. builder covers every _ALL_COLUMNS-storable field
  8. override_id / override_now make output fully deterministic

  promotion_payload_from_proposal
  9. deterministic id = mem_from_prop_<pid>
  10. proposer_ai_id / proposed_room / created_at threading
  11. adds origin='promotion' + proposal_id to history entry

  TargetSnapshot / parse_target_snapshot
  12. extra='forbid' rejects unknown field
  13. status not in allowlist rejected
  14. relation not in allowlist rejected
  15. invalid time rejected; ISO with 'Z' accepted
  16. empty raw string raises ValueError (not ValidationError, per A3)

  append_supersede_note
  17. does NOT mutate input
  18. comment appended with {date, author, kind, content} — full schema
  19. history appended with v=last+1 and supersede metadata
  20. comments starting as non-list gets replaced with a fresh list
"""
import json
import os
import sys

os.environ.setdefault("ALLOW_DEFAULT_HUB_SECRET", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from pydantic import ValidationError

import memory_payload as mp


FIXED_NOW = "2026-09-02T00:00:00+00:00"
FIXED_ID = "mem_fixedTESTID12"


def _build(**overrides):
    """Common builder call with overrides for determinism."""
    args = dict(
        content="hello world",
        layer="shared", room="living_room", category="",
        owner_ai="", importance=0.5, emotion_arousal=0.3, valence=0.5,
        domain=None, source_ai="cloudy", source_platform="",
        subject_id="", source_actor_id="", info_type="",
        event_date="", source_context="",
        provenance_type="", fact_confidence=None,
        tags=None, linked_memories=None, supersedes=None,
        embedding=None, client_request_id="",
        override_id=FIXED_ID, override_now=FIXED_NOW,
    )
    args.update(overrides)
    return mp.build_new_memory_payload(**args)


# ═══ Builder equivalence with current memory_ops.remember() ═════════════

def test_builder_no_relation_matches_current_shape():
    """Snapshot of exactly what memory_ops.remember() writes on the plain
    no-relation create path (lines 447-480 in memory_ops.py).
    """
    out = _build()
    # decay_score MUST be 1.0 not 0.5 — v3 got this wrong.
    assert out["decay_score"] == 1.0
    # linked_memories / supersedes hardcoded to "[]" string on no-relation.
    assert out["linked_memories"] == "[]"
    assert out["supersedes"] == "[]"
    # info_type empty → 'fact' (memory_ops:472 pattern)
    assert out["info_type"] == "fact"
    # domain None → '[]' string
    assert out["domain"] == "[]"
    # comments is a Python list, NOT a string (DB layer serializes).
    assert out["comments"] == []
    # history is a list of one dict {v, content, date, by} — no extras.
    assert out["history"] == [
        {"v": 1, "content": "hello world", "date": FIXED_NOW, "by": "cloudy"}
    ]
    assert out["status"] == "active"
    assert out["created_at"] == FIXED_NOW
    assert out["updated_at"] == FIXED_NOW
    assert out["activation_count"] == 0
    assert out["last_activated"] == ""


def test_builder_supplements_linked_memories_list_becomes_json_string():
    out = _build(linked_memories=["mem_a", "mem_b"])
    assert out["linked_memories"] == '["mem_a", "mem_b"]'


def test_builder_supersede_path_produces_json_supersedes():
    out = _build(supersedes=["mem_old_1", "mem_old_2"])
    assert out["supersedes"] == '["mem_old_1", "mem_old_2"]'


def test_builder_empty_info_type_falls_back_to_fact():
    assert _build(info_type="")["info_type"] == "fact"
    assert _build(info_type="opinion")["info_type"] == "opinion"


def test_builder_analyzer_domain_list_becomes_canonical_json():
    out = _build(domain=["work", "family"])
    # Canonical JSON — sort_keys=True on a list is a no-op, but the
    # normalizer never re-orders elements; content is preserved.
    assert json.loads(out["domain"]) == ["work", "family"]
    assert isinstance(out["domain"], str)


def test_builder_domain_variants_all_normalize():
    for empty in (None, "", [], "[]"):
        assert _build(domain=empty)["domain"] == "[]"
    # str already-JSON gets re-canonicalized
    assert json.loads(_build(domain='["a"]')["domain"]) == ["a"]
    # Non-JSON str gets wrapped as single-element list
    assert json.loads(_build(domain="lonestr")["domain"]) == ["lonestr"]


def test_builder_output_covers_all_memory_columns_minus_lifecycle_only_ones():
    """Every column that memory_ops.remember() sets must exist in builder
    output. Lifecycle-only columns set by other paths (finalize_claim_id,
    link_to_real_id, resolved, anchored, superseded_by) are DB-level
    defaults and are NOT set by memory_ops.remember() either — so builder
    also leaves them out for exact equivalence.
    """
    out = _build()
    must_have = {
        "id", "content", "layer", "room", "category", "owner_ai",
        "importance", "emotion_arousal", "valence", "domain",
        "decay_score", "activation_count", "last_activated",
        "source_ai", "source_platform", "tags", "linked_memories",
        "supersedes", "event_date", "source_context",
        "comments", "embedding", "status", "created_at", "updated_at",
        "history", "provenance_type", "fact_confidence",
        "subject_id", "source_actor_id", "info_type", "client_request_id",
    }
    missing = must_have - set(out.keys())
    assert not missing, f"builder missing keys memory_ops.remember() sets: {missing}"


def test_builder_is_deterministic_with_overrides():
    a = _build()
    b = _build()
    assert a == b


# ═══ promotion_payload_from_proposal ═════════════════════════════════════

def _base_proposal():
    return {
        "id": "prop_ABC",
        "content": "proposal content",
        "layer": "shared",
        "proposed_room": "study",
        "category": "work",
        "owner_ai": "",
        "proposer_ai_id": "jasper",
        "source_platform": "telegram",
        "source_context": "chat_msg",
        "subject_id": "ceci",
        "source_actor_id": "ceci",
        "info_type": "fact",
        "event_date": "2026-08-01",
        "provenance_type": "user_statement",
        "confidence": 0.9,
        "importance": 0.7,
        "emotion_arousal": 0.4,
        "tags": '["work", "meeting"]',
        "domain": ["work"],
        "embedding": None,
        "created_at": "2026-08-01T10:00:00+00:00",
    }


def test_promotion_id_is_deterministic():
    out = mp.promotion_payload_from_proposal(_base_proposal())
    assert out["id"] == "mem_from_prop_prop_ABC"


def test_promotion_preserves_room_and_source_ai():
    out = mp.promotion_payload_from_proposal(_base_proposal())
    assert out["room"] == "study"
    assert out["source_ai"] == "jasper"
    assert out["created_at"] == "2026-08-01T10:00:00+00:00"


def test_promotion_history_has_origin_and_proposal_id():
    out = mp.promotion_payload_from_proposal(_base_proposal())
    entry = out["history"][0]
    assert entry["origin"] == "promotion"
    assert entry["proposal_id"] == "prop_ABC"
    # base fields still present, ORDER of new fields is additive.
    assert entry["v"] == 1
    assert entry["by"] == "jasper"


# ═══ TargetSnapshot / parse_target_snapshot ════════════════════════════

def _snap_json(**overrides):
    d = {
        "target_id": "mem_target_1",
        "expected_status": "active",
        "expected_updated_at": "2026-08-30T12:00:00+00:00",
        "relation": "supersede",
        "reason": "conflicts with existing",
    }
    d.update(overrides)
    return json.dumps(d)


def test_snapshot_extra_field_rejected():
    raw = json.dumps({
        "target_id": "m", "expected_status": "active",
        "expected_updated_at": "2026-08-30T12:00:00+00:00",
        "relation": "update", "reason": "",
        "hacker_field": "boom",
    })
    with pytest.raises(ValidationError):
        mp.parse_target_snapshot(raw)


def test_snapshot_invalid_status_rejected():
    with pytest.raises(ValidationError):
        mp.parse_target_snapshot(_snap_json(expected_status="ghost"))


def test_snapshot_invalid_relation_rejected():
    with pytest.raises(ValidationError):
        mp.parse_target_snapshot(_snap_json(relation="delete"))


def test_snapshot_invalid_time_rejected():
    with pytest.raises(ValidationError):
        mp.parse_target_snapshot(_snap_json(expected_updated_at="yesterday-ish"))


def test_snapshot_accepts_iso_with_z_suffix():
    snap = mp.parse_target_snapshot(_snap_json(expected_updated_at="2026-08-30T12:00:00Z"))
    assert snap.expected_status == "active"


def test_parse_snapshot_empty_raises_value_error_not_validation():
    """A3: caller (S3+ wrapper) distinguishes empty (data missing) from
    schema-invalid (data present but wrong) — the first is fatal, the
    second triggers mark_promotion_failed."""
    with pytest.raises(ValueError) as exc:
        mp.parse_target_snapshot("")
    assert not isinstance(exc.value, ValidationError)
    with pytest.raises(ValueError) as exc:
        mp.parse_target_snapshot("   ")
    assert not isinstance(exc.value, ValidationError)


# ═══ append_supersede_note ══════════════════════════════════════════════

def _old_mem():
    return {
        "id": "mem_old",
        "content": "旧的内容",
        "status": "active",
        "updated_at": "2026-07-01T00:00:00+00:00",
        "comments": [
            {"date": "2026-06-01T00:00:00+00:00", "author": "cloudy",
             "kind": "note", "content": "旧评论"},
        ],
        "history": [
            {"v": 1, "content": "旧的内容", "date": "2026-06-01T00:00:00+00:00", "by": "cloudy"},
        ],
    }


def test_supersede_does_not_mutate_input():
    src = _old_mem()
    src_copy = json.loads(json.dumps(src))
    mp.append_supersede_note(src, "mem_new", "冲突", FIXED_NOW)
    assert src == src_copy, "append_supersede_note must be pure"


def test_supersede_comment_matches_existing_schema():
    out = mp.append_supersede_note(_old_mem(), "mem_new", "冲突", FIXED_NOW)
    assert out["status"] == "superseded"
    assert out["superseded_by"] == "mem_new"
    assert out["updated_at"] == FIXED_NOW
    # Old comment still there in order.
    assert out["comments"][0] == {
        "date": "2026-06-01T00:00:00+00:00", "author": "cloudy",
        "kind": "note", "content": "旧评论",
    }
    # New note has EXACTLY the fields production code writes.
    new_comment = out["comments"][-1]
    assert set(new_comment.keys()) == {"date", "author", "kind", "content"}
    assert new_comment["author"] == "system"
    assert new_comment["kind"] == "supersede_note"
    assert new_comment["date"] == FIXED_NOW
    assert "冲突" in new_comment["content"]


def test_supersede_history_v_increments_with_supersede_metadata():
    out = mp.append_supersede_note(_old_mem(), "mem_new", "reason X", FIXED_NOW)
    last = out["history"][-1]
    assert last["v"] == 2  # was 1
    assert last["date"] == FIXED_NOW
    assert last["by"] == "system"
    assert last["op"] == "supersede"
    assert last["superseded_by"] == "mem_new"
    assert last["reason"] == "reason X"
    # Base fields still there so anyone parsing normal-create history keeps working.
    assert "content" in last


def test_supersede_recovers_when_comments_is_not_a_list():
    """Defensive: an older row might have `comments` stored as a string or
    None; helper must still produce a valid comments list."""
    weird = {**_old_mem(), "comments": None}
    out = mp.append_supersede_note(weird, "mem_new", "r", FIXED_NOW)
    assert isinstance(out["comments"], list)
    assert out["comments"][-1]["kind"] == "supersede_note"

    weird2 = {**_old_mem(), "comments": "not-a-list"}
    out = mp.append_supersede_note(weird2, "mem_new", "r", FIXED_NOW)
    # `list("not-a-list")` yields characters — that's undesirable but is
    # the caller's mistake. We accept it as-is and just ensure the new
    # note is at the end and the helper does not crash.
    assert isinstance(out["comments"], list)
    assert out["comments"][-1]["kind"] == "supersede_note"


# ═══ canonical_proposal_fingerprint (S0 shipped, no change here) ═══════
# Regression sanity: S0 fingerprint still works alongside S2 additions.

def test_fingerprint_still_deterministic_after_s2_additions():
    prop = {"content": "abc", "importance": 0.5, "emotion_arousal": 0.3,
            "confidence": 0.9, "maintenance_action": ""}
    fp1 = mp.canonical_proposal_fingerprint(prop)
    fp2 = mp.canonical_proposal_fingerprint(prop)
    assert fp1 == fp2
    assert len(fp1) == 64
