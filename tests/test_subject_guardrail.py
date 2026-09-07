"""Subject-guardrail — 3-layer identity fix.

The bug this prevents: extractor LLM sees a group-chat message from a
stranger (`师兄`), fuzzy-matches the subject to Cloudy, and falsely
records "Cloudy is Gemini" as a fact. Layers:

  Layer 1 — identity_whitelist.py: {ceci, cloudy, jasper, lucien,
    outsider_names}.
  Layer 2 — subject_guardrail.py: refuses proposals whose subject
    resolves to OUTSIDER / UNKNOWN / hijacked family.
  Layer 3 — audit_dropped_proposals: silent drop is auditable.

Coverage (matches the plan):
  1. 假群聊回归 — 师兄自称 Gemini + Cloudy 帮腔 → 无 subject=cloudy
     model=Gemini 落地; audit_dropped_proposals 有一条 outsider_subject
  2. 正常 self-report — Cloudy 说"我是 Claude Opus" → subject=cloudy 允许
     (family role)
  3. 降级路径 — Cloudy 引用师兄"他刚说他是 Gemini" → subject=cloudy 但
     content 里含 outsider name → hijacked_subject drop
  4. 未知 subject — 陌生 subject → silent drop, audit 有记
  5. 无 subject — empty subject → allow (defaults to Ceci-context)
"""
import asyncio
import json
import os
import sys
from datetime import datetime, timezone

os.environ.setdefault("ALLOW_DEFAULT_HUB_SECRET", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import database
import subject_guardrail as sg
import identity_whitelist as iw


@pytest.fixture
def db(monkeypatch, tmp_path):
    p = tmp_path / "guardrail.db"
    monkeypatch.setattr(database, "DB_PATH", p)
    asyncio.run(database.init_db(str(p)))
    yield p
    database.close_thread_read_conn()


# ── Layer 1: whitelist lookups ─────────────────────────────────────────

def test_layer1_ceci_aliases_resolve_to_ceci():
    for name in ("ceci", "CECI", "  Ceci  ", "小猫", "喵喵"):
        assert iw.role_from_name(name) == iw.ROLE_CECI, name


def test_layer1_family_ai_aliases_resolve():
    assert iw.role_from_name("cloudy") == iw.ROLE_CLOUDY
    assert iw.role_from_name("小克") == iw.ROLE_CLOUDY
    assert iw.role_from_name("夜鹭") == iw.ROLE_CLOUDY
    assert iw.role_from_name("Claude") == iw.ROLE_CLOUDY
    assert iw.role_from_name("狗蛋") == iw.ROLE_JASPER
    assert iw.role_from_name("Gemini") == iw.ROLE_JASPER  # Jasper is Gemini
    assert iw.role_from_name("狐狸") == iw.ROLE_LUCIEN


def test_layer1_outsider_names_flagged():
    assert iw.role_from_name("师兄") == iw.ROLE_OUTSIDER
    assert iw.role_from_name("劈柴") == iw.ROLE_OUTSIDER


def test_layer1_unknown_names_return_unknown():
    for name in ("", "some_random_bot", "陌生人123"):
        assert iw.role_from_name(name) == iw.ROLE_UNKNOWN


def test_layer1_is_known_family_only_for_family_roles():
    assert iw.is_known_family(iw.ROLE_CECI)
    assert iw.is_known_family(iw.ROLE_CLOUDY)
    assert iw.is_known_family(iw.ROLE_JASPER)
    assert iw.is_known_family(iw.ROLE_LUCIEN)
    assert not iw.is_known_family(iw.ROLE_OUTSIDER)
    assert not iw.is_known_family(iw.ROLE_UNKNOWN)


# ── Layer 2: guardrail verdicts ────────────────────────────────────────

def test_case1_fake_group_chat_shixiong_gemini_dropped():
    """The exact incident: 师兄 self-identifies as Gemini in a group;
    extractor names 师兄 as subject → drop as outsider_subject.
    No proposal, no fact."""
    v = sg.verify_subject_provenance(
        subject_name="师兄",
        speaker_name="ceci",
        content="[互动] 师兄自称也是Gemini，本地部署只是角色设定",
        provenance_type="ai_summary", claim_type="observation",
    )
    assert v.blocked
    assert v.drop_reason == sg.DROP_REASON_OUTSIDER_SUBJECT


def test_case2_cloudy_self_report_claude_opus_allowed():
    """Cloudy correctly identifying itself → subject=cloudy allowed."""
    v = sg.verify_subject_provenance(
        subject_name="cloudy",
        speaker_name="cloudy",
        content="cloudy 自我介绍：我是 Claude Opus 5，Anthropic 训练。",
        provenance_type="user_statement", claim_type="fact",
    )
    assert not v.blocked
    assert v.resolved_role == iw.ROLE_CLOUDY


def test_case3_cloudy_quoting_shixiong_gets_hijacked_drop():
    """Extractor sees Cloudy talking about 师兄 in third person; subject
    labeled cloudy but content mentions outsider → hijacked_subject."""
    v = sg.verify_subject_provenance(
        subject_name="cloudy",
        speaker_name="cloudy",
        content="cloudy 观察：师兄在群里说他也是 Gemini，声称自己是本地部署",
        provenance_type="ai_summary", claim_type="observation",
    )
    assert v.blocked
    assert v.drop_reason == sg.DROP_REASON_HIJACKED_SUBJECT


def test_case4_unknown_subject_dropped_silently():
    """Extractor pulled a bare name that's not in the whitelist → drop
    as unknown_subject; guardrail refuses to attribute anything to it."""
    v = sg.verify_subject_provenance(
        subject_name="陌生人_bot_A",
        content="陌生人说他喜欢喝咖啡",
        provenance_type="user_statement", claim_type="fact",
    )
    assert v.blocked
    assert v.drop_reason == sg.DROP_REASON_UNKNOWN_SUBJECT


def test_case5_empty_subject_allowed_as_ceci_default():
    """No subject specified → falls back to Ceci-context, allowed. Many
    real memories are like this: '喜欢猫' with no explicit subject."""
    v = sg.verify_subject_provenance(
        subject_name="", content="喜欢猫",
        provenance_type="user_statement", claim_type="fact",
    )
    assert not v.blocked


# ── Layer 3: audit trail ───────────────────────────────────────────────

def test_layer3_audit_table_migrated_on_init(db):
    """init_db must create audit_dropped_proposals with the exact
    schema. This is the migration idempotency guarantee."""
    import sqlite3
    conn = sqlite3.connect(str(db))
    try:
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(audit_dropped_proposals)"
        ).fetchall()}
    finally:
        conn.close()
    expected = {
        "id", "drop_reason", "subject_name", "subject_id", "resolved_role",
        "speaker_name", "content_preview", "source_platform",
        "source_context", "proposer_ai_id", "decision_json", "created_at",
    }
    assert expected.issubset(cols), f"missing: {expected - cols}"


