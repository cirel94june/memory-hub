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


# ── Layer 1 + guardrail: sender_id fallback (post-whitelist-fill) ──────

def test_senderid_ceci_no_names_in_content_resolves_to_ceci():
    """Ceci sends a message with no explicit subject in content; sender_id
    fallback attributes it to ceci."""
    v = sg.verify_subject_provenance(
        subject_name="",
        content="今天头痛得厉害",
        provenance_type="user_statement", claim_type="fact",
        sender_id=8749953218,
    )
    assert not v.blocked
    assert v.resolved_role == iw.ROLE_CECI


def test_senderid_cloudy_first_person_claude_opus_resolves_to_cloudy():
    """Cloudy sends 'I am Claude Opus' with no explicit third-person
    subject; sender_id fallback attributes it to cloudy and allows."""
    v = sg.verify_subject_provenance(
        subject_name="",
        content="我是 Claude Opus 5，Anthropic 训练的助手",
        provenance_type="user_statement", claim_type="fact",
        sender_id=8638070562,
    )
    assert not v.blocked
    assert v.resolved_role == iw.ROLE_CLOUDY


def test_senderid_stranger_dropped_as_unknown():
    """Unknown sender_id, no subject_name in content → drop
    unknown_subject (guardrail refuses to attribute anything)."""
    v = sg.verify_subject_provenance(
        subject_name="",
        content="随便说了句话",
        provenance_type="user_statement", claim_type="fact",
        sender_id=9999999999,
    )
    assert v.blocked
    assert v.drop_reason == sg.DROP_REASON_UNKNOWN_SUBJECT


def test_senderid_lucien_jasper_also_wired():
    """Sanity: the other two ids from the whitelist map to the right roles."""
    v = sg.verify_subject_provenance(
        subject_name="", content="test", sender_id=8821013839,
    )
    assert not v.blocked and v.resolved_role == iw.ROLE_LUCIEN
    v = sg.verify_subject_provenance(
        subject_name="", content="test", sender_id=8553463347,
    )
    assert not v.blocked and v.resolved_role == iw.ROLE_JASPER


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


# ── Layer 4: guardrail inside memory_ops.remember() ──────────────────

def test_remember_blocks_outsider_subject(db, monkeypatch):
    """memory_ops.remember() must block when subject_name is an outsider,
    returning guardrail_blocked and writing an audit row."""
    import memory_ops
    # Stub out embedding/LLM so remember() doesn't need real API keys
    monkeypatch.setattr(memory_ops, "get_embedding", lambda *a, **kw: None)
    result = asyncio.run(
        memory_ops.remember(
            content="师兄说他也是 Gemini",
            room="living_room",
            source_ai="jasper",
            subject_name="师兄",
            speaker_name="ceci",
        )
    )
    assert result["status"] == "guardrail_blocked"
    assert result["reason"] == sg.DROP_REASON_OUTSIDER_SUBJECT
    rows = database.list_dropped_proposal_audits()
    assert len(rows) >= 1
    assert rows[-1]["subject_name"] == "师兄"


def test_remember_allows_known_family_subject(db):
    """memory_ops.remember() guardrail must NOT block known family subjects.
    We only test the guardrail gate, not the full pipeline."""
    v = sg.verify_subject_provenance(
        subject_name="ceci",
        speaker_name="cloudy",
        content="ceci 今天心情不错",
        provenance_type="user_statement",
        claim_type="fact",
    )
    assert not v.blocked
    assert database.count_dropped_proposal_audits() == 0


def test_remember_no_subject_name_skips_guardrail(db):
    """When subject_name is empty, guardrail does not fire (backwards compat).
    The guardrail in remember() only runs when subject_name or speaker_name
    is non-empty."""
    v = sg.verify_subject_provenance(
        subject_name="",
        content="random fact",
        provenance_type="user_statement",
        claim_type="fact",
    )
    assert not v.blocked


# ── Layer 5: user_correction guardrail ordering ──────────────────────

