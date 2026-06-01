"""
记忆操作：增删改查 + 向量搜索 + 衰减
基于内存操作，后台异步推送到 GitHub
"""
import json
import time
import math
import asyncio
from datetime import datetime, timezone

from config import DECAY_LAMBDA, DECAY_LAMBDA_FAST, DECAY_THRESHOLD, ROOMS, get_room
from embedding import get_embedding, pack_embedding, unpack_embedding, cosine_similarity
import github_store as store


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _gen_id() -> str:
    return f"mem_{int(time.time() * 1000)}_{int(time.time_ns() % 10000):04d}"


def _schedule_push():
    """安排一个延迟推送（避免频繁写 GitHub）"""
    asyncio.get_event_loop().call_later(5.0, lambda: asyncio.ensure_future(store.push_dirty()))


# ── 写入记忆 ──

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
) -> dict:
    """写入一条新记忆"""
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
    return {"id": mem_id, "status": "created"}


# ── 更新记忆 ──

async def update_memory(memory_id: str, content: str = None, importance: float = None,
                        room: str = None, category: str = None, tags: list[str] = None,
                        changed_by: str = "") -> dict:
    """更新已有记忆"""
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
        from config import get_room
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

    store.set_memory(mem)
    _schedule_push()
    return {"id": memory_id, "status": "updated"}


# ── 召回记忆（向量搜索） ──

async def recall(
    query: str,
    ai_id: str = "",
    top_k: int = 8,
    threshold: float = 0.35,
    include_rooms: list[str] = None,
    exclude_isolated: bool = True,
) -> list[dict]:
    """智能召回记忆"""
    query_vec = await get_embedding(query)
    if not query_vec:
        return []

    all_mems = store.get_all_memories()
    isolated_rooms = {k for k, v in ROOMS.items() if v.get("isolated")}

    scored = []
    for mem in all_mems.values():
        if mem.get("status") != "active":
            continue
        if not mem.get("embedding"):
            continue

        # 房间过滤
        room = mem.get("room", "")
        if exclude_isolated and room in isolated_rooms:
            continue
        if include_rooms and room not in include_rooms:
            continue

        # 私有权限
        if mem.get("layer") == "private":
            if not ai_id or mem.get("owner_ai") != ai_id:
                continue

        # 计算综合评分
        mem_vec = unpack_embedding(mem["embedding"])
        vec_sim = cosine_similarity(query_vec, mem_vec)
        importance = float(mem.get("importance", 0.5))
        decay = float(mem.get("decay_score", 1.0))
        activation_bonus = min(float(mem.get("activation_count", 0)) / 20.0, 1.0)
        final = vec_sim * 0.55 + importance * 0.2 + decay * 0.15 + activation_bonus * 0.1

        if final >= threshold:
            scored.append({
                "id": mem["id"],
                "content": mem["content"],
                "layer": mem.get("layer", ""),
                "room": room,
                "category": mem.get("category", ""),
                "importance": importance,
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

    # 过滤
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

    # 导出时去掉 embedding 和 history（前端不需要）
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


# ── 记忆衰减 ──

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

        # 检查房间是否快速衰减
        room_cfg = get_room(mem.get("room", "")) or {}
        lam = DECAY_LAMBDA_FAST if room_cfg.get("fast_decay") else DECAY_LAMBDA

        emotion_weight = 0.7 + (arousal * 0.6)
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
