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
                    MERGE_SIMILARITY, ROOMS, get_room)
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
) -> dict:
    """写入一条新记忆，自动打标 + 智能关系检测（更新/取代/合并/新建）"""
    # 归一化 AI 别名（cloudy → claude）
    from config import AI_ALIASES
    source_ai = AI_ALIASES.get(source_ai, source_ai)
    owner_ai = AI_ALIASES.get(owner_ai, owner_ai)

    if quick:
        auto_merge = False
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
    if auto_merge:
        # 先找相似候选
        query_vec = await get_embedding(content)
        candidates = _find_similar_candidates(query_vec, domain, threshold=0.55, top_k=5) if query_vec else []

        if candidates:
            # 高相似度（>= MERGE_SIMILARITY）走传统合并
            best = candidates[0]
            if best["score"] >= MERGE_SIMILARITY:
                merge_result = await _try_merge(content, domain, tags, importance, valence, emotion_arousal)
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

                return {
                    "id": mem_id,
                    "status": "created",
                    "category": category,
                    "domain": domain,
                    "superseded": superseded_ids,
                    "linked": linked_ids,
                }

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
    return {"id": mem_id, "status": "created", "category": category, "domain": domain}


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
                     valence: float, arousal: float) -> dict | None:
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

    logger.info(f"Merged into {best_mem['id']} (score={best_score:.3f})")
    return {"id": best_mem["id"], "status": "merged", "score": round(best_score, 3)}


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
        elif r.get("status") == "merged":
            merged += 1
        results.append(r)

    return {"total": len(results), "created": created, "merged": merged, "items": results}


# ── 更新记忆 ──

async def update_memory(memory_id: str, content: str = None, importance: float = None,
                        room: str = None, category: str = None, tags: list[str] = None,
                        owner_ai: str = None, layer: str = None, changed_by: str = "") -> dict:
    mem = store.get_memory(memory_id)
    if not mem:
        return {"id": memory_id, "status": "not_found"}

    now = _now()
    mem["updated_at"] = now

    if content is not None:
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
) -> list[dict]:
    """混合搜索召回记忆（向量 + BM25 FTS + 精确匹配，RRF 融合）

    三路并行搜索，Reciprocal Rank Fusion 融合排序：
    1. 向量路：database.vector_search（语义匹配）
    2. 关键词路：database.fts_search（BM25 全文搜索）
    3. 精确路：SQL LIKE + Python post-filter（最强信号）

    + unresolved 记忆优先浮现（最多 2 条）
    """
    query_vec = await get_embedding(query)

    # 如果没指定 domain，用 analyzer 快速分析 query
    if not query_domain:
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
            "source_ai": mem.get("source_ai", ""),
            "source_context": mem.get("source_context", ""),
        }

    def _passes_private_filter(mem):
        """Check private layer access — must be done in Python since
        database queries don't enforce cross-field logic like this."""
        if mem.get("layer") == "private":
            if not ai_id or mem.get("owner_ai") != ai_id:
                return False
        return True

    # ── 路径 1：向量搜索（语义匹配）──
    vec_results = []
    if query_vec:
        raw_vec = database.vector_search(query_vec, top_k=50, status="active", **db_kwargs)

        vec_scored = []
        for mem in raw_vec:
            if not _passes_private_filter(mem):
                continue

            distance = mem.pop("distance", 0.0)
            vec_sim = _distance_to_cosine(distance)

            # 多维加权：向量为主，emotion/time/importance 辅助
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

    # ── 路径 2：全文搜索（FTS5 BM25）──
    raw_fts = database.fts_search(query, top_k=50, status="active")
    kw_scored = []
    for mem in raw_fts:
        if not _passes_private_filter(mem):
            continue
        # Apply room filters in Python (FTS doesn't support room filters)
        room = mem.get("room", "")
        if exclude_isolated and room in isolated_rooms:
            continue
        if include_rooms and room not in include_rooms:
            continue

        rank = mem.pop("rank", 0.0)
        # FTS5 rank is negative (lower = better match); normalize to 0..1
        bm25 = min(1.0, abs(rank) / 10.0) if rank else 0.0
        if bm25 > 0.01:
            kw_scored.append((mem, bm25))

    kw_scored.sort(key=lambda x: x[1], reverse=True)
    kw_results = [_build_result(m, s) for m, s in kw_scored[:50]]

    # ── 路径 3：精确匹配（post-filter on candidates from paths 1+2） ──
    # Collect all unique candidate mems from vector + FTS paths
    seen_ids = set()
    exact_candidates = []
    for result_list in (vec_results, kw_results):
        for item in result_list:
            mid = item["id"]
            if mid not in seen_ids:
                seen_ids.add(mid)
                mem = store.get_memory(mid)
                if mem:
                    exact_candidates.append(mem)

    # Note: exact match scoring runs on candidates already gathered from vec+FTS paths.
    # FTS5 should catch most content-matching memories. If needed, a SQL LIKE query
    # could be added to database.py in the future for truly exact substring matches.

    exact_scored = []
    for mem in exact_candidates:
        exact = _exact_match_score(query, mem)
        if exact > 0.3:
            exact_scored.append((mem, exact))

    exact_scored.sort(key=lambda x: x[1], reverse=True)
    exact_results = [_build_result(m, s) for m, s in exact_scored[:50]]

    # ── RRF 融合三路结果 ──
    merged = _rrf_merge(vec_results, kw_results, exact_results)

    # ── Unresolved 优先浮现（最多 2 条插到最前面）──
    unresolved = []
    normal = []
    for item in merged:
        mem = store.get_memory(item["id"])
        if mem and mem.get("resolved") == False and len(unresolved) < 2:
            item["_unresolved"] = True
            unresolved.append(item)
        else:
            normal.append(item)

    results = (unresolved + normal)[:top_k]

    # ── Touch：更新 activation_count + 时间涟漪 ──
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
        # 清理内部标记
        r.pop("_unresolved", None)
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
    status: str = "active", page: int = 1, per_page: int = 20,
) -> dict:
    mems = database.query_memories(
        layer=layer, room=room, owner_ai=owner_ai, status=status,
        limit=per_page, offset=(page - 1) * per_page,
        order_by="updated_at DESC",
    )
    total = database.count_memories(status=status)

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


# ── 记忆衰减（含情感维度） ──

async def run_decay() -> dict:
    now_dt = datetime.now(timezone.utc)
    archived = 0
    decayed = 0

    for mem in database.iter_memories(status="active"):
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

        if new_score < DECAY_THRESHOLD and mem.get("room") != "living_room":
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