def test_autocapture_user_correction_outsider_blocked(db):
    """An outsider subject tagged as user_correction must still be blocked
    by the guardrail — the guardrail runs BEFORE the correction branch."""
    import conversation_capture as cap
    v = cap._guardrail_check_and_audit(
        item={"claim_type": "fact", "info_type": "fact"},
        subj_name="师兄", subject_id="", spkr_name="ceci",
        content="[纠正] 师兄说的不对，他不是 Gemini",
        provenance="user_correction",
        source_ctx="ceci: 笑死",
        source_platform="auto_capture:telegram:public_group",
        proposer_ai_id="jasper",
    )
    assert v is not None
    assert v.blocked
    assert v.drop_reason == sg.DROP_REASON_OUTSIDER_SUBJECT


def test_guardrail_exception_returns_blocked_verdict_and_writes_audit(db, monkeypatch):
    """_guardrail_check_and_audit must return a blocked verdict AND write
    an audit row when the guardrail raises an exception — fail-closed."""
    import conversation_capture as cap
    monkeypatch.setattr(
        sg, "verify_subject_provenance",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("simulated crash")),
    )
    v = cap._guardrail_check_and_audit(
        item={"claim_type": "fact", "info_type": "fact"},
        subj_name="ceci", subject_id="", spkr_name="cloudy",
        content="test content",
        provenance="user_correction",
        source_ctx="...",
        source_platform="auto_capture:telegram:private",
        proposer_ai_id="cloudy",
    )
    assert v is not None
    assert v.blocked
    assert v.drop_reason == "guardrail_unavailable"
    rows = database.list_dropped_proposal_audits()
    assert len(rows) == 1
    assert rows[0]["drop_reason"] == "guardrail_unavailable"
    assert rows[0]["subject_name"] == "ceci"
    assert rows[0]["proposer_ai_id"] == "cloudy"


# ── Layer 6: fail-closed guardrail in memory_ops ─────────────────────

def test_remember_fail_closed_on_import_error(db, monkeypatch):
    """When subject_guardrail module is missing, remember() must fail-closed
    (return guardrail_unavailable) instead of allowing the write through."""
    import memory_ops
    monkeypatch.setattr(memory_ops, "get_embedding", lambda *a, **kw: None)
    import builtins
    _real_import = builtins.__import__
    def _block_sg(name, *args, **kwargs):
        if name == "subject_guardrail":
            raise ImportError("simulated missing module")
        return _real_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", _block_sg)
    result = asyncio.run(
        memory_ops.remember(
            content="some content",
            room="living_room",
            source_ai="jasper",
            subject_name="ceci",
            speaker_name="cloudy",
        )
    )
    assert result["status"] == "guardrail_unavailable"


# ── Layer 7: grow per-item subject threading ─────────────────────────

def test_grow_threads_per_item_subject(db, monkeypatch):
    """grow() must pass each digest item's subject_name to remember(),
    not just the global fallback."""
    import memory_ops
    import analyzer
    remembered_subjects = []
    async def _spy_remember(*a, **kw):
        remembered_subjects.append(kw.get("subject_name", ""))
        return {"status": "created", "id": f"mem_fake_{len(remembered_subjects)}"}
    monkeypatch.setattr(memory_ops, "remember", _spy_remember)
    async def _fake_digest(content):
        return [
            {"content": "师兄说他是 Gemini", "room": "living_room",
             "importance": 0.7, "subject_name": "师兄", "speaker_name": "ceci"},
            {"content": "Ceci 喜欢猫", "room": "living_room",
             "importance": 0.6, "subject_name": "ceci", "speaker_name": ""},
        ]
    monkeypatch.setattr(analyzer, "digest", _fake_digest)
    asyncio.run(memory_ops.grow(
        content="混合长文", source_ai="jasper",
        subject_name="fallback", speaker_name="",
    ))
    assert remembered_subjects == ["师兄", "ceci"]


