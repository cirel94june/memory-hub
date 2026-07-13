"""
记忆操作：增删改查 + 向量搜索 + 衰减 + 自动合并 + 长文拆分
基于 SQLite 持久化，通过 database.py 做搜索/查询，通过 github_store 做 CRUD
"""
import json
import time
import math
import asyncio
import logging
from datetime import datetime, timezone

from config import (DECAY_LAMBDA, DECAY_LAMBDA_FAST, DECAY_THRESHOLD,
                    MERGE_SIMILARITY, ROOMS, get_room, AI_ALIASES, AI_ALIAS_GROUPS)
from embedding import get_embedding, pack_embedding, unpack_embedding, cosine_similarity
import github_store as store
import database
import analyzer

logger = logging.getLogger("memory_hub.ops")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _gen_id() -> str:
    return f"mem_{int(time.time() * 1000)}_{int(time.time_ns() % 10000):04d}"


def _safe_float(val, default: float = 0.0) -> float:
    """Safely convert a value to float, returning default for None/empty/invalid."""
    if val is None or val == "":
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _identity_ids(ai_id: str) -> set[str]:
    if not ai_id:
        return set()
    canonical = AI_ALIASES.get(ai_id, ai_id)
    return set(AI_ALIAS_GROUPS.get(canonical, [canonical, ai_id]))


def _distance_to_cosine(distance: float) -> float:
    """Convert sqlite-vec L2 distance to cosine similarity.

    For normalized vectors (all-MiniLM-L6-v2 produces unit vectors):
        cosine_similarity = 1 - (distance^2) / 2
    """
    return 1.0 - (distance ** 2) / 2


def _fuzzy_score(query: str, text: str) -> float:
    if not query or not text:
        return 0.0
    query_lower = query.lower()
    text_lower = text.lower()
    if query_lower in text_lower:
        return 1.0
    words = query_lower.split()
    if not words:
        return 0.0
    matched = sum(1 for w in words if w in text_lower)
    return matched / len(words)


def _bm25_score(query: str, content: str, avg_len: float = 200.0, k1: float = 1.5, b: float = 0.75) -> float:
    """简化版 BM25 关键词评分"""
    if not query or not content:
        return 0.0
    query_terms = set(query.lower().split())
    content_lower = content.lower()
    doc_len = len(content_lower)

    score = 0.0
    for term in query_terms:
        # term frequency
        tf = content_lower.count(term)
        if tf == 0:
            continue
        # BM25 TF saturation
        tf_norm = (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * doc_len / avg_len))
        score += tf_norm

    # normalize by query length
    return min(1.0, score / max(1, len(query_terms)))


def _exact_match_score(query: str, mem: dict) -> float:
    """精确匹配评分：query 完整出现在 content/tags/category 中"""
    if not query:
        return 0.0
    q = query.lower().strip()
    fields = [
        mem.get("content", ""),
        mem.get("category", ""),
        " ".join(_parse_json_field(mem.get("tags", "[]"))),
        " ".join(_parse_json_field(mem.get("domain", "[]"))),
    ]
    for field in fields:
        if q in field.lower():
            return 1.0
    # 检查 query 中每个长词（>=2字符）是否出现
    words = [w for w in q.split() if len(w) >= 2]
    if not words:
        return 0.0
    all_text = " ".join(fields).lower()
    matched = sum(1 for w in words if w in all_text)
    return matched / len(words)


def _rrf_merge(*rank_lists, k: int = 60) -> list[dict]:
    """Reciprocal Rank Fusion：多路排序结果融合

    每路给每个 ID 打分 1/(k+rank)，最后按总分排序。
    k=60 是标准 RRF 参数。
    """
    scores = {}  # id -> {"score": float, "data": dict}
    for rank_list in rank_lists:
        for rank, item in enumerate(rank_list):
            mid = item["id"]
            rrf_score = 1.0 / (k + rank + 1)
            if mid not in scores:
                scores[mid] = {"score": 0.0, "data": item}
            scores[mid]["score"] += rrf_score

    merged = sorted(scores.values(), key=lambda x: x["score"], reverse=True)
    return [item["data"] | {"score": round(item["score"], 6)} for item in merged]


# ── 写入记忆（带自动打标 + 合并检测） ──

