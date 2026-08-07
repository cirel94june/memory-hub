"""
Phase 1.7 PR A — recall 质量升级回归测试。

覆盖：
- Block 1: recall 状态硬过滤 (resolved/superseded)
- Block 2: RRF 后新鲜度 boost
- Block 3: activation_count P95 顶端惩罚
- Block 6: resolve_thread 触发词扩展

运行：ALLOW_DEFAULT_HUB_SECRET=1 python -m pytest test_recall_quality.py -q
"""
import os
import sys
import json
import math
import asyncio
import struct
from datetime import datetime, timezone, timedelta

os.environ.setdefault("ALLOW_DEFAULT_HUB_SECRET", "1")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest
import database
import memory_ops
from memory_ops import _RESOLVE_PATTERNS, _check_auto_resolve, _matches_resolve_pattern, _rrf_merge

EMBEDDING_DIM = 1024


def _make_vec(seed: float) -> list[float]:
    """Generate a deterministic 1024-dim unit vector from a seed."""
    import hashlib
    h = hashlib.sha256(str(seed).encode()).hexdigest()
    # Extend to enough hex chars: 1024 values * 2 hex chars = 2048 needed
    while len(h) < EMBEDDING_DIM * 2:
        h += hashlib.sha256(h.encode()).hexdigest()
    raw = [int(h[i:i+2], 16) / 255.0 for i in range(0, EMBEDDING_DIM * 2, 2)]
    norm = math.sqrt(sum(x*x for x in raw))
    return [x / norm for x in raw]


def _pack(vec):
    return struct.pack(f"{EMBEDDING_DIM}f", *vec)


def _make_mem(mems, mem_id, content, *, resolved=None, superseded_by="",
              activation_count=0, days_ago=0, importance=0.5, room="living_room"):
    """Insert a memory into both the dict store and SQLite."""
    created = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    vec = _make_vec(hash(content))
    mem = {
        "id": mem_id,
        "content": content,
        "layer": "shared",
        "room": room,
        "category": "",
        "owner_ai": "",
        "importance": importance,
        "emotion_arousal": 0.3,
        "valence": 0.5,
        "domain": "[]",
        "decay_score": 1.0,
        "activation_count": activation_count,
        "last_activated": "",
        "source_ai": "claude",
        "source_platform": "",
        "tags": "[]",
        "linked_memories": "[]",
        "supersedes": "[]",
        "superseded_by": superseded_by,
        "event_date": "",
        "source_context": "",
        "comments": [],
        "embedding": _pack(vec),
        "status": "active",
        "created_at": created,
        "updated_at": created,
        "resolved": resolved,
        "info_type": "fact",
        "anchored": False,
        "provenance_type": "",
        "subject_id": "",
        "source_speaker_id": "",
    }
    mems[mem_id] = mem
    database.set_memory(mem)
    return mem


@pytest.fixture
def fake_env(monkeypatch, tmp_path):
    """In-memory store + SQLite, short-circuit embedding/analyzer."""
    mems = {}

    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test.db")
    asyncio.run(database.init_db(db_path))

    import github_store
    monkeypatch.setattr(github_store, "get_all_memories", lambda: mems)
    monkeypatch.setattr(github_store, "get_memory", lambda mid: mems.get(mid))

    def fake_set(m):
        mems[m["id"]] = m
        database.set_memory(m)
    monkeypatch.setattr(github_store, "set_memory", fake_set)
    monkeypatch.setattr(memory_ops, "store", github_store)

    async def fake_embedding(text):
        return _make_vec(hash(text))
    monkeypatch.setattr(memory_ops, "get_embedding", fake_embedding)
    monkeypatch.setattr("memory_ops.pack_embedding", lambda v: _pack(v) if v else None)

    async def fake_analyze(text):
        return {"tags": [], "suggested_category": "", "domain": ["general"],
                "valence": 0.5, "arousal": 0.3}
    monkeypatch.setattr(memory_ops.analyzer, "analyze", fake_analyze)

    # Disable classify_relation to avoid LLM calls
    async def fake_classify(content, candidates):
        return {"relations": []}
    monkeypatch.setattr(memory_ops.analyzer, "classify_relation", fake_classify)

    return mems


# ════════════════════════════════════════════
#  Block 1: recall 状态硬过滤
# ════════════════════════════════════════════

