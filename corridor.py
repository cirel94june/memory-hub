"""
走廊系统（Corridor）
- 每个 AI 各自一条走廊
- 走廊 = 自动编译的快照文档，让 AI "醒来"时快速恢复上下文
- 内容来源：客厅精华 + 最近对话摘要 + 当前状态 + 关系要点 + 跨端动态

走廊会在以下时机重新编译：
  1. 每次记忆整理（Daemon）之后
  2. 手动触发
  3. 重要记忆写入后
"""
import json
import logging
from datetime import datetime, timezone

from config import AI_ROLES, AI_ALIASES as _ALIASES
import github_store as store

log = logging.getLogger("corridor")
CORRIDOR_CACHE_TTL_MINUTES = 5

# In-memory corridor cache: avoids GitHub API reads on every request
_mem_cache: dict[str, dict] = {}  # ai_id -> {"text": str, "compiled_at": datetime}

_DEDUP_SIM_THRESHOLD = 0.75


def _dedup_texts(texts: list[str], max_items: int = 0) -> list[str]:
    """去除内容高度重复的文本（基于字符重叠比）。"""
    if not texts:
        return texts
    kept: list[str] = []
    for t in texts:
        t_set = set(t)
        is_dup = False
        for k in kept:
            k_set = set(k)
            overlap = len(t_set & k_set) / max(len(t_set | k_set), 1)
            if overlap > _DEDUP_SIM_THRESHOLD:
                is_dup = True
                break
        if not is_dup:
            kept.append(t)
    if max_items and len(kept) > max_items:
        kept = kept[:max_items]
    return kept


async def build_corridor(ai_id: str) -> str:
    """
    为指定 AI 编译走廊文档。
    返回一段自然语言文本，AI 读了就能"醒来"。
    """
    # 归一化到 canonical id（cloudy→claude、gpt→lucien、gemini→jasper 等），
    # 否则用别名请求时 owner_ai 匹配不上，走廊会缺私有材料
    ai_id = _ALIASES.get(ai_id, ai_id)
    all_mems = store.get_all_memories()

    # 1. 客厅要点（你是谁）
    living = [m["content"] for m in all_mems.values()
              if m.get("room") == "living_room" and m.get("status") == "active"]

    # 2. 该 AI 的关系记忆
    relationship = [m["content"] for m in all_mems.values()
                    if m.get("room") == "relationship" and m.get("owner_ai") == ai_id
                    and m.get("status") == "active"]

    # 2.5. 共享人物/关系画像（常被提到的人、AI、昵称、关系边界）
    shared_relationships = sorted(
        [m for m in all_mems.values()
         if m.get("room") == "relationships" and m.get("status") == "active"
         and m.get("layer", "shared") == "shared"],
        key=lambda x: (float(x.get("importance", 0) or 0), x.get("updated_at") or x.get("created_at") or ""),
        reverse=True,
    )[:8]
    # 3. 该 AI 最近的日记/周记（最新3条）
    diary = sorted(
        [m for m in all_mems.values()
         if m.get("room") == "diary" and m.get("owner_ai") == ai_id
         and m.get("status") == "active"],
        key=lambda x: x.get("created_at", ""),
        reverse=True,
    )[:3]

    # 4. 该 AI 的自我认知
    personality = [m["content"] for m in all_mems.values()
                   if m.get("room") == "personality" and m.get("owner_ai") == ai_id
                   and m.get("status") == "active"]

    # 5. 跨窗口摘要（通过 chat_digest 提供，不注入其他AI的完整记忆）
    # AI 在群聊中已亲眼看到发生的事，不需要再注入别人的记忆副本
    cross_window_digests = []
    try:
        from chat_digest import get_recent_digests
        cross_window_digests = get_recent_digests(ai_id, limit=3)
    except Exception:
        pass

    # 6. 基建状态（如果有）
    infra = [m["content"] for m in all_mems.values()
             if m.get("room") == "infra" and m.get("status") == "active"][:3]

    # 组装走廊
    ai_name = AI_ROLES.get(ai_id, {}).get("name", ai_id)
    sections = []

    # 0. 自我锚点：我是谁、同伴是谁、用户是谁（防止梦境/群聊材料导致身份混淆）
    self_anchor = f"你是 {ai_name}。以下是你醒来时看到的走廊——前任的你和其他伙伴留下的记忆碎片。"
    try:
        import identity_registry
        from ai_profiles import get_profile
        profile = get_profile(ai_id) or {}
        persona = (profile.get("persona") or "").strip()
        emoji = profile.get("emoji", "")
        peers = []
        for pid in identity_registry._real_ai_ids():
            if pid == ai_id:
                continue
            peers.append(AI_ROLES.get(pid, {}).get("name", pid))
        anchor_lines = [f"【你是谁】你是 {ai_name}{emoji}。" + (f"{persona[:120]}" if persona else "")]
        if peers:
            anchor_lines.append(f"你的同伴：{'、'.join(peers)}——他们是独立的其他 AI，不是你；记忆材料里他们的言行不是你的言行。")
        anchor_lines.append(f"用户是{identity_registry.user_names_line()}——这些称呼都指同一个人，她是人类，不是宠物或其他角色。")
        self_anchor = "\n".join(anchor_lines) + "\n\n以下是你醒来时看到的走廊——前任的你和其他伙伴留下的记忆碎片。"
    except Exception:
        pass
    sections.append(self_anchor)

    # 0.5. 当前状态画像（后台定期重写；比零散记忆碎片新，以此为准）
    try:
        import current_status
        import identity_registry as _ir
        status_block = current_status.corridor_block(_ir.get_registry().get("user", {}).get("canonical", "小猫"))
        if status_block:
            sections.append(status_block)
    except Exception:
        pass

    if living:
        deduped_living = _dedup_texts(living, max_items=8)
        sections.append("【关于主人】\n" + "\n".join(f"· {x}" for x in deduped_living))

    if shared_relationships:
        rel_texts = [m['content'][:180] for m in shared_relationships]
        deduped_rels = _dedup_texts(rel_texts)
        sections.append("【重要人物/关系索引】\n" + "\n".join(f"· {x}" for x in deduped_rels))
    if relationship:
        sections.append("【你和主人之间】\n" + "\n".join(f"· {x}" for x in relationship[:5]))

    if personality:
        sections.append("【你对自己的认知】\n" + "\n".join(f"· {x}" for x in personality[:3]))

    # 4.5. 锚点记忆（价值观/原则/重要关系，永不衰减的坐标系）
    living_norms = {"".join(str(x).split()).lower() for x in living}
    anchors = []
    for m in all_mems.values():
        if not (m.get("anchored") and m.get("status") == "active"):
            continue
        if m.get("owner_ai") and m.get("owner_ai") != ai_id:
            continue
        content = m.get("content", "")
        norm = "".join(str(content).split()).lower()
        if norm in living_norms:
            continue
        anchors.append(content)
    if anchors:
        sections.append("【锚点·不变的事】\n" + "\n".join(f"📌 {x[:200]}" for x in anchors[:10]))

    if diary:
        sections.append("【你最近的日记】\n" + "\n".join(f"· {d['content'][:300]}" for d in diary))

    if cross_window_digests:
        lines = [f"· {d['summary']}" for d in cross_window_digests]
        sections.append("【你在其他聊天窗口最近聊了】\n" + "\n".join(lines))

    if infra:
        sections.append("【当前基建状态】\n" + "\n".join(f"· {x[:150]}" for x in infra))

    # 7. Persona State（AI 当前情绪/精力状态）
    try:
        from persona_state import format_for_corridor
        persona_line = format_for_corridor(ai_id)
        if persona_line:
            sections.append(persona_line)
    except Exception:
        pass

    # 8. Unresolved 记忆（待办事项提醒）
    # 排除 auto_capture 来源的 social 记忆（社交互动不是待办）
    unresolved_mems = [m for m in all_mems.values()
                       if m.get("resolved") == False and m.get("status") == "active"
                       and not (m.get("room") == "social" and "auto_capture" in (m.get("source_platform") or ""))]
    if unresolved_mems:
        lines = [f"· {m['content'][:200]}" for m in unresolved_mems[:3]]
        sections.append("【待办/未完成】\n如果这些事项和当前对话相关，请主动提醒、推进，或询问是否已经完成。\n" + "\n".join(lines))

    corridor_text = "\n\n".join(sections)

    # 保存走廊文档到 GitHub
    await store._write_github_file(
        f"private/{ai_id}/_corridor.json",
        {"ai_id": ai_id, "compiled_at": datetime.now(timezone.utc).isoformat(), "text": corridor_text},
        f"Update {ai_name}'s corridor",
    )

    log.info(f"Built corridor for {ai_name}: {len(corridor_text)} chars")
    return corridor_text