async def remember(
    content: str,
    layer: str = "shared",
    room: str = "living_room",
    category: str = "",
    owner_ai: str = "",
    importance: float = 0.5,
    emotion_arousal: float = 0.3,
    source_ai: str = "",
    source_platform: str = "",
    tags: list[str] = None,
    event_date: str = "",
    source_context: str = "",
    auto_analyze: bool = True,
    auto_merge: bool = True,
    quick: bool = False,
    force_create: bool = False,
) -> dict:
    """写入一条新记忆，自动打标 + 智能关系检测（更新/取代/合并/新建）"""
    # 归一化 AI 别名（cloudy → claude）
    from config import AI_ALIASES
    source_ai = AI_ALIASES.get(source_ai, source_ai)
    owner_ai = AI_ALIASES.get(owner_ai, owner_ai)

    if quick:
        auto_merge = False
        # 写入门卫：自动提取路径先过一道检查，明显的垃圾碎片不入库
        # （人工写入 quick=False，不走门卫）
        try:
            import write_gate
            allowed, gate_reason = write_gate.check(content, room=room, source=source_platform)
            if not allowed:
                logger.info(f"Write gate blocked ({gate_reason}): {content[:60]}")
                return {"id": "", "status": "gated", "reason": gate_reason}
        except Exception as e:
            logger.warning(f"Write gate check failed (allowing): {e}")
        # 轻量去重：只查 embedding 相似度，不调 LLM
        try:
            qv = await get_embedding(content)
            if qv:
                similar = _find_similar_candidates(qv, [], threshold=0.85, top_k=1)
                if similar and similar[0]["score"] >= 0.85:
                    existing = similar[0]["mem"]
                    logger.info(f"Quick dedup: skipped (sim={similar[0]['score']:.2f}) with {existing['id']}")
                    return {"id": existing["id"], "status": "dedup_skipped", "similarity": similar[0]["score"]}
        except Exception as e:
            logger.warning(f"Quick dedup check failed: {e}")

    # Step 1: 自动打标
    original_category = category
    analysis = None
    if auto_analyze:
        analysis = await analyzer.analyze(content)
        if not tags:
            tags = analysis.get("tags", [])
        if not category and analysis.get("suggested_category"):
            category = analysis["suggested_category"]
        if emotion_arousal == 0.3 and analysis.get("arousal") is not None:
            emotion_arousal = analysis["arousal"]

    domain = analysis.get("domain", []) if analysis else []
    valence = analysis.get("valence", 0.5) if analysis else 0.5

    # Step 2: 智能关系检测（替代简单合并）
    if force_create:
        auto_merge = False
    if auto_merge:
        # 先找相似候选
        query_vec = await get_embedding(content)
        candidates = _find_similar_candidates(query_vec, domain, threshold=0.55, top_k=5) if query_vec else []

        if candidates:
            # 高相似度（>= MERGE_SIMILARITY）走传统合并
            best = candidates[0]
            if best["score"] >= MERGE_SIMILARITY:
                merge_result = await _try_merge(content, domain, tags, importance, valence, emotion_arousal, target_room=room)
                if merge_result:
                    return merge_result

            # 中等相似度（0.55-0.75）走关系分类：可能是更新/补充/矛盾
            if candidates and best["score"] < MERGE_SIMILARITY:
                relation_candidates = [
                    {"id": c["mem"]["id"], "content": c["mem"]["content"]}
                    for c in candidates[:5]
                ]
                relations = await analyzer.classify_relation(content, relation_candidates)

                superseded_ids = []
                linked_ids = []
                for rel in relations.get("relations", []):
                    target_id = rel.get("target_id", "")
                    target_mem = store.get_memory(target_id)
                    if not target_mem:
                        continue

                    if rel.get("should_supersede") and rel["relation"] in ("updates", "contradicts"):
                        # 标记旧记忆为 superseded
                        now = _now()
                        target_mem["status"] = "superseded"
                        target_mem["updated_at"] = now
                        target_mem["superseded_by"] = ""  # 先占位，下面填新 ID
                        # 给旧记忆追加一条年轮评论记录被取代的原因
                        comments = target_mem.get("comments", [])
                        if not isinstance(comments, list):
                            comments = []
                        comments.append({
                            "date": now,
                            "author": source_ai or "system",
                            "kind": "supersede_note",
                            "content": f"被新记忆取代。原因：{rel.get('reason', '信息已更新')}",
                        })
                        target_mem["comments"] = comments
                        store.set_memory(target_mem)
                        superseded_ids.append(target_id)
                        logger.info(f"Superseded {target_id}: {rel.get('reason')}")

                    elif rel["relation"] in ("supplements", "same_topic"):
                        linked_ids.append(target_id)

                # 新建记忆并关联
                mem_id = _gen_id()
                now = _now()

                mem = {
                    "id": mem_id,
                    "content": content,
                    "layer": layer,
                    "room": room,
                    "category": category,
                    "owner_ai": owner_ai,
                    "importance": importance,
                    "emotion_arousal": emotion_arousal,
                    "valence": valence,
                    "domain": json.dumps(domain),
                    "decay_score": 1.0,
                    "activation_count": 0,
                    "last_activated": "",
                    "source_ai": source_ai,
                    "source_platform": source_platform,
                    "tags": json.dumps(tags or []),
                    "linked_memories": json.dumps(linked_ids),
                    "supersedes": json.dumps(superseded_ids),
                    "event_date": event_date,
                    "source_context": source_context,
                    "comments": [],
                    "embedding": pack_embedding(query_vec) if query_vec else None,
                    "status": "active",
                    "created_at": now,
                    "updated_at": now,
                    "history": [{"v": 1, "content": content, "date": now, "by": source_ai or "system"}],
                }

                store.set_memory(mem)

                # 回填 superseded_by
                for sid in superseded_ids:
                    s_mem = store.get_memory(sid)
                    if s_mem:
                        s_mem["superseded_by"] = mem_id
                        store.set_memory(s_mem)

                result = {
                    "id": mem_id,
                    "status": "created",
                    "category": category,
                    "domain": domain,
                    "superseded": superseded_ids,
                    "linked": linked_ids,
                }
                if original_category and original_category != category:
                    result["original_category"] = original_category
                return result

    # Step 3: 新建记忆（无关联）
    mem_id = _gen_id()
    now = _now()
    vec = await get_embedding(content)

    mem = {
        "id": mem_id,
        "content": content,
        "layer": layer,
        "room": room,
        "category": category,
        "owner_ai": owner_ai,
        "importance": importance,
        "emotion_arousal": emotion_arousal,
        "valence": valence,
        "domain": json.dumps(domain),
        "decay_score": 1.0,
        "activation_count": 0,
        "last_activated": "",
        "source_ai": source_ai,
        "source_platform": source_platform,
        "tags": json.dumps(tags or []),
        "linked_memories": "[]",
        "supersedes": "[]",
        "event_date": event_date,
        "source_context": source_context,
        "comments": [],
        "embedding": pack_embedding(vec) if vec else None,
        "status": "active",
        "created_at": now,
        "updated_at": now,
        "history": [{"v": 1, "content": content, "date": now, "by": source_ai or "system"}],
    }

    store.set_memory(mem)
    result = {"id": mem_id, "status": "created", "category": category, "domain": domain, "linked": [], "superseded": []}
    if original_category and original_category != category:
        result["original_category"] = original_category
    return result


def _find_similar_candidates(query_vec, domain: list, threshold: float = 0.55, top_k: int = 5) -> list[dict]:
    """找出与 query 向量相似度超过阈值的记忆候选，按分数降序。
    Uses database.vector_search() instead of iterating all memories.
    """
    if not query_vec:
        return []

    # Fetch more than needed so we can filter by threshold after converting distance
    raw = database.vector_search(query_vec, top_k=top_k * 2, status="active")

    scored = []
    for mem in raw:
        distance = mem.pop("distance", 0.0)
        vec_sim = _distance_to_cosine(distance)

        # domain 匹配加分
        mem_domain = _parse_json_field(mem.get("domain", "[]"))
        domain_bonus = 0.05 if any(d in mem_domain for d in domain) else 0.0
        score = vec_sim + domain_bonus

        if score >= threshold:
            scored.append({"mem": mem, "score": score})

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_k]