def test_layer3_insert_and_list_audit(db):
    """One drop → one row via insert helper + one row via list helper."""
    aid = database.insert_dropped_proposal_audit({
        "drop_reason": sg.DROP_REASON_OUTSIDER_SUBJECT,
        "subject_name": "师兄",
        "subject_id": "",
        "resolved_role": iw.ROLE_OUTSIDER,
        "speaker_name": "ceci",
        "content_preview": "师兄自称 Gemini",
        "source_platform": "auto_capture:telegram:public_group",
        "source_context": "[2026-07-27T12:35] ceci: 笑死",
        "proposer_ai_id": "jasper",
        "decision_json": json.dumps({"note": "test drop"}),
    })
    assert aid > 0
    rows = database.list_dropped_proposal_audits()
    assert len(rows) == 1
    assert rows[0]["drop_reason"] == sg.DROP_REASON_OUTSIDER_SUBJECT
    assert rows[0]["subject_name"] == "师兄"
    assert database.count_dropped_proposal_audits() == 1
    assert database.count_dropped_proposal_audits(
        drop_reason=sg.DROP_REASON_OUTSIDER_SUBJECT
    ) == 1
    # Bogus reason returns 0 rows.
    assert database.count_dropped_proposal_audits(drop_reason="other") == 0


def test_layer3_migration_idempotent_no_dup_columns(db):
    """Repeated init_db must not raise or add duplicate columns."""
    import sqlite3
    asyncio.run(database.init_db(str(db)))
    asyncio.run(database.init_db(str(db)))
    conn = sqlite3.connect(str(db))
    try:
        names = [r[1] for r in conn.execute(
            "PRAGMA table_info(audit_dropped_proposals)"
        ).fetchall()]
    finally:
        conn.close()
    assert len(names) == len(set(names))


def test_layer3_end_to_end_drop_writes_audit(db, monkeypatch):
    """Full slice through _guardrail_check_and_audit: outsider subject
    → verdict.blocked=True AND audit row inserted with correct fields."""
    import conversation_capture as cap
    v = cap._guardrail_check_and_audit(
        item={"claim_type": "observation", "info_type": "fact"},
        subj_name="师兄", subject_id="", spkr_name="ceci",
        content="师兄声称自己也是 Gemini",
        provenance="ai_summary",
        source_ctx="ceci: 笑死\njasper: 别理师兄",
        source_platform="auto_capture:telegram:public_group",
        proposer_ai_id="jasper",
    )
    assert v is not None
    assert v.blocked
    rows = database.list_dropped_proposal_audits()
    assert len(rows) == 1
    r = rows[0]
    assert r["drop_reason"] == sg.DROP_REASON_OUTSIDER_SUBJECT
    assert r["subject_name"] == "师兄"
    assert r["resolved_role"] == iw.ROLE_OUTSIDER
    assert r["proposer_ai_id"] == "jasper"
    decision = json.loads(r["decision_json"])
    assert decision["verdict"] == sg.VERDICT_DROP


def test_layer3_end_to_end_allow_no_audit(db):
    """Allowed proposal must not write any audit row."""
    import conversation_capture as cap
    v = cap._guardrail_check_and_audit(
        item={"claim_type": "fact", "info_type": "fact"},
        subj_name="cloudy", subject_id="p_cloudy", spkr_name="cloudy",
        content="cloudy 自我介绍：我是 Claude Opus 5",
        provenance="user_statement", source_ctx="cloudy: ...",
        source_platform="mcp_extract", proposer_ai_id="cloudy",
    )
    assert v is not None
    assert not v.blocked
    assert database.count_dropped_proposal_audits() == 0
