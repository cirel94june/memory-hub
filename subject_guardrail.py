"""Layer 2 — subject-provenance guardrail.

Runs after the extractor produces a candidate memory but before it
becomes a pending proposal / active memory. Refuses any candidate whose
`subject_name` cannot be resolved to a KNOWN_FAMILY role
(`ceci` / `cloudy` / `jasper` / `lucien`) via `identity_whitelist`.

This closes the bug where the extractor conflated `师兄` (someone else's
bot in a shared group) with `cloudy`, producing a false `Cloudy=Gemini`
fact. With the guardrail: subject_name="师兄" → resolves to OUTSIDER →
verdict is `drop`, logged to `audit_dropped_proposals`, never enters
`pending`.

The guardrail is intentionally NARROW:
  * Only checks the subject field (not the whole content).
  * Only drops when subject is set to an outsider or unknown identity.
  * Missing/empty subject is allowed through — many memories don't have
    a specific subject and default to Ceci-context.
  * The result is a plain dataclass so callers can log without side
    effects.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

import identity_whitelist as iw


VERDICT_ALLOW = "allow"
VERDICT_DROP = "drop"
VERDICT_DOWNGRADE = "downgrade"

DROP_REASON_OUTSIDER_SUBJECT = "outsider_subject"
DROP_REASON_UNKNOWN_SUBJECT = "unknown_subject"
DROP_REASON_HIJACKED_SUBJECT = "hijacked_subject"

# Kill-switch: while False, guardrail logs "would-drop" cases to audit
# but STILL lets the proposal through. Flip to True after Ceci finishes
# populating identity_whitelist so we know unknowns are real strangers.
ENFORCE_UNKNOWN_SUBJECT_DROP = True


@dataclass
class GuardrailVerdict:
    verdict: str          # 'allow' | 'drop' | 'downgrade'
    drop_reason: str = ""  # non-empty iff verdict != 'allow'
    resolved_role: str = ""
    note: str = ""

    @property
    def blocked(self) -> bool:
        return self.verdict == VERDICT_DROP


# Third-person markers that signal the extractor is quoting/observing
# someone else, not stating a first-person identity fact.
_THIRD_PERSON_MARKERS = (
    "他说", "她说", "他自称", "她自称", "自称", "自诩",
    "认为", "指出", "夸", "说 ",
)


def _content_mentions_outsider(content: str) -> Optional[str]:
    """Return the outsider name if `content` mentions any registered
    outsider, else None. Case-insensitive substring match — the whitelist
    is short by design so this stays cheap."""
    if not content:
        return None
    lower = content.lower()
    for out in iw.OUTSIDER_NAMES:
        if out.lower() in lower:
            return out
    return None


def verify_subject_provenance(
    *,
    subject_name: str = "",
    speaker_name: str = "",
    content: str = "",
    provenance_type: str = "",
    claim_type: str = "",
) -> GuardrailVerdict:
    """Decide whether a candidate memory may proceed to `insert_proposal`.

    The extractor already produced:
      - subject_name: who the memory is *about*
      - speaker_name: who said/observed it
      - content: the memory sentence itself
      - provenance_type / claim_type: extractor's own confidence tags

    Rules (fail-closed for identity claims):
      1. subject_name resolves to OUTSIDER → drop.
      2. subject_name resolves to UNKNOWN (present but not a family or
         outsider) → drop.
      3. subject_name resolves to a family role BUT content mentions an
         outsider by name AND the extractor tagged it observation/
         hypothesis → drop as `hijacked_subject` (extractor probably
         conflated the outsider with the family member — this is the
         exact bug pattern from the 师兄=Cloudy incident).
      4. Empty subject → allow (defaults to Ceci-context memory).
      5. Family subject with clean content → allow.

    `ENFORCE_UNKNOWN_SUBJECT_DROP=False` degrades rule 2 to "would-drop"
    (still returns `drop` verdict; caller decides to enforce or just
    audit). Rule 1 (outsider) and rule 3 (hijack) are always enforced.
    """
    subj_role = iw.role_from_name(subject_name)

    # Rule 1: subject_name explicitly resolves to a known outsider.
    if subj_role == iw.ROLE_OUTSIDER:
        return GuardrailVerdict(
            verdict=VERDICT_DROP,
            drop_reason=DROP_REASON_OUTSIDER_SUBJECT,
            resolved_role=iw.ROLE_OUTSIDER,
            note=f"subject_name={subject_name!r} matches OUTSIDER_NAMES",
        )

    # Rule 4: empty subject → default Ceci-context, allow.
    if not subject_name or not subject_name.strip():
        return GuardrailVerdict(
            verdict=VERDICT_ALLOW, resolved_role="",
            note="empty subject_name; defaults to Ceci-context",
        )

    # Rule 2: subject is non-empty but doesn't resolve to family.
    if subj_role == iw.ROLE_UNKNOWN:
        if not ENFORCE_UNKNOWN_SUBJECT_DROP:
            return GuardrailVerdict(
                verdict=VERDICT_ALLOW,
                resolved_role=iw.ROLE_UNKNOWN,
                note=(f"subject_name={subject_name!r} unresolved; "
                      "would-drop but enforcement disabled"),
            )
        return GuardrailVerdict(
            verdict=VERDICT_DROP,
            drop_reason=DROP_REASON_UNKNOWN_SUBJECT,
            resolved_role=iw.ROLE_UNKNOWN,
            note=f"subject_name={subject_name!r} not in whitelist",
        )

    # Rule 3: subject resolves to family but content name-drops an
    # outsider — likely the exact hijack the guardrail is here to catch.
    outsider_hit = _content_mentions_outsider(content)
    if outsider_hit and iw.is_known_family(subj_role):
        # Bias toward drop only when the extractor already flagged the
        # claim as observation/hypothesis — pure user_statement + literal
        # from Ceci calling out her own family AI shouldn't get caught.
        soft_provenance = (
            provenance_type in ("ai_summary", "ai_speculation", "roleplay_meme", "")
            or claim_type in ("observation", "hypothesis")
        )
        if soft_provenance:
            return GuardrailVerdict(
                verdict=VERDICT_DROP,
                drop_reason=DROP_REASON_HIJACKED_SUBJECT,
                resolved_role=subj_role,
                note=(f"subject={subj_role} but content name-drops "
                      f"outsider {outsider_hit!r}; extractor likely hijacked"),
            )

    return GuardrailVerdict(
        verdict=VERDICT_ALLOW, resolved_role=subj_role,
        note=f"subject_name={subject_name!r} resolved to family role {subj_role}",
    )