async def _try_merge(content: str, domain: list, tags: list, importance: float,
                     valence: float, arousal: float, target_room: str = "") -> dict | None:
    """查找最相似的已有记忆，如果超过阈值则合并。
    Uses database.vector_search() instead of iterating all memories.
    """
    query_vec = await get_embedding(content)
    if not query_vec:
        return None

    raw = database.vector_search(query_vec, top_k=5, status="active")

    best_mem = None
    best_score = 0.0

    for mem in raw:
        distance = mem.pop("distance", 0.0)
        vec_sim = _distance_to_cosine(distance)

        # 跨房间不合并
        if target_room and mem.get("room", "") != target_room:
            continue

        # domain 匹配加分
        mem_domain = _parse_json_field(mem.get("domain", "[]"))
        domain_bonus = 0.05 if any(d in mem_domain for d in domain) else 0.0

        score = vec_sim + domain_bonus
        if score > best_score:
            best_score = score
            best_mem = mem

    if not best_mem or best_score < MERGE_SIMILARITY:
        return None

    # Re-fetch full memory (vector_search strips embedding)
    best_mem = store.get_memory(best_mem["id"])
    if not best_mem:
        return None

    # 执行合并（保留原文用于回滚）
    original_content = best_mem["content"]
    merged_content = await analyzer.merge(original_content, content)

    # 双重安全：如果合并结果和原文一模一样（说明 merge 返回了拼接而非真正合并），也接受
    # 但如果合并结果异常短，analyzer.merge 内部已经会拒绝并返回拼接版本
    now = _now()
    vec = await get_embedding(merged_content)

    best_mem["content"] = merged_content
    best_mem["updated_at"] = now
    best_mem["importance"] = max(_safe_float(best_mem.get("importance"), 0.5), importance)
    best_mem["emotion_arousal"] = (_safe_float(best_mem.get("emotion_arousal"), 0.3) + arousal) / 2
    best_mem["valence"] = (_safe_float(best_mem.get("valence"), 0.5) + valence) / 2

    old_tags = set(_parse_json_field(best_mem.get("tags", "[]")))
    old_tags.update(tags or [])
    best_mem["tags"] = json.dumps(list(old_tags)[:20])

    old_domain = set(_parse_json_field(best_mem.get("domain", "[]")))
    old_domain.update(domain)
    best_mem["domain"] = json.dumps(list(old_domain)[:5])

    if vec:
        best_mem["embedding"] = pack_embedding(vec)

    history = best_mem.get("history", [])
    # 先备份合并前的原文，再记录合并后的内容
    history.append({"v": len(history) + 1, "content": original_content, "date": now, "by": "pre_merge_backup"})
    history.append({"v": len(history) + 1, "content": merged_content, "date": now, "by": "auto_merge"})
    best_mem["history"] = history

    store.set_memory(best_mem)

    final_tags = _parse_json_field(best_mem.get("tags", "[]"))
    logger.info(f"Merged into {best_mem['id']} (score={best_score:.3f})")
    return {
        "id": best_mem["id"],
        "status": "merged_into_existing",
        "merged_into": best_mem["id"],
        "score": round(best_score, 3),
        "final_importance": best_mem["importance"],
        "merged_tags_count": len(final_tags),
    }


# ── 长文拆分（grow） ──

async def grow(
    content: str,
    source_ai: str = "",
    auto_merge: bool = True,
    quick: bool = False,
) -> dict:
    """把长文本拆分成多条独立记忆，每条独立走合并检测"""
    items = await analyzer.digest(content)
    if not items:
        result = await remember(content, source_ai=source_ai, auto_merge=auto_merge)
        return {"total": 1, "created": 1, "merged": 0, "items": [result]}

    created = 0
    merged = 0
    results = []
    for item in items:
        r = await remember(
            content=item["content"],
            room=item.get("room", "living_room"),
            category=item.get("name", ""),
            importance=item.get("importance", 0.5),
            emotion_arousal=item.get("arousal", 0.3),
            source_ai=source_ai,
            tags=item.get("tags"),
            auto_analyze=False,
            auto_merge=auto_merge,
        )
        # Set domain/valence from digest result
        if r.get("status") == "created":
            mem = store.get_memory(r["id"])
            if mem:
                mem["domain"] = json.dumps(item.get("domain", []))
                mem["valence"] = item.get("valence", 0.5)
                store.set_memory(mem)
            created += 1
        elif r.get("status") in ("merged", "merged_into_existing"):
            merged += 1
        results.append(r)

    return {"total": len(results), "created": created, "merged": merged, "items": results}


# ── 更新记忆 ──

async def update_memory(memory_id: str, content: str = None, importance: float = None,
                        room: str = None, category: str = None, tags: list[str] = None,
                        owner_ai: str = None, source_ai: str = None,
                        layer: str = None, changed_by: str = "") -> dict:
    mem = store.get_memory(memory_id)
    if not mem:
        return {"id": memory_id, "status": "not_found"}

    now = _now()
    mem["updated_at"] = now

    if content is not None and content != mem.get("content"):
        mem["content"] = content
        vec = await get_embedding(content)
        if vec:
            mem["embedding"] = pack_embedding(vec)
        history = mem.get("history", [])
        history.append({"v": len(history) + 1, "content": content, "date": now, "by": changed_by})
        mem["history"] = history

    if importance is not None:
        mem["importance"] = importance
    if room is not None:
        mem["room"] = room
        new_room_cfg = get_room(room) or {}
        if new_room_cfg.get("scope") == "per_ai" and not mem.get("owner_ai"):
            mem["owner_ai"] = changed_by or "claude"
    if category is not None:
        mem["category"] = category
    if tags is not None:
        mem["tags"] = json.dumps(tags)
    if owner_ai is not None:
        mem["owner_ai"] = owner_ai
    if source_ai is not None:
        mem["source_ai"] = source_ai
    if layer is not None:
        mem["layer"] = layer

    store.set_memory(mem)
    return {"id": memory_id, "status": "updated"}


# ── 召回记忆（多维搜索） ──

def _parse_json_field(val) -> list:
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            return []
    return []