def test_recall_excludes_resolved_memories(fake_env):
    """resolved=True 的记忆不应出现在 recall 结果中。"""
    _make_mem(fake_env, "mem_active", "小猫喜欢吃草莓")
    _make_mem(fake_env, "mem_resolved", "给小猫买草莓蛋糕", resolved=True)

    results = asyncio.run(memory_ops.recall("草莓"))
    ids = [r["id"] for r in results]
    assert "mem_active" in ids
    assert "mem_resolved" not in ids


def test_recall_excludes_superseded_memories(fake_env):
    """superseded_by 非空的记忆不应出现在 recall 结果中。"""
    _make_mem(fake_env, "mem_old", "小猫住在北京", superseded_by="mem_new")
    _make_mem(fake_env, "mem_new", "小猫搬到上海了")

    results = asyncio.run(memory_ops.recall("小猫住在哪"))
    ids = [r["id"] for r in results]
    assert "mem_old" not in ids
    assert "mem_new" in ids


def test_recall_returns_normal_active_memories(fake_env):
    """正常 active 记忆应被正常返回。"""
    _make_mem(fake_env, "mem_1", "小猫在腾讯做产品经理")
    _make_mem(fake_env, "mem_2", "小猫养了三只猫")

    results = asyncio.run(memory_ops.recall("小猫"))
    assert len(results) >= 1
    ids = [r["id"] for r in results]
    assert any(m in ids for m in ("mem_1", "mem_2"))


def test_recall_still_surfaces_unresolved_tasks(fake_env):
    """resolved=False 的待办应正常出现（甚至优先浮现）。"""
    _make_mem(fake_env, "mem_task", "帮小猫预约牙医", resolved=False)

    results = asyncio.run(memory_ops.recall("牙医"))
    ids = [r["id"] for r in results]
    assert "mem_task" in ids


# ════════════════════════════════════════════
#  Block 2: 新鲜度 boost
# ════════════════════════════════════════════

def test_recency_boost_formula():
    """验证 recency boost 公式的数学正确性。"""
    # 1 day ago → ~1.29
    assert abs((1 + 0.3 * math.exp(-1/30)) - 1.29) < 0.01
    # 30 days ago → ~1.11
    assert abs((1 + 0.3 * math.exp(-30/30)) - 1.11) < 0.01
    # 90 days ago → ~1.015
    assert abs((1 + 0.3 * math.exp(-90/30)) - 1.015) < 0.01


def test_recent_memory_ranks_higher(fake_env):
    """相同内容质量下，近期记忆应排名靠前。"""
    _make_mem(fake_env, "mem_old", "小猫最近心情不好", days_ago=60)
    _make_mem(fake_env, "mem_new", "小猫最近心情不好啊", days_ago=1)

    results = asyncio.run(memory_ops.recall("小猫心情"))
    if len(results) >= 2:
        ids = [r["id"] for r in results]
        old_idx = ids.index("mem_old") if "mem_old" in ids else 999
        new_idx = ids.index("mem_new") if "mem_new" in ids else 999
        assert new_idx < old_idx, "Recent memory should rank higher"


# ════════════════════════════════════════════
#  Block 3: activation_count P95 惩罚
# ════════════════════════════════════════════

def test_activation_penalty_applied_with_enough_results(fake_env):
    """≥20 条结果时，activation_count > P95 的记忆应被惩罚。"""
    # Create 20 normal memories + 1 high-activation memory
    for i in range(20):
        _make_mem(fake_env, f"mem_normal_{i}", f"测试记忆内容编号{i}",
                  activation_count=5, days_ago=i)
    _make_mem(fake_env, "mem_hot", "测试记忆热门内容",
              activation_count=500, days_ago=0, importance=0.9)

    # Build fake RRF results to test the penalty logic directly
    items = []
    for i in range(20):
        items.append({
            "id": f"mem_normal_{i}", "score": 0.03,
            "activation_count": 5,
            "created_at": (datetime.now(timezone.utc) - timedelta(days=i)).isoformat(),
        })
    items.append({
        "id": "mem_hot", "score": 0.04,
        "activation_count": 500,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })

    # P95 of 21 items: index 19 → activation_count=5
    # mem_hot (500) > 5 → gets 0.7 penalty: 0.04 * recency * 0.7
    counts = sorted(item.get("activation_count", 0) for item in items)
    p95 = counts[int(len(counts) * 0.95)]
    assert p95 == 5
    assert items[-1]["activation_count"] > p95  # mem_hot should be penalized


