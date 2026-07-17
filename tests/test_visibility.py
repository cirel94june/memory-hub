"""
权限矩阵回归测试（对应 2026-07 Lucien MCP 审计的 P0#1 私有记忆串台）。

覆盖：
- can_view 矩阵：lucien/jasper/claude × shared / private-self / private-other
  × 别名（gpt→lucien、cloudy→claude、gemini→jasper）× owner 为空的 private
- smart_context 增量模式：最近动态 / 待办 / 其他伙伴动态不得出现他人 private
- smart_context 全量模式：走廊不得挤掉「与当前话题相关的记忆」（P0#2）

运行：ALLOW_DEFAULT_HUB_SECRET=1 python -m pytest tests/ -q
"""
import os
import sys
import asyncio
from datetime import datetime, timezone

os.environ.setdefault("ALLOW_DEFAULT_HUB_SECRET", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from visibility import can_view, filter_visible, viewer_ids


def _mem(id, layer="shared", owner="", source="", room="living_room",
         status="active", resolved=None, content=None, updated=None):
    return {
        "id": id,
        "content": content or f"content-of-{id}",
        "layer": layer,
        "owner_ai": owner,
        "source_ai": source,
        "room": room,
        "status": status,
        "resolved": resolved,
        "importance": 0.8,
        "updated_at": updated or datetime.now(timezone.utc).isoformat(),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


# ── can_view 矩阵 ──

def test_shared_visible_to_everyone():
    m = _mem("m1", layer="shared")
    for ai in ("lucien", "jasper", "claude", "gpt", "cloudy", ""):
        assert can_view(m, ai)


def test_missing_layer_treated_as_shared():
    m = _mem("m2")
    del m["layer"]
    assert can_view(m, "lucien")


def test_private_only_owner():
    m = _mem("m3", layer="private", owner="jasper", source="jasper")
    assert can_view(m, "jasper")
    assert not can_view(m, "lucien")
    assert not can_view(m, "claude")


def test_private_alias_mapping():
    """gpt 是 lucien 的别名、cloudy 是 claude 的别名——权限映射不能错。"""
    lucien_private = _mem("m4", layer="private", owner="lucien")
    assert can_view(lucien_private, "gpt")
    assert not can_view(lucien_private, "gemini")

    claude_private = _mem("m5", layer="private", owner="claude")
    assert can_view(claude_private, "cloudy")
    assert not can_view(claude_private, "gpt")

    # owner 记的是别名、请求者用 canonical，也要能对上
    aliased_owner = _mem("m6", layer="private", owner="gpt")
    assert can_view(aliased_owner, "lucien")


def test_private_ownerless_falls_back_to_source():
    m = _mem("m7", layer="private", owner="", source="jasper")
    assert can_view(m, "jasper")
    assert not can_view(m, "lucien")


def test_private_orphan_visible_to_nobody():
    m = _mem("m8", layer="private", owner="", source="")
    for ai in ("lucien", "jasper", "claude", ""):
        assert not can_view(m, ai)


def test_anonymous_viewer_sees_no_private():
    m = _mem("m9", layer="private", owner="lucien")
    assert not can_view(m, "")


def test_filter_visible_keeps_order():
    mems = [
        _mem("a", layer="shared"),
        _mem("b", layer="private", owner="lucien"),
        _mem("c", layer="private", owner="jasper"),
        _mem("d", layer="shared"),
    ]
    ids = [m["id"] for m in filter_visible(mems, "lucien")]
    assert ids == ["a", "b", "d"]


def test_viewer_ids_includes_aliases():
    ids = viewer_ids("gpt")
    assert "lucien" in ids and "gpt" in ids


# ── smart_context 泄漏复现/回归 ──

JASPER_DREAM = "jasper-private-dream-绝不能被lucien看到"
LUCIEN_DIARY = "lucien-own-private-diary"
SHARED_NEWS = "shared-recent-news"
JASPER_SHARED = "jasper-shared-activity"
JASPER_PRIVATE_TODO = "jasper-private-unresolved"


def _fake_store(monkeypatch):
    import github_store
    mems = {
        "d1": _mem("d1", layer="private", owner="jasper", source="jasper",
                   room="dreams", content=JASPER_DREAM),
        "d2": _mem("d2", layer="private", owner="lucien", source="lucien",
                   room="diary", content=LUCIEN_DIARY),
        "s1": _mem("s1", layer="shared", source="claude", content=SHARED_NEWS),
        "s2": _mem("s2", layer="shared", source="jasper", content=JASPER_SHARED),
        "t1": _mem("t1", layer="private", owner="jasper", source="jasper",
                   resolved=False, content=JASPER_PRIVATE_TODO),
    }
    monkeypatch.setattr(github_store, "get_all_memories", lambda: mems)
    return mems


@pytest.fixture
def smart_ctx(monkeypatch):
    import smart_context
    _fake_store(monkeypatch)
    # 隔离外部依赖：梦境区与召回不在本测试范围
    monkeypatch.setattr(smart_context, "_dream_section", lambda ai, md: "")
    return smart_context


def test_incremental_no_cross_private_leak(smart_ctx):
    """P0#1 复现：jasper 的 private 梦/待办绝不能进 lucien 的增量上下文。"""
    result = asyncio.run(smart_ctx.get_smart_context(
        ai_id="lucien", has_base_context=True, max_chars=4000))
    text = result["text"]
    assert JASPER_DREAM not in text
    assert JASPER_PRIVATE_TODO not in text
    # 自己的 private 和 shared 应该在
    assert LUCIEN_DIARY in text
    assert SHARED_NEWS in text


def test_incremental_cross_ai_section_shared_only(smart_ctx):
    """【其他伙伴动态】只允许 shared 层。"""
    result = asyncio.run(smart_ctx.get_smart_context(
        ai_id="lucien", has_base_context=True, max_chars=4000))
    text = result["text"]
    assert JASPER_SHARED in text
    assert JASPER_DREAM not in text


def test_incremental_owner_sees_own_private(smart_ctx):
    result = asyncio.run(smart_ctx.get_smart_context(
        ai_id="jasper", has_base_context=True, max_chars=4000))
    text = result["text"]
    assert JASPER_DREAM in text
    assert LUCIEN_DIARY not in text


def test_incremental_alias_viewer(smart_ctx):
    """用别名 gpt 请求，应等同 lucien。"""
    result = asyncio.run(smart_ctx.get_smart_context(
        ai_id="gpt", has_base_context=True, max_chars=4000))
    text = result["text"]
    assert LUCIEN_DIARY in text
    assert JASPER_DREAM not in text


# ── P0#2：全量模式预算 ──

def test_full_mode_reserves_recall_budget(smart_ctx, monkeypatch):
    """走廊超长时，「与当前话题相关的记忆」不能被挤掉。"""
    import corridor
    import memory_ops

    async def huge_corridor(ai_id, force=False):
        return "走廊固定内容。" * 2000  # 远超 max_chars

    async def fake_recall(query, ai_id="", top_k=5, **kw):
        return [_mem("r1", layer="shared", content="用户刚才要求继续排查权限矩阵")]

    monkeypatch.setattr(corridor, "get_corridor", huge_corridor)
    monkeypatch.setattr(memory_ops, "recall", fake_recall)

    result = asyncio.run(smart_ctx.get_smart_context(
        ai_id="lucien", user_message="继续刚才的任务",
        has_base_context=False, max_chars=3500))
    text = result["text"]
    assert "用户刚才要求继续排查权限矩阵" in text, "recall 区被走廊挤掉了"
    assert len(text) <= 3500 + 50


def test_fit_sections_drops_whole_sections(smart_ctx):
    fit = smart_ctx._fit_sections
    a, b, c = "A" * 100, "B" * 100, "C" * 100
    out = fit([a, b, c], 210)
    assert a in out and b in out and c not in out
    # 第一段就超限时截断保底
    out2 = fit([a], 50)
    assert out2.startswith("A" * 50)