async def recall(
    query: str,
    ai_id: str = "",
    top_k: int = 8,
    threshold: float = 0.25,
    include_rooms: list[str] = None,
    exclude_isolated: bool = True,
    query_domain: list[str] = None,
    query_valence: float = -1,
    query_arousal: float = -1,
    skip_analyze: bool = False,
) -> list[dict]:
    """混合搜索召回记忆（向量 + BM25 FTS + 精确匹配，RRF 融合）

    三路并行搜索，Reciprocal Rank Fusion 融合排序：
    1. 向量路：database.vector_search（语义匹配）
    2. 关键词路：database.fts_search（BM25 全文搜索）
    3. 精确路：SQL LIKE + Python post-filter（最强信号）

    + unresolved 记忆优先浮现（最多 2 条）

    skip_analyze=True: 跳过 LLM analyzer 调用（用于 TG context 快速路径），
    仅用向量+关键词+LIKE 搜索，不做 emotion/domain 加权。
    """
    ai_ids = _identity_ids(ai_id)
    query_vec = await get_embedding(query)

    if not query_domain and not skip_analyze:
        try:
            q_analysis = await analyzer.analyze(query)
            query_domain = q_analysis.get("domain", [])
            if query_valence < 0:
                query_valence = q_analysis.get("valence", 0.5)
            if query_arousal < 0:
                query_arousal = q_analysis.get("arousal", 0.3)
        except Exception:
            query_domain = []

    isolated_rooms = {k for k, v in ROOMS.items() if v.get("type") == "isolated"}

    # Build filter kwargs for database queries
    db_kwargs = {}
    if include_rooms:
        db_kwargs["include_rooms"] = include_rooms
    elif exclude_isolated and isolated_rooms:
        db_kwargs["exclude_rooms"] = list(isolated_rooms)

    def _build_result(mem, score):
        room = mem.get("room", "")
        comments = mem.get("comments", [])
        if isinstance(comments, str):
            try:
                comments = json.loads(comments)
            except Exception:
                comments = []
        comment_count = len(comments) if isinstance(comments, list) else 0
        linked = _parse_json_field(mem.get("linked_memories", "[]"))
        return {
            "id": mem["id"],
            "content": mem["content"],
            "layer": mem.get("layer", ""),
            "room": room,
            "category": mem.get("category", ""),
            "domain": _parse_json_field(mem.get("domain", "[]")),
            "importance": _safe_float(mem.get("importance"), 0.5),
            "valence": _safe_float(mem.get("valence"), 0.5),
            "arousal": _safe_float(mem.get("emotion_arousal"), 0.3),
            "score": round(score, 4),
            "created_at": mem.get("created_at", ""),
            "event_date": mem.get("event_date", ""),
            "resolved": mem.get("resolved", None),
            "activation_count": mem.get("activation_count", 0),
            "comment_count": comment_count,
            "comments": comments[-3:] if comment_count > 0 else [],
            "linked_memories": linked,
            "source_ai": mem.get("source_ai", ""),
            "source_context": mem.get("source_context", ""),
        }

    def _passes_private_filter(mem):
        """Check private layer access — must be done in Python since
        database queries don't enforce cross-field logic like this."""
        if mem.get("layer") == "private":
            if not ai_ids or mem.get("owner_ai") not in ai_ids:
                return False
        return True

    # ── 纯 DB 搜索逻辑（可在线程中运行）──
    def _db_search_all():
        """同步函数：执行所有 DB 搜索路径，返回四路原始结果。
        skip_analyze=True 时此函数在 to_thread 中运行，使用只读连接。"""
        use_ro = skip_analyze  # context hot path → 用只读连接

        # 路径 1：向量搜索
        vec_results = []
        if query_vec:
            search_fn = database.ro_vector_search if use_ro else database.vector_search
            raw_vec = search_fn(query_vec, top_k=50, status="active", **db_kwargs)
            vec_scored = []
            for mem in raw_vec:
                if not _passes_private_filter(mem):
                    continue
                distance = mem.pop("distance", 0.0)
                vec_sim = _distance_to_cosine(distance)
                embed_score = max(0, vec_sim)
                if query_valence >= 0 and query_arousal >= 0:
                    mv = _safe_float(mem.get("valence"), 0.5)
                    ma = _safe_float(mem.get("emotion_arousal"), 0.3)
                    emotion_score = 1.0 - math.sqrt(((query_valence - mv)**2 + (query_arousal - ma)**2) / 2)
                else:
                    emotion_score = 0.5
                try:
                    created = datetime.fromisoformat(mem["created_at"])
                    days = (datetime.now(timezone.utc) - created).total_seconds() / 86400
                    time_score = math.exp(-0.02 * days)
                except Exception:
                    time_score = 0.5
                importance_score = _safe_float(mem.get("importance"), 0.5)
                final = (embed_score * 0.6 + emotion_score * 0.15 +
                         time_score * 0.1 + importance_score * 0.15)
                vec_scored.append((mem, final))
            vec_scored.sort(key=lambda x: x[1], reverse=True)
            vec_results = [_build_result(m, s) for m, s in vec_scored[:50]]

        # 路径 2：FTS5
        fts_fn = database.ro_fts_search if use_ro else database.fts_search
        raw_fts = fts_fn(query, top_k=50, status="active")
        kw_scored = []
        for mem in raw_fts:
            if not _passes_private_filter(mem):
                continue
            room = mem.get("room", "")
            if exclude_isolated and room in isolated_rooms:
                continue
            if include_rooms and room not in include_rooms:
                continue
            rank = mem.pop("rank", 0.0)
            bm25 = min(1.0, abs(rank) / 10.0) if rank else 0.0
            if bm25 > 0.01:
                kw_scored.append((mem, bm25))
        kw_scored.sort(key=lambda x: x[1], reverse=True)
        kw_results = [_build_result(m, s) for m, s in kw_scored[:50]]

        # 路径 2.5：CJK LIKE
        like_results = []
        try:
            like_fn = database.ro_cjk_like_search if use_ro else database.cjk_like_search
            like_raw = like_fn(query, top_k=50, status="active")
            like_scored = []
            for mem in like_raw:
                if not _passes_private_filter(mem):
                    continue
                room = mem.get("room", "")
                if exclude_isolated and room in isolated_rooms:
                    continue
                if include_rooms and room not in include_rooms:
                    continue
                like_scored.append((mem, min(1.0, mem.pop("like_hits", 1) / 5.0)))
            like_results = [_build_result(m, s) for m, s in like_scored[:50]]
        except Exception as e:
            logger.warning(f"cjk_like_search path failed: {e}")

        # 路径 3：精确匹配
        get_fn = database.ro_get_memory if use_ro else store.get_memory
        seen_ids = set()
        exact_candidates = []
        for result_list in (vec_results, kw_results, like_results):
            for item in result_list:
                mid = item["id"]
                if mid not in seen_ids:
                    seen_ids.add(mid)
                    mem = get_fn(mid)
                    if mem:
                        exact_candidates.append(mem)
        exact_scored = []
        for mem in exact_candidates:
            exact = _exact_match_score(query, mem)
            if exact > 0.3:
                exact_scored.append((mem, exact))
        exact_scored.sort(key=lambda x: x[1], reverse=True)
        exact_results = [_build_result(m, s) for m, s in exact_scored[:50]]

        # RRF 融合
        merged = _rrf_merge(vec_results, kw_results, like_results, exact_results)

        # Unresolved 优先浮现
        unresolved_items = []
        normal_items = []
        for item in merged:
            mem = get_fn(item["id"])
            if mem and mem.get("resolved") == False and len(unresolved_items) < 2:
                item["_unresolved"] = True
                unresolved_items.append(item)
            else:
                normal_items.append(item)

        return (unresolved_items + normal_items)[:top_k]

    # skip_analyze=True → context hot path：在线程中跑 DB 搜索，不阻塞事件循环
    if skip_analyze:
        results = await asyncio.to_thread(_db_search_all)
    else:
        results = _db_search_all()

    # ── Touch：更新 activation_count（写操作留在主线程） ──
    now = _now()
    for r in results:
        m = store.get_memory(r["id"])
        if m:
            m["activation_count"] = m.get("activation_count", 0) + 1
            m["last_activated"] = now
            store.set_memory(m)
            _time_ripple(m)

    # ── 替换 superseded 记忆 ──
    filtered_results = []
    for r in results:
        m = store.get_memory(r["id"])
        if m and m.get("status") == "superseded":
            new_id = m.get("superseded_by")
            if new_id:
                new_m = store.get_memory(new_id)
                if new_m and new_m.get("status") == "active":
                    r["id"] = new_m["id"]
                    r["content"] = new_m["content"]
                    r["superseded_from"] = m["id"]
            else:
                continue
        r.pop("_unresolved", None)
        s = r.get("score", 0)
        r["confidence"] = "high" if s >= 0.035 else "medium" if s >= 0.02 else "low" if s >= 0.01 else "weak"
        filtered_results.append(r)

    return filtered_results