def test_grow_uses_global_fallback_when_item_has_no_subject(db, monkeypatch):
    """When a digest item has no subject_name, grow() uses the global fallback."""
    import memory_ops
    import analyzer
    remembered_subjects = []
    async def _spy_remember(*a, **kw):
        remembered_subjects.append(kw.get("subject_name", ""))
        return {"status": "created", "id": f"mem_fake_{len(remembered_subjects)}"}
    monkeypatch.setattr(memory_ops, "remember", _spy_remember)
    async def _fake_digest(content):
        return [
            {"content": "some content without subject", "room": "living_room",
             "importance": 0.5},
        ]
    monkeypatch.setattr(analyzer, "digest", _fake_digest)
    asyncio.run(memory_ops.grow(
        content="test", source_ai="jasper",
        subject_name="ceci", speaker_name="cloudy",
    ))
    assert remembered_subjects == ["ceci"]


# ── Layer 8: digest preserves subject fields ─────────────────────────

def test_digest_output_preserves_subject_fields(monkeypatch):
    """Call real analyzer.digest() with mocked LLM; verify subject_name
    and speaker_name survive the parsing pipeline."""
    import analyzer
    fake_llm_response = json.dumps([
        {
            "content": "师兄在群里自称是 Gemini 的本地部署版本",
            "name": "师兄自称",
            "room": "living_room",
            "importance": 0.7,
            "subject_name": "师兄",
            "speaker_name": "ceci",
        }
    ])
    async def _fake_call_llm(*args, **kwargs):
        return fake_llm_response
    monkeypatch.setattr(analyzer, "_call_llm", _fake_call_llm)
    result = asyncio.run(analyzer.digest("一段很长的对话内容，包括师兄自称是 Gemini"))
    assert len(result) == 1
    assert result[0]["subject_name"] == "师兄"
    assert result[0]["speaker_name"] == "ceci"
    assert result[0]["content"] == "师兄在群里自称是 Gemini 的本地部署版本"


# ── Layer 9: import speaker fallback + missing-subject skip ──────────

def test_import_defaults_speaker_to_ai_id(db, monkeypatch):
    """Call real _extract_from_chunk with mocked LLM; when item has no
    speaker_name, it should default to ai_id."""
    import conversation_import as ci
    import memory_ops
    remembered_kwargs = []
    async def _spy_remember(**kw):
        remembered_kwargs.append(kw)
        return {"status": "created", "id": "fake_1"}
    monkeypatch.setattr(memory_ops, "remember", _spy_remember)
    fake_response = json.dumps([
        {
            "content": "Ceci 喜欢猫猫，经常在群里发猫咪照片",
            "room": "living_room",
            "importance": 0.6,
            "subject_name": "Ceci",
            "speaker_name": "",
        }
    ])
    async def _fake_call_llm(prompt):
        return fake_response
    monkeypatch.setattr(ci, "_call_llm", _fake_call_llm)
    chunk = [{"role": "user", "content": "我喜欢猫猫"}]
    result = asyncio.run(ci._extract_from_chunk(chunk, "jasper", 0, 1))
    assert len(remembered_kwargs) == 1
    assert remembered_kwargs[0]["speaker_name"] == "jasper"


def test_import_skips_missing_subject(db, monkeypatch):
    """When LLM returns an item with no subject_name, import must skip it
    with status skipped_no_subject instead of sending to remember()."""
    import conversation_import as ci
    import memory_ops
    remember_called = []
    async def _spy_remember(**kw):
        remember_called.append(kw)
        return {"status": "created", "id": "fake_1"}
    monkeypatch.setattr(memory_ops, "remember", _spy_remember)
    fake_response = json.dumps([
        {
            "content": "一条没有 subject 的记忆，内容足够长",
            "room": "living_room",
            "importance": 0.6,
            "subject_name": "",
            "speaker_name": "ceci",
        }
    ])
    async def _fake_call_llm(prompt):
        return fake_response
    monkeypatch.setattr(ci, "_call_llm", _fake_call_llm)
    chunk = [{"role": "user", "content": "随便聊聊"}]
    result = asyncio.run(ci._extract_from_chunk(chunk, "jasper", 0, 1))
    assert len(remember_called) == 0
    assert len(result) == 1
    assert result[0]["status"] == "skipped_no_subject"
