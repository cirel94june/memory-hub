"""
记忆操作：增删改查 + 向量搜索 + 衰减 + 自动合并 + 长文拆分
基于内存操作，后台异步推送到 GitHub
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
import analyzer

logger = logging.getLogger("memory_hub.ops")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _gen_id() -> str:
    return f"mem_{int(time.time() * 1000)}_{int(time.time_ns() % 10000):04d}"


def _schedule_push():
    asyncio.get_event_loop().call_later(5.0, lambda: asyncio.ensure_future(store.push_dirty()))


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
    auto_analyze: bool = True,
    auto_merge: bool = True,
) -> dict:
    """写入一条新记忆，自动打标 + 合并检测"""

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

    # Step 2: 合并检测
    if auto_merge:
        merge_result = await _try_merge(content, domain, tags, importance, valence, emotion_arousal)
        if merge_result:
            return merge_result

    # Step 3: 新建记忆
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
        "embedding": pack_embedding(vec) if vec else None,
        "status": "active",
        "created_at": now,
        "updated_at": now,
        "history": [{"v": 1, "content": content, "date": now, "by": source_ai or "system"}],
    }

    store.set_memory(mem)
    _schedule_push()
    return {"id": mem_id, "status": "created", "category": category, "domain": domain}


async def _try_merge(content: str, domain: list, tags: list, importance: float,
                     valence: float, arousal: float) -> dict | None:
    """查找最相似的已有记忆，如果超过阈值则合并"""
    query_vec = await get_embedding(content)
    if not query_vec:
        return None

    best_mem = None
    best_score = 0.0

    for mem in store.get_all_memories().values():
        if mem.get("status") != "active" or not mem.get("embedding"):
            continue

        mem_vec = unpack_embedding(mem["embedding"])
        vec_sim = cosine_similarity(query_vec, mem_vec)

        # domain 匹配加分
        mem_domain = _parse_json_field(mem.get("domain", "[]"))
        domain_bonus = 0.05 if any(d in mem_domain for d in domain) else 0.0

        score = vec_sim + domain_bonus
        if score > best_score:
            best_score = score
            best_mem = mem

    if not best_mem or best_score < MERGE_SIMILARITY:
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
    best_mem["importance"] = max(float(best_mem.get("importance", 0.5)), importance)
    best_mem["emotion_arousal"] = (float(best_mem.get("emotion_arousal", 0.3)) + arousal) / 2
    best_mem["valence"] = (float(best_mem.get("valence", 0.5)) + valence) / 2

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
    _schedule_push()

    logger.info(f"Merged into {best_mem['id']} (score={best_score:.3f})")
    return {"id": best_mem["id"], "status": "merged", "score": round(best_score, 3)}


# ── 长文拆分（grow） ──

async def grow(
    content: str,
    source_ai: str = "",
    auto_merge: bool = True,
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

    _schedule_push()
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
    if room is not None and room != mem.get("room"):
        old_path = store._file_path_for_memory(mem)
        mem["room"] = room
        new_room_cfg = get_room(room) or {}
        if new_room_cfg.get("scope") == "per_ai" and not mem.get("owner_ai"):
            mem["owner_ai"] = changed_by or "claude"
        store._dirty_files.add(old_path)
    elif room is not None:
        mem["room"] = room
    if category is not None:
        mem["category"] = category
    if tags is not None:
        mem["tags"] = json.dumps(tags)
    if owner_ai is not None:
        mem["owner_ai"] = owner_ai
    if layer is not None:
        mem["layer"] = layer

    store.set_memory(mem)
    _schedule_push()
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
    threshold: float = 0.30,
    include_rooms: list[str] = None,
    exclude_isolated: bool = True,
    query_domain: list[str] = None,
    query_valence: float = -1,
    query_arousal: float = -1,
) -> list[dict]:
    """多维搜索召回记忆"""
    query_vec = await get_embedding(query)
    if not query_vec:
        return []

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

    all_mems = store.get_all_memories()
    isolated_rooms = {k for k, v in ROOMS.items() if v.get("type") == "isolated"}

    candidates = []
    for mem in all_mems.values():
        if mem.get("status") != "active":
            continue
        if not mem.get("embedding"):
            continue
        room = mem.get("room", "")
        if exclude_isolated and room in isolated_rooms:
            continue
        if include_rooms and room not in include_rooms:
            continue
        if mem.get("layer") == "private":
            if not ai_id or mem.get("owner_ai") != ai_id:
                continue
        candidates.append(mem)

    # Domain 预筛：如果有 domain，优先匹配的排前面
    scored = []
    for mem in candidates:
        mem_vec = unpack_embedding(mem["embedding"])
        vec_sim = cosine_similarity(query_vec, mem_vec)

        # Topic score: fuzzy match on category + domain + tags + content
        mem_domain = _parse_json_field(mem.get("domain", "[]"))
        mem_tags = _parse_json_field(mem.get("tags", "[]"))
        cat = mem.get("category", "")

        domain_match = _fuzzy_score(
            " ".join(query_domain),
            " ".join(mem_domain)
        ) if query_domain else 0.0
        tag_match = _fuzzy_score(query, " ".join(mem_tags))
        cat_match = _fuzzy_score(query, cat)
        content_match = _fuzzy_score(query, mem.get("content", "")[:200])

        topic_score = (cat_match * 3 + domain_match * 2.5 + tag_match * 2 + content_match * 1) / 8.5

        # Embedding score (normalized to 0-1)
        embed_score = max(0, vec_sim)

        # Emotion score
        if query_valence >= 0 and query_arousal >= 0:
            mv = float(mem.get("valence", 0.5))
            ma = float(mem.get("emotion_arousal", 0.3))
            emotion_score = 1.0 - math.sqrt(((query_valence - mv)**2 + (query_arousal - ma)**2) / 2)
        else:
            emotion_score = 0.5

        # Time score
        try:
            created = datetime.fromisoformat(mem["created_at"])
            days = (datetime.now(timezone.utc) - created).total_seconds() / 86400
            time_score = math.exp(-0.02 * days)
        except Exception:
            time_score = 0.5

        # Importance score
        importance_score = float(mem.get("importance", 0.5))

        # Combined: embedding dominates but other dims help
        final = (
            embed_score * 5.0 +
            topic_score * 4.0 +
            emotion_score * 2.0 +
            time_score * 1.5 +
            importance_score * 1.0
        ) / 13.5

        if final >= threshold:
            scored.append({
                "id": mem["id"],
                "content": mem["content"],
                "layer": mem.get("layer", ""),
                "room": room,
                "category": mem.get("category", ""),
                "domain": mem_domain,
                "importance": float(mem.get("importance", 0.5)),
                "valence": float(mem.get("valence", 0.5)),
                "arousal": float(mem.get("emotion_arousal", 0.3)),
                "score": round(final, 4),
                "created_at": mem.get("created_at", ""),
            })

    scored.sort(key=lambda x: x["score"], reverse=True)
    results = scored[:top_k]

    # 更新 activation_count
    now = _now()
    for r in results:
        m = store.get_memory(r["id"])
        if m:
            m["activation_count"] = m.get("activation_count", 0) + 1
            m["last_activated"] = now
            store.set_memory(m)

    if results:
        _schedule_push()

    return results


# ── 获取客厅内容 ──

async def get_living_room() -> list[dict]:
    all_mems = store.get_all_memories()
    items = [
        {"id": m["id"], "content": m["content"], "category": m.get("category", ""),
         "importance": m.get("importance", 0.5), "updated_at": m.get("updated_at", "")}
        for m in all_mems.values()
        if m.get("room") == "living_room" and m.get("status") == "active"
    ]
    items.sort(key=lambda x: (-x["importance"], x["updated_at"]))
    return items


# ── AI 私有记忆概要 ──

async def get_ai_private_summary(ai_id: str, limit: int = 10) -> list[dict]:
    all_mems = store.get_all_memories()
    items = [
        {"id": m["id"], "content": m["content"], "category": m.get("category", ""),
         "importance": m.get("importance", 0.5), "created_at": m.get("created_at", "")}
        for m in all_mems.values()
        if m.get("layer") == "private" and m.get("owner_ai") == ai_id and m.get("status") == "active"
    ]
    items.sort(key=lambda x: -x["importance"])
    return items[:limit]


# ── 列出记忆 ──

async def list_memories(
    layer: str = None, room: str = None, owner_ai: str = None,
    status: str = "active", page: int = 1, per_page: int = 20,
) -> dict:
    all_mems = list(store.get_all_memories().values())
    filtered = []
    for m in all_mems:
        if layer and m.get("layer") != layer:
            continue
        if room and m.get("room") != room:
            continue
        if owner_ai and m.get("owner_ai") != owner_ai:
            continue
        if status and m.get("status") != status:
            continue
        filtered.append(m)

    filtered.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
    total = len(filtered)
    start = (page - 1) * per_page
    items = filtered[start:start + per_page]

    clean = []
    for m in items:
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
        _schedule_push()
    return {"id": memory_id, "status": "archived"}


async def delete_memory(memory_id: str) -> dict:
    store.remove_memory(memory_id)
    _schedule_push()
    return {"id": memory_id, "status": "deleted"}


# ── 记忆衰减（含情感维度） ──

async def run_decay() -> dict:
    now_dt = datetime.now(timezone.utc)
    archived = 0
    decayed = 0

    for mem in list(store.get_all_memories().values()):
        if mem.get("status") != "active":
            continue

        try:
            created = datetime.fromisoformat(mem["created_at"])
            days = (now_dt - created).total_seconds() / 86400
        except Exception:
            continue

        importance = float(mem.get("importance", 0.5))
        arousal = float(mem.get("emotion_arousal", 0.3))
        activations = int(mem.get("activation_count", 0))

        room_cfg = get_room(mem.get("room", "")) or {}
        lam = DECAY_LAMBDA_FAST if room_cfg.get("fast_decay") else DECAY_LAMBDA

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
        elif abs(new_score - float(mem.get("decay_score", 1.0))) > 0.01:
            mem["decay_score"] = new_score
            mem["updated_at"] = _now()
            store.set_memory(mem)
            decayed += 1

    await store.push_dirty()
    return {"archived": archived, "decayed": decayed}


# ── 导出 ──

async def export_all() -> dict:
    all_mems = store.get_all_memories()
    memories = []
    for m in all_mems.values():
        if m.get("status") == "deleted":
            continue
        clean = {k: v for k, v in m.items() if k not in ("embedding",)}
        memories.append(clean)
    memories.sort(key=lambda x: x.get("created_at", ""))
    return {"exported_at": _now(), "count": len(memories), "memories": memories}