# ── 年轮评论（不改原文，追加感悟/反思/更新注记） ──

async def add_comment(
    memory_id: str,
    content: str,
    author: str = "claude",
    kind: str = "comment",
    valence: float = None,
    arousal: float = None,
) -> dict:
    """给记忆追加年轮评论，不修改原始内容。

    kind 类型：
    - comment: 普通评论
    - reflection: 回顾反思（重读旧记忆时的新理解）
    - update_note: 信息补充（不改原文，追加新发现）
    - feel: 情感标注
    - supersede_note: 系统自动追加的取代备注
    """
    mem = store.get_memory(memory_id)
    if not mem:
        return {"id": memory_id, "status": "not_found"}

    now = _now()
    comments = mem.get("comments", [])
    if not isinstance(comments, list):
        comments = []

    entry = {
        "id": f"cmt_{int(time.time() * 1000)}",
        "date": now,
        "author": author,
        "kind": kind,
        "content": content,
    }
    if valence is not None:
        entry["valence"] = max(0.0, min(1.0, valence))
    if arousal is not None:
        entry["arousal"] = max(0.0, min(1.0, arousal))

    comments.append(entry)
    mem["comments"] = comments
    mem["updated_at"] = now
    # 评论也算一次激活
    mem["activation_count"] = mem.get("activation_count", 0) + 1
    mem["last_activated"] = now

    store.set_memory(mem)

    logger.info(f"Added {kind} comment to {memory_id} by {author}")
    return {"id": memory_id, "comment_id": entry["id"], "status": "commented"}


# ── 标记记忆为已解决/未解决 ──

async def resolve_memory(memory_id: str, resolved: bool = True) -> dict:
    """标记一条记忆为已解决（resolved=True）或未解决（resolved=False）。
    未解决的记忆在 recall 时会优先浮现。"""
    mem = store.get_memory(memory_id)
    if not mem:
        return {"id": memory_id, "status": "not_found"}
    mem["resolved"] = resolved
    mem["updated_at"] = _now()
    store.set_memory(mem)
    return {"id": memory_id, "status": "resolved" if resolved else "unresolved"}


# ── 时间涟漪（触碰一条记忆时，时间上相邻的记忆轻微激活） ──

def _time_ripple(touched_mem: dict, max_ripple: int = 5, hours: float = 48.0):
    """被召回的记忆附近 ±48 小时创建的记忆也轻微唤醒 (+0.3)

    NOTE: This still uses store.get_all_memories() which queries SQLite under the hood.
    Could be optimized with a time-range SQL query, but ripple runs rarely and only
    affects max 5 memories, so the current approach is acceptable.
    """
    try:
        ref_time = datetime.fromisoformat(touched_mem.get("event_date") or touched_mem["created_at"])
    except Exception:
        return

    rippled = 0
    for mem in store.get_all_memories().values():
        if rippled >= max_ripple:
            break
        if mem["id"] == touched_mem["id"]:
            continue
        if mem.get("status") != "active" or not mem.get("embedding"):
            continue
        try:
            created = datetime.fromisoformat(mem["created_at"])
            delta_hours = abs((ref_time - created).total_seconds()) / 3600
            if delta_hours <= hours:
                mem["activation_count"] = round(mem.get("activation_count", 0) + 0.3, 1)
                store.set_memory(mem)
                rippled += 1
        except Exception:
            continue


# ── 获取客厅内容 ──