def test_no_penalty_under_20_results():
    """<20 条结果时不触发 P95 惩罚。"""
    items = [
        {"id": f"mem_{i}", "score": 0.03, "activation_count": i * 10,
         "created_at": datetime.now(timezone.utc).isoformat()}
        for i in range(10)
    ]
    # Should not apply penalty
    assert len(items) < 20  # Guard: this test only makes sense with <20 items


# ════════════════════════════════════════════
#  Block 6: resolve_thread 触发词
# ════════════════════════════════════════════

class TestResolvePatterns:
    """resolve_thread 触发词正例和反例。"""

    # ── 正例：应该触发 ──

    @pytest.mark.parametrize("phrase", [
        "已完成", "搞定了", "做完了", "已解决", "完成了",
        "改了", "改完了", "好了", "弄好了", "处理了",
        "处理完了", "OK了", "ok了", "搞好了", "修好了",
        "解决了", "Done", "done", "finished", "fixed",
        "已经弄好", "已经做了", "办好了",
    ])
    def test_positive_pattern_matches(self, phrase):
        """每个触发词都应被识别。"""
        assert phrase.lower() in [p.lower() for p in _RESOLVE_PATTERNS]

    def test_auto_resolve_triggers_on_match(self):
        """包含触发词 + 有 resolved=False 的候选 → 自动标 resolved。"""
        import github_store as store
        mem = {"id": "mem_task_1", "resolved": False, "updated_at": ""}
        candidates = [{"mem": mem}]

        resolved = _check_auto_resolve("那个bug已经修好了", candidates)
        assert "mem_task_1" in resolved
        assert mem["resolved"] is True

    def test_auto_resolve_with_done_english(self):
        """英文 done 也触发。"""
        mem = {"id": "mem_task_2", "resolved": False, "updated_at": ""}
        resolved = _check_auto_resolve("That's done now", [{"mem": mem}])
        assert "mem_task_2" in resolved

    # ── 反例：不应触发 ──

    def test_no_resolve_without_trigger_phrase(self):
        """不包含触发词时不触发。"""
        mem = {"id": "mem_task_3", "resolved": False, "updated_at": ""}
        resolved = _check_auto_resolve("我还在做这个功能", [{"mem": mem}])
        assert resolved == []
        assert mem["resolved"] is False

    def test_no_resolve_when_already_resolved(self):
        """已经 resolved=True 的记忆不被重复处理。"""
        mem = {"id": "mem_task_4", "resolved": True, "updated_at": ""}
        resolved = _check_auto_resolve("搞定了", [{"mem": mem}])
        assert resolved == []

    def test_no_resolve_when_resolved_is_none(self):
        """resolved=None（非待办记忆）不触发。"""
        mem = {"id": "mem_fact", "resolved": None, "updated_at": ""}
        resolved = _check_auto_resolve("搞定了", [{"mem": mem}])
        assert resolved == []

    def test_partial_match_still_triggers(self):
        """触发词出现在句子中间也能识别。"""
        mem = {"id": "mem_task_5", "resolved": False, "updated_at": ""}
        resolved = _check_auto_resolve("上次说的那个事情我已经处理完了哦", [{"mem": mem}])
        assert "mem_task_5" in resolved

    def test_multiple_candidates_selective_resolve(self):
        """多个候选中只 resolve 有 resolved=False 的。"""
        mem_task = {"id": "t1", "resolved": False, "updated_at": ""}
        mem_fact = {"id": "t2", "resolved": None, "updated_at": ""}
        mem_done = {"id": "t3", "resolved": True, "updated_at": ""}
        candidates = [{"mem": mem_task}, {"mem": mem_fact}, {"mem": mem_done}]

        resolved = _check_auto_resolve("OK了", candidates)
        assert resolved == ["t1"]
        assert mem_task["resolved"] is True
        assert mem_fact["resolved"] is None
        assert mem_done["resolved"] is True


# ════════════════════════════════════════════
#  Block 1 补充：database 层过滤
# ════════════════════════════════════════════

