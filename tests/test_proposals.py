"""
MemoryProposal 候选区回归测试。

覆盖：
- _triage_proposal 分流规则
- quick=True 路径创建 proposal 而非直接写入
- fact + literal 自动通过并晋升为正式记忆
- 冲突/敏感房间/非 literal 阻止自动通过
- review_proposal 批准/拒绝
- quick=False 不走 proposal

运行：ALLOW_DEFAULT_HUB_SECRET=1 python -m pytest tests/ -q
"""
import os
import sys
import json
import asyncio
from datetime import datetime, timezone

os.environ.setdefault("ALLOW_DEFAULT_HUB_SECRET", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import database
import memory_ops
from memory_ops import _triage_proposal, _provenance_to_claim_type, _provenance_to_speech_mode


# ── provenance 映射 ──

def test_provenance_to_claim_type():
    assert _provenance_to_claim_type("user_statement") == "fact"
    assert _provenance_to_claim_type("user_correction") == "fact"
    assert _provenance_to_claim_type("user_quote") == "observation"
    assert _provenance_to_claim_type("ai_summary") == "observation"
    assert _provenance_to_claim_type("ai_speculation") == "hypothesis"
    assert _provenance_to_claim_type("roleplay_meme") == "observation"
    assert _provenance_to_claim_type("") == "observation"


def test_provenance_to_speech_mode():
    assert _provenance_to_speech_mode("user_statement") == "literal"
    assert _provenance_to_speech_mode("user_quote") == "uncertain"
    assert _provenance_to_speech_mode("ai_summary") == "literal"
    assert _provenance_to_speech_mode("ai_speculation") == "uncertain"
    assert _provenance_to_speech_mode("roleplay_meme") == "playful"
    assert _provenance_to_speech_mode("") == "uncertain"


# ── triage 分流规则 ──

def _prop(**overrides):
    base = {
        "claim_type": "fact",
        "speech_mode": "literal",
        "proposed_room": "living_room",
        "conversation_kind": "house_chat",
        "conflicts_with": "[]",
    }
    base.update(overrides)
    return base


def test_triage_fact_literal_auto_approves():
    assert _triage_proposal(_prop()) == "auto_approve"


def test_triage_conflicts_block():
    p = _prop(conflicts_with=json.dumps(["mem_123"]))
    assert _triage_proposal(p) == "conflicts_with_existing"


def test_triage_sensitive_room_blocks():
    assert _triage_proposal(_prop(proposed_room="health")) == "sensitive_room"
    assert _triage_proposal(_prop(proposed_room="psychology")) == "sensitive_room"


def test_triage_game_content_blocks():
    assert _triage_proposal(_prop(conversation_kind="game_world")) == "game_content"
    assert _triage_proposal(_prop(conversation_kind="game_discussion")) == "game_content"


def test_triage_non_literal_blocks():
    assert _triage_proposal(_prop(speech_mode="playful")) == "playful_speech_mode"
    assert _triage_proposal(_prop(speech_mode="hypothetical")) == "hypothetical_speech_mode"
    assert _triage_proposal(_prop(speech_mode="fictional")) == "fictional_speech_mode"
    assert _triage_proposal(_prop(speech_mode="uncertain")) == "uncertain_speech_mode"


def test_triage_observation_blocks():
    assert _triage_proposal(_prop(claim_type="observation")) == "observation_claim"


def test_triage_hypothesis_blocks():
    assert _triage_proposal(_prop(claim_type="hypothesis")) == "hypothesis_claim"


# ── 端到端场景（fake store + fake embedding）──

@pytest.fixture
def fake_env(monkeypatch, tmp_path):
    """内存版 store + 短路 embedding/analyzer，真正走 remember() 路径。"""
    mems = {}

    # 初始化 SQLite 用临时路径
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.db")
    asyncio.run(database.init_db(db_path))

    # 短路 github_store
    import github_store
    monkeypatch.setattr(github_store, "get_all_memories", lambda: mems)
    monkeypatch.setattr(github_store, "get_memory", lambda mid: mems.get(mid))

    _original_set = github_store.set_memory
    def fake_set(m):
        mems[m["id"]] = m
        # 也写入 SQLite 让 vector_search 能找到
        database.set_memory(m)
    monkeypatch.setattr(github_store, "set_memory", fake_set)
    monkeypatch.setattr(memory_ops, "store", github_store)

    # 短路 embedding（返回固定向量，避免网络调用）
    async def fake_embedding(text):
        import hashlib
        h = hashlib.md5(text.encode()).hexdigest()
        vec = [int(c, 16) / 15.0 for c in h] * 64
        return vec[:1024]

    monkeypatch.setattr(memory_ops, "get_embedding", fake_embedding)
    monkeypatch.setattr("memory_ops.pack_embedding", lambda v: b"\x00" * 4096 if v else None)

    # 短路 analyzer
    async def fake_analyze(text):
        return {"tags": ["test"], "suggested_category": "test", "domain": ["general"], "valence": 0.5, "arousal": 0.3}
    monkeypatch.setattr(memory_ops.analyzer, "analyze", fake_analyze)

    return mems


def test_quick_ai_summary_creates_pending_proposal(fake_env):
    result = asyncio.run(memory_ops.remember(
        content="ceci喜欢看动漫",
        quick=True,
        provenance_type="ai_summary",
        source_ai="claude",
    ))
    assert result["status"] == "proposed"
    assert result["proposal_status"] == "pending"
    assert result["id"].startswith("prop_")
    # 不应该在正式库中
    assert not any(m.get("content") == "ceci喜欢看动漫" for m in fake_env.values())


def test_quick_user_statement_auto_approves(fake_env):
    result = asyncio.run(memory_ops.remember(
        content="我今天搬到新公寓了",
        quick=True,
        provenance_type="user_statement",
        source_ai="claude",
    ))
    assert result["status"] == "created"
    assert result.get("proposal_status") == "auto_approved"
    # 应该在正式库中
    assert any("搬到新公寓" in m.get("content", "") for m in fake_env.values())


def test_quick_roleplay_meme_goes_pending(fake_env):
    result = asyncio.run(memory_ops.remember(
        content="ceci自称是大蟑螂女王",
        quick=True,
        provenance_type="roleplay_meme",
        source_ai="jasper",
    ))
    assert result["status"] == "proposed"
    assert "playful" in result.get("triage_reason", "")


def test_quick_false_bypasses_proposals(fake_env):
    result = asyncio.run(memory_ops.remember(
        content="这是手动写入的记忆",
        quick=False,
        provenance_type="ai_summary",
        source_ai="claude",
        auto_merge=False,
    ))
    assert result["status"] == "created"
    assert result["id"].startswith("mem_")


def test_sensitive_room_blocks_even_fact(fake_env):
    result = asyncio.run(memory_ops.remember(
        content="ceci最近焦虑比较严重",
        quick=True,
        provenance_type="user_statement",
        room="psychology",
        source_ai="claude",
    ))
    assert result["status"] == "proposed"
    assert result["triage_reason"] == "sensitive_room"


def test_review_approve_promotes(fake_env):
    # 先创建一个 pending proposal
    result = asyncio.run(memory_ops.remember(
        content="ceci可能要换工作",
        quick=True,
        provenance_type="ai_speculation",
        source_ai="lucien",
    ))
    assert result["status"] == "proposed"
    prop_id = result["id"]

    # 批准
    review = asyncio.run(memory_ops.review_proposal(prop_id, "approve", reviewed_by="user"))
    assert review.get("status") == "created"
    assert review.get("proposal_status") == "approved"
    mem_id = review["id"]
    assert mem_id.startswith("mem_")
    assert any("换工作" in m.get("content", "") for m in fake_env.values())


def test_review_reject(fake_env):
    result = asyncio.run(memory_ops.remember(
        content="ceci可能不太喜欢养猫，她从来没提过想养猫",
        quick=True,
        provenance_type="ai_speculation",
        source_ai="lucien",
    ))
    prop_id = result["id"]

    review = asyncio.run(memory_ops.review_proposal(prop_id, "reject", reject_reason="不准确"))
    assert review["status"] == "rejected"
    # 不应该在正式库中
    assert not any("不太喜欢养猫" in m.get("content", "") for m in fake_env.values())


def test_user_quote_goes_pending(fake_env):
    """user_quote 不应自动通过——防止"狗蛋说的话被记成用户说的"。"""
    result = asyncio.run(memory_ops.remember(
        content="狗蛋说他最近在学日语，每天背五十个单词",
        quick=True,
        provenance_type="user_quote",
        source_ai="jasper",
    ))
    assert result["status"] == "proposed"
    assert result["proposal_status"] == "pending"


def test_explicit_claim_type_overrides_provenance(fake_env):
    result = asyncio.run(memory_ops.remember(
        content="ceci说她在开玩笑",
        quick=True,
        provenance_type="user_statement",
        claim_type="observation",
        speech_mode="playful",
        source_ai="claude",
    ))
    assert result["status"] == "proposed"
    assert "playful" in result.get("triage_reason", "")