async def get_living_room() -> list[dict]:
    mems = database.query_memories(
        room="living_room", status="active",
        order_by="importance DESC", limit=20,
    )
    return [
        {"id": m["id"], "content": m["content"], "category": m.get("category", ""),
         "importance": m.get("importance", 0.5), "updated_at": m.get("updated_at", "")}
        for m in mems
    ]


# ── AI 私有记忆概要 ──

async def get_ai_private_summary(ai_id: str, limit: int = 10) -> list[dict]:
    mems = database.query_memories(
        layer="private", owner_ai=ai_id, status="active",
        order_by="importance DESC", limit=limit,
    )
    return [
        {"id": m["id"], "content": m["content"], "category": m.get("category", ""),
         "importance": m.get("importance", 0.5), "created_at": m.get("created_at", "")}
        for m in mems
    ]


# ── 列出记忆 ──

async def list_memories(
    layer: str = None, room: str = None, owner_ai: str = None,
    source_ai: str = None,
    ai_id: str = None,
    status: str = "active", page: int = 1, per_page: int = 20,
) -> dict:
    if ai_id:
        ids = _identity_ids(ai_id)
        all_mems = database.query_memories(
            layer=layer, room=room, owner_ai=owner_ai, status=status,
            order_by="updated_at DESC",
        )

        def related(mem):
            return mem.get("source_ai") in ids or mem.get("owner_ai") in ids

        filtered = [m for m in all_mems if related(m)]
        total = len(filtered)
        start = (page - 1) * per_page
        mems = filtered[start:start + per_page]
    else:
        mems = database.query_memories(
            layer=layer, room=room, owner_ai=owner_ai, source_ai=source_ai, status=status,
            limit=per_page, offset=(page - 1) * per_page,
            order_by="updated_at DESC",
        )
        total = len(database.query_memories(
            layer=layer, room=room, owner_ai=owner_ai, source_ai=source_ai, status=status,
        ))

    clean = []
    for m in mems:
        c = {k: v for k, v in m.items() if k not in ("embedding", "history")}
        clean.append(c)

    return {"total": total, "page": page, "per_page": per_page, "items": clean}


# ── 归档/删除 ──

async def archive_memory(memory_id: str) -> dict:
    mem = store.get_memory(memory_id)
    if mem:
        mem["status"] = "archived"
        mem["updated_at"] = _now()
        store.set_memory(mem)
    return {"id": memory_id, "status": "archived"}


async def delete_memory(memory_id: str) -> dict:
    store.remove_memory(memory_id)
    return {"id": memory_id, "status": "deleted"}


def _normalize_for_dedup(content: str) -> str:
    text = (content or "").strip().lower()
    for prefix in ("[用户]", "[互动]", "[AI]", "[鐢ㄦ埛]", "[浜掑姩]"):
        if text.startswith(prefix.lower()):
            text = text[len(prefix):].strip()
    return "".join(ch for ch in text if ch.isalnum())


async def deduplicate_public_memories(similarity_threshold: float = 0.92, dry_run: bool = True) -> dict:
    """Conservatively archive duplicate shared memories."""
    active = [
        m for m in database.iter_memories(status="active")
        if m.get("layer", "shared") == "shared"
        and not m.get("owner_ai")
        and m.get("room") != "game_room"
    ]
    archived = []
    checked = set()

    for i, a in enumerate(active):
        a_key = _normalize_for_dedup(a.get("content", ""))
        for b in active[i + 1:]:
            pair_key = tuple(sorted([a["id"], b["id"]]))
            if pair_key in checked:
                continue
            checked.add(pair_key)
            if a.get("room") != b.get("room"):
                continue
            if a.get("category", "") and b.get("category", "") and a.get("category") != b.get("category"):
                continue

            duplicate = False
            reason = ""
            b_key = _normalize_for_dedup(b.get("content", ""))
            if a_key and a_key == b_key:
                duplicate = True
                reason = "normalized_equal"
            elif a.get("embedding") and b.get("embedding"):
                sim = cosine_similarity(unpack_embedding(a["embedding"]), unpack_embedding(b["embedding"]))
                if sim >= similarity_threshold:
                    duplicate = True
                    reason = f"embedding_sim={sim:.2f}"
            if not duplicate:
                continue

            older = a if a.get("created_at", "") <= b.get("created_at", "") else b
            newer = b if older is a else a
            archived.append({
                "archived_id": older["id"],
                "kept_id": newer["id"],
                "reason": reason,
                "archived_preview": older.get("content", "")[:80],
                "kept_preview": newer.get("content", "")[:80],
            })
            if not dry_run:
                older["status"] = "archived"
                older["updated_at"] = _now()
                comments = older.get("comments", [])
                if not isinstance(comments, list):
                    comments = []
                comments.append({
                    "date": _now(),
                    "author": "deduplicate_public_memories",
                    "kind": "supersede_note",
                    "content": f"公共记忆去重：与 {newer['id']} 重复，原因 {reason}，归档较旧条目。",
                })
                older["comments"] = comments
                store.set_memory(older)

    return {
        "dry_run": dry_run,
        "candidates": len(archived),
        "archived": 0 if dry_run else len(archived),
        "items": archived[:50],
    }


async def fix_private_capture_layers(dry_run: bool = True) -> dict:
    """Move memories captured from private chats back to the owner's private layer."""
    fixed = []
    for mem in database.iter_memories(status="active"):
        platform = mem.get("source_platform") or ""
        source_ai = mem.get("source_ai") or ""
        if mem.get("layer") == "private":
            continue
        if not source_ai:
            continue
        is_private_capture = (
            platform.endswith(":private")
            or platform in ("proxy", "mcp_extract")
        )
        if not is_private_capture:
            continue
        fixed.append({
            "id": mem["id"],
            "source_ai": source_ai,
            "old_layer": mem.get("layer", "shared"),
            "preview": mem.get("content", "")[:80],
        })
        if not dry_run:
            mem["layer"] = "private"
            mem["owner_ai"] = source_ai
            mem["updated_at"] = _now()
            store.set_memory(mem)
    return {"dry_run": dry_run, "fixed": 0 if dry_run else len(fixed), "candidates": len(fixed), "items": fixed[:50]}