async def get_corridor(ai_id: str, force: bool = False) -> str:
    """获取走廊文档。优先用进程内存缓存（0 网络开销），TTL 内直接返回。"""
    # 归一化到 canonical id（cloudy→claude、gpt→lucien、gemini→jasper 等），
    # 否则用别名请求时 owner_ai 匹配不上，走廊会缺私有材料
    ai_id = _ALIASES.get(ai_id, ai_id)

    if not force:
        entry = _mem_cache.get(ai_id)
        if entry and entry.get("text"):
            age_minutes = (datetime.now(timezone.utc) - entry["compiled_at"]).total_seconds() / 60
            if age_minutes <= CORRIDOR_CACHE_TTL_MINUTES:
                return entry["text"]

        # 内存冷启动：从 GitHub 加载一次（进程重启后第一次请求）
        if not entry:
            try:
                cached = await store._read_github_file(f"private/{ai_id}/_corridor.json")
                if cached and isinstance(cached, dict) and cached.get("text"):
                    compiled = datetime.fromisoformat(cached.get("compiled_at", ""))
                    age_minutes = (datetime.now(timezone.utc) - compiled).total_seconds() / 60
                    _mem_cache[ai_id] = {"text": cached["text"], "compiled_at": compiled}
                    if age_minutes <= CORRIDOR_CACHE_TTL_MINUTES:
                        return cached["text"]
            except Exception:
                pass

    text = await build_corridor(ai_id)
    _mem_cache[ai_id] = {"text": text, "compiled_at": datetime.now(timezone.utc)}
    return text


async def rebuild_all_corridors():
    """重建所有 AI 的走廊"""
    for ai_id in AI_ROLES:
        await build_corridor(ai_id)