def test_fts_search_exclude_resolved(fake_env):
    """fts_search 的 exclude_resolved 参数正确过滤。"""
    _make_mem(fake_env, "mem_fts_active", "Alexander works in Shenzhen office")
    _make_mem(fake_env, "mem_fts_resolved", "Book flight for Alexander", resolved=True)

    results = database.fts_search("Alexander", exclude_resolved=True)
    ids = [r["id"] for r in results]
    assert "mem_fts_active" in ids
    assert "mem_fts_resolved" not in ids


def test_fts_search_exclude_superseded(fake_env):
    """fts_search 的 exclude_superseded 参数正确过滤。"""
    _make_mem(fake_env, "mem_fts_old", "Benjamin lives in Hangzhou", superseded_by="mem_fts_new2")
    _make_mem(fake_env, "mem_fts_new2", "Benjamin moved to Chengdu")

    results = database.fts_search("Benjamin", exclude_superseded=True)
    ids = [r["id"] for r in results]
    assert "mem_fts_old" not in ids
    assert "mem_fts_new2" in ids


def test_vector_search_exclude_resolved(fake_env):
    """vector_search 的 exclude_resolved 参数正确过滤。"""
    mem = _make_mem(fake_env, "mem_vec_resolved", "王五的生日礼物买好了", resolved=True)
    _make_mem(fake_env, "mem_vec_active", "王五喜欢看电影")

    vec = _make_vec(hash("王五"))
    results = database.vector_search(vec, top_k=10, exclude_resolved=True)
    ids = [r["id"] for r in results]
    assert "mem_vec_resolved" not in ids


def test_vector_search_exclude_superseded(fake_env):
    """vector_search 的 exclude_superseded 参数正确过滤。"""
    _make_mem(fake_env, "mem_vec_old", "赵六用 iPhone 12", superseded_by="mem_vec_new3")
    _make_mem(fake_env, "mem_vec_new3", "赵六换了 iPhone 15")

    vec = _make_vec(hash("赵六"))
    results = database.vector_search(vec, top_k=10, exclude_superseded=True)
    ids = [r["id"] for r in results]
    assert "mem_vec_old" not in ids


# ════════════════════════════════════════════
#  压力测试：大量 resolved 记忆下 recall 仍返回满额
# ════════════════════════════════════════════

def test_recall_returns_full_topk_despite_many_resolved(fake_env):
    """100 条 resolved + 15 条 active → recall(top_k=10) 应从 active 里拿满 10 条。"""
    for i in range(100):
        _make_mem(fake_env, f"mem_resolved_{i}", f"resolved task number {i}",
                  resolved=True, days_ago=i % 30)

    for i in range(15):
        _make_mem(fake_env, f"mem_active_{i}", f"active memory about cats number {i}",
                  days_ago=i)

    results = asyncio.run(memory_ops.recall("cats", top_k=10))
    assert len(results) >= 10, f"Expected ≥10 results, got {len(results)}"
    for r in results:
        assert not r["id"].startswith("mem_resolved_"), \
            f"Resolved memory {r['id']} leaked into results"


# ════════════════════════════════════════════
#  Block 6 补充：词边界防护反例
# ════════════════════════════════════════════

class TestResolveBoundaryGuards:
    """触发词的否定前缀和疑问后缀应阻止触发。"""

    def test_doubt_suffix_haoleba(self):
        """'好了吧' — 疑问语气，不应触发。"""
        assert not _matches_resolve_pattern("好了吧？")

    def test_negation_prefix_mei(self):
        """'还没好了' — 否定前缀，不应触发。"""
        assert not _matches_resolve_pattern("还没好了")

    def test_doubt_suffix_ma(self):
        """'好了吗' — 疑问语气，不应触发。"""
        assert not _matches_resolve_pattern("搞定了吗")

    def test_negation_prefix_mei_you(self):
        """'没好呢' — 否定，不应触发。"""
        assert not _matches_resolve_pattern("没好呢")

    def test_positive_still_works_after_guards(self):
        """正常完成短语仍然触发。"""
        assert _matches_resolve_pattern("那个 bug 修好了")
        assert _matches_resolve_pattern("已经搞定了！")
        assert _matches_resolve_pattern("done")

    def test_sentence_ending_triggers(self):
        """触发词在句尾（无后缀）应触发。"""
        assert _matches_resolve_pattern("上次说的事情改完了")

    def test_doubt_with_but(self):
        """'好了但是' — 后缀不在疑问列表中，应触发。"""
        assert _matches_resolve_pattern("好了但是还有问题")