# ── 记忆衰减（含情感维度） ──
PROTECTION_REASON_TEXT = {
    "anchored": "你手动设为了锚点：它不参与衰减，和重要度高低无关。",
    "living_room": "它在客厅：这是核心身份或当前状态，会优先注入，但仍应随新信息更新。",
    "high_importance": "重要度达到 80% 以上：系统把它当作长期重要记忆。",
    "often_recalled": "它已经被召回至少 3 次：说明 AI 经常用到它。",
    "emotionally_strong": "情绪唤醒度较高：强情绪记忆会衰减更慢。",
}

PRESSURE_REASON_TEXT = {
    "low_importance": "重要度低于 50%：更容易进入衰减。",
    "never_recalled": "从未被召回：系统暂时没有证据说明它常用。",
    "fast_decay_room": "它所在房间属于快衰减房间。",
    "auto_capture_unrecalled": "这是自动捕获且长期没被召回的记忆，会加速衰减。",
    "old": "它已经存在较久，会受到时间衰减压力。",
}


def _lane_reason(lane: str, protections: list[str], pressures: list[str]) -> str:
    if lane == "protected":
        if "anchored" in protections:
            return PROTECTION_REASON_TEXT["anchored"]
        if "living_room" in protections:
            return PROTECTION_REASON_TEXT["living_room"]
        return "这条记忆命中了强保护规则。"
    if lane == "long_term":
        reasons = [PROTECTION_REASON_TEXT[p] for p in protections if p in PROTECTION_REASON_TEXT]
        return " ".join(reasons) or "这条记忆重要度较高或经常被想起，因此暂时放在长期记忆。"
    if lane == "short_term":
        reasons = [PRESSURE_REASON_TEXT[p] for p in pressures if p in PRESSURE_REASON_TEXT]
        return " ".join(reasons) or "这条记忆更像短期材料，会自然衰减。"
    reasons = [PRESSURE_REASON_TEXT[p] for p in pressures if p in PRESSURE_REASON_TEXT]
    return " ".join(reasons) or "这条记忆没有强保护理由，先放在观察中。"

def explain_decay(mem: dict, now_dt: datetime | None = None) -> dict:
    """Explain why a memory is kept, watched, or allowed to decay."""
    now_dt = now_dt or datetime.now(timezone.utc)
    try:
        created = datetime.fromisoformat(mem.get("created_at", ""))
        days = max(0.0, (now_dt - created).total_seconds() / 86400)
    except Exception:
        days = 0.0

    importance = _safe_float(mem.get("importance"), 0.5)
    arousal = _safe_float(mem.get("emotion_arousal"), 0.3)
    activations = int(_safe_float(mem.get("activation_count"), 0))
    room = mem.get("room", "")
    room_cfg = get_room(room) or {}
    source_platform = mem.get("source_platform") or ""
    anchored = bool(mem.get("anchored"))

    lam = DECAY_LAMBDA_FAST if room_cfg.get("fast_decay") else DECAY_LAMBDA
    is_auto = "auto_capture" in source_platform
    auto_accelerated = bool(is_auto and activations == 0 and days > 3)
    if auto_accelerated:
        lam *= 2.0

    activation_factor = max(activations, 1) ** 0.3
    emotion_weight = 1.0 + (arousal * 0.8)
    base_score = importance * activation_factor * emotion_weight
    score = min(1.0, base_score * math.exp(-lam * days))

    protections = []
    pressures = []
    if anchored:
        protections.append("anchored")
    if room == "living_room":
        protections.append("living_room")
    if importance >= 0.8:
        protections.append("high_importance")
    if activations >= 3:
        protections.append("often_recalled")
    if arousal >= 0.65:
        protections.append("emotionally_strong")

    if importance < 0.5:
        pressures.append("low_importance")
    if activations == 0:
        pressures.append("never_recalled")
    if room_cfg.get("fast_decay"):
        pressures.append("fast_decay_room")
    if auto_accelerated:
        pressures.append("auto_capture_unrecalled")
    if days >= 30:
        pressures.append("old")

    if anchored:
        lane = "protected"
        recommendation = "keep"
        will_archive = False
    elif room == "living_room":
        lane = "long_term"
        recommendation = "refresh_if_outdated" if "old" in pressures else "keep"
        will_archive = False
    elif room_cfg.get("fast_decay") or (is_auto and importance < 0.65 and activations == 0):
        lane = "short_term"
        recommendation = "archive_soon" if score < DECAY_THRESHOLD else "let_decay"
        will_archive = score < DECAY_THRESHOLD
    elif importance >= 0.75 or activations >= 2:
        lane = "long_term"
        recommendation = "keep"
        will_archive = False
    else:
        lane = "watch"
        recommendation = "review" if score < DECAY_THRESHOLD * 1.5 else "let_decay"
        will_archive = score < DECAY_THRESHOLD

    days_to_archive = None
    if will_archive:
        days_to_archive = 0.0
    elif lane in ("short_term", "watch") and not anchored and room != "living_room" and score > DECAY_THRESHOLD and lam > 0 and base_score > 0:
        target_days = math.log(max(base_score, 0.0001) / DECAY_THRESHOLD) / lam
        days_to_archive = max(0.0, target_days - days)

    health = "healthy" if score >= 0.6 else "decaying" if score >= DECAY_THRESHOLD else "critical"
    return {
        "current_score": round(score, 4),
        "threshold": DECAY_THRESHOLD,
        "days_alive": round(days, 1),
        "days_to_archive": None if days_to_archive is None else round(days_to_archive, 1),
        "will_archive": will_archive,
        "health": health,
        "lane": lane,
        "recommendation": recommendation,
        "protections": protections,
        "pressures": pressures,
        "protection_reasons": [PROTECTION_REASON_TEXT[p] for p in protections if p in PROTECTION_REASON_TEXT],
        "pressure_reasons": [PRESSURE_REASON_TEXT[p] for p in pressures if p in PRESSURE_REASON_TEXT],
        "lane_reason": _lane_reason(lane, protections, pressures),
        "factors": {
            "importance": round(importance, 3),
            "activation_count": activations,
            "activation_factor": round(activation_factor, 3),
            "emotion_arousal": round(arousal, 3),
            "emotion_weight": round(emotion_weight, 3),
            "lambda": round(lam, 4),
            "auto_accelerated": auto_accelerated,
            "fast_decay_room": bool(room_cfg.get("fast_decay")),
        },
    }


