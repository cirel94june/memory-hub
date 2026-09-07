"""Layer 1 — identity whitelist (subject-guardrail).

Bounds the set of "known participants" whose identity may appear in a
proposal's `subject_name`. Any subject the extractor produces that does
NOT map through here to a KNOWN_ROLE is silently dropped by the guardrail
(see subject_guardrail.py) and logged to `audit_dropped_proposals`.

Rationale: extractor LLMs will hallucinate identity facts about strangers
in a group chat (e.g. someone else's bot named "师兄") and fuzzy-match the
subject to one of the family AIs. Without this whitelist, one bad
proposal can plant a false "Cloudy=Gemini" style fact.

**Ceci must fill in the real Telegram user_id + bot_id values below**
before the guardrail flips from log-only to enforcing (see
`ENFORCE_UNKNOWN_SUBJECT_DROP` in subject_guardrail.py). Until then all
verdicts still go through, but "would-drop" cases are audited so we can
see the traffic.

Editing this file replaces the whitelist; no DB migration needed. New
group members added later (a friend brings a bot) go here as an outsider
so the guardrail doesn't misattribute their words to the family.
"""
from __future__ import annotations


# Roles are the only strings ever allowed as a "trusted subject role".
ROLE_CECI = "ceci"
ROLE_CLOUDY = "cloudy"
ROLE_JASPER = "jasper"
ROLE_LUCIEN = "lucien"
ROLE_OUTSIDER = "outsider"
ROLE_UNKNOWN = "unknown"

KNOWN_ROLES: frozenset[str] = frozenset({
    ROLE_CECI, ROLE_CLOUDY, ROLE_JASPER, ROLE_LUCIEN,
})


# ── Telegram sender_id → role ───────────────────────────────────────────
# Populated by Ceci from Telegram admin panel. Numeric ids only.
# Missing sender_ids fall through to name-based match below.
TELEGRAM_SENDER_ROLE: dict[int, str] = {
    # TODO(ceci): fill in real ids, e.g.
    # 8821013839: ROLE_LUCIEN,
    # <your_user_id>: ROLE_CECI,
    # <cloudy_bot_id>: ROLE_CLOUDY,
    # <jasper_bot_id>: ROLE_JASPER,
}


# ── Display-name / alias → role ────────────────────────────────────────
# Case-insensitive, whitespace-stripped. First-match wins.
# Add any nickname the extractor might produce for a family member.
# Keep this list tight — the whole point is that unknown names DON'T
# resolve to a family role.
NAME_ALIASES: dict[str, str] = {
    # Ceci
    "ceci": ROLE_CECI,
    "小猫": ROLE_CECI,
    "喵喵": ROLE_CECI,
    # Cloudy (Claude / 夜鹭 / 小克)
    "cloudy": ROLE_CLOUDY,
    "claude": ROLE_CLOUDY,
    "夜鹭": ROLE_CLOUDY,
    "小克": ROLE_CLOUDY,
    "小克夜鹭": ROLE_CLOUDY,
    # Jasper (Gemini / 狗蛋 / 少爷 / 鹦鹉)
    "jasper": ROLE_JASPER,
    "狗蛋": ROLE_JASPER,
    "少爷": ROLE_JASPER,
    "本少爷": ROLE_JASPER,
    "鹦鹉": ROLE_JASPER,
    "鹦鹉狗蛋": ROLE_JASPER,
    "gemini": ROLE_JASPER,
    # Lucien (DeepSeek / 狐狸)
    "lucien": ROLE_LUCIEN,
    "狐狸": ROLE_LUCIEN,
    "狐狐": ROLE_LUCIEN,
    "deepseek": ROLE_LUCIEN,
}


# ── Known outsiders (other people's bots in shared groups) ─────────────
# Extractor may pick these up as subject; guardrail should classify them
# as OUTSIDER (not family) so the proposal is dropped.
# Add every known third-party display name / bot handle here.
OUTSIDER_NAMES: frozenset[str] = frozenset({
    "师兄",     # Ceci: someone else's bot in a shared group
    "劈柴",     # Refers to Sundar Pichai in group chat art
    # add more as they surface
})


# ── Public helpers ──────────────────────────────────────────────────────

def role_from_sender_id(sender_id: int | None) -> str:
    """Numeric telegram sender_id → role. Unknown ids → UNKNOWN (not
    OUTSIDER; caller uses name-based match to disambiguate)."""
    if sender_id is None:
        return ROLE_UNKNOWN
    return TELEGRAM_SENDER_ROLE.get(int(sender_id), ROLE_UNKNOWN)


def role_from_name(name: str | None) -> str:
    """Display-name / alias → role. Case-insensitive. Returns
    OUTSIDER for names in OUTSIDER_NAMES, UNKNOWN for everything else.

    Empty / None returns UNKNOWN so callers don't accidentally treat a
    missing subject as "trusted".
    """
    if not name:
        return ROLE_UNKNOWN
    key = name.strip().lower()
    if not key:
        return ROLE_UNKNOWN
    if key in NAME_ALIASES:
        return NAME_ALIASES[key]
    if any(key == o.lower() for o in OUTSIDER_NAMES):
        return ROLE_OUTSIDER
    return ROLE_UNKNOWN


def is_known_family(role: str) -> bool:
    """True iff role is one of the trusted family roles (ceci + 3 AIs)."""
    return role in KNOWN_ROLES