async def run_decay() -> dict:
    now_dt = datetime.now(timezone.utc)
    archived = 0
    decayed = 0

    for mem in database.iter_memories(status="active"):
        if mem.get("anchored"):
            continue

        try:
            created = datetime.fromisoformat(mem["created_at"])
            days = (now_dt - created).total_seconds() / 86400
        except Exception:
            continue

        importance = _safe_float(mem.get("importance"), 0.5)
        arousal = _safe_float(mem.get("emotion_arousal"), 0.3)
        activations = int(_safe_float(mem.get("activation_count"), 0))

        room_cfg = get_room(mem.get("room", "")) or {}
        lam = DECAY_LAMBDA_FAST if room_cfg.get("fast_decay") else DECAY_LAMBDA

        # 从未被召回的自动记忆加速衰减
        is_auto = "auto_capture" in (mem.get("source_platform") or "")
        if is_auto and activations == 0 and days > 3:
            lam *= 2.0

        # 高唤醒记忆衰减更慢
        emotion_weight = 1.0 + (arousal * 0.8)
        new_score = importance * (max(activations, 1) ** 0.3) * math.exp(-lam * days) * emotion_weight
        new_score = min(new_score, 1.0)

        decay = explain_decay(mem, now_dt=now_dt)
        if decay.get("will_archive"):
            mem["status"] = "archived"
            mem["decay_score"] = new_score
            mem["updated_at"] = _now()
            store.set_memory(mem)
            archived += 1
        elif abs(new_score - _safe_float(mem.get("decay_score"), 1.0)) > 0.01:
            mem["decay_score"] = new_score
            mem["updated_at"] = _now()
            store.set_memory(mem)
            decayed += 1

    return {"archived": archived, "decayed": decayed}


# ── 导出 ──

async def export_all() -> dict:
    memories = []
    for m in database.iter_memories(status=None):
        if m.get("status") == "deleted":
            continue
        clean = {k: v for k, v in m.items() if k not in ("embedding",)}
        memories.append(clean)
    memories.sort(key=lambda x: x.get("created_at", ""))
    return {"exported_at": _now(), "count": len(memories), "memories": memories}


# ── 锚点系统 ──

ANCHOR_MAX = 20

async def anchor_memory(memory_id: str) -> dict:
    """将记忆设为锚点——不衰减、不随机浮出，但可搜索。最多 20 条。"""
    mem = store.get_memory(memory_id)
    if not mem:
        return {"error": f"Memory {memory_id} not found"}
    if mem.get("anchored"):
        return {"status": "already_anchored", "id": memory_id}

    anchor_count = sum(1 for m in database.iter_memories(status="active") if m.get("anchored"))
    if anchor_count >= ANCHOR_MAX:
        return {"error": f"Anchor limit reached ({ANCHOR_MAX}). Release an existing anchor first."}

    mem["anchored"] = True
    mem["updated_at"] = _now()
    store.set_memory(mem)
    return {"status": "anchored", "id": memory_id, "content_preview": mem["content"][:100]}


async def release_anchor(memory_id: str) -> dict:
    """解除锚点，记忆恢复正常衰减。"""
    mem = store.get_memory(memory_id)
    if not mem:
        return {"error": f"Memory {memory_id} not found"}
    if not mem.get("anchored"):
        return {"status": "not_anchored", "id": memory_id}

    mem["anchored"] = None
    mem["updated_at"] = _now()
    store.set_memory(mem)
    return {"status": "released", "id": memory_id}


async def list_anchors() -> list[dict]:
    """列出所有锚点记忆。"""
    anchors = []
    for mem in database.iter_memories(status="active"):
        if mem.get("anchored"):
            anchors.append({
                "id": mem["id"],
                "content": mem["content"],
                "room": mem.get("room", ""),
                "category": mem.get("category", ""),
                "importance": _safe_float(mem.get("importance"), 0.5),
                "created_at": mem.get("created_at", ""),
                "owner_ai": mem.get("owner_ai", ""),
            })
    return anchors


# ── 按标签搜索 ──

async def search_by_tags(
    tags: list[str],
    mode: str = "any",
    room: str = "",
    status: str = "active",
    limit: int = 20,
) -> list[dict]:
    """按标签搜索记忆。mode="any" 匹配任一标签，mode="all" 要求全部匹配。"""
    results = []
    search_lower = [t.lower() for t in tags]

    for mem in database.iter_memories(status=status):
        if room and mem.get("room") != room:
            continue
        mem_tags = [t.lower() for t in _parse_json_field(mem.get("tags", "[]"))]
        if mode == "all":
            if all(any(st in mt for mt in mem_tags) for st in search_lower):
                results.append(mem)
        else:
            if any(any(st in mt for mt in mem_tags) for st in search_lower):
                results.append(mem)
        if len(results) >= limit:
            break

    return [
        {"id": m["id"], "content": m["content"], "room": m.get("room", ""),
         "category": m.get("category", ""), "tags": _parse_json_field(m.get("tags", "[]")),
         "importance": _safe_float(m.get("importance"), 0.5),
         "created_at": m.get("created_at", "")}
        for m in results
    ]


# ── 批量存储 ──

async def batch_remember(
    memories: list[dict],
    source_ai: str = "",
) -> dict:
    """批量存储多条记忆，共享分析流程。"""
    created = 0
    merged = 0
    skipped = 0
    results = []
    for item in memories:
        r = await remember(
            content=item["content"],
            room=item.get("room", "living_room"),
            category=item.get("category", ""),
            importance=item.get("importance", 0.5),
            source_ai=source_ai or item.get("source_ai", ""),
            source_platform="mcp",
            tags=item.get("tags"),
            event_date=item.get("event_date", ""),
            force_create=item.get("force_create", False),
        )
        status = r.get("status", "")
        if status == "created":
            created += 1
        elif status in ("merged", "merged_into_existing"):
            merged += 1
        elif status == "dedup_skipped":
            skipped += 1
        results.append(r)
    return {"total": len(results), "created": created, "merged": merged, "skipped": skipped, "items": results}


