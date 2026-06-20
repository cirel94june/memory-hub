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

from config import AI_ROLES
import github_store as store

log = logging.getLogger("corridor")


async def build_corridor(ai_id: str) -> str:
    """
    为指定 AI 编译走廊文档。
    返回一段自然语言文本，AI 读了就能"醒来"。
    """
    ai_id = {"cloudy": "claude"}.get(ai_id, ai_id)
    all_mems = store.get_all_memories()

    # 1. 客厅要点（你是谁）
    living = [m["content"] for m in all_mems.values()
              if m.get("room") == "living_room" and m.get("status") == "active"]

    # 2. 该 AI 的关系记忆
    relationship = [m["content"] for m in all_mems.values()
                    if m.get("room") == "relationship" and m.get("owner_ai") == ai_id
                    and m.get("status") == "active"]

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

    sections.append(f"你是 {ai_name}。以下是你醒来时看到的走廊——前任的你和其他伙伴留下的记忆碎片。")

    if living:
        sections.append("【关于主人】\n" + "\n".join(f"· {x}" for x in living[:8]))

    if relationship:
        sections.append("【你和主人之间】\n" + "\n".join(f"· {x}" for x in relationship[:5]))

    if personality:
        sections.append("【你对自己的认知】\n" + "\n".join(f"· {x}" for x in personality[:3]))

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
        sections.append("【待办/未完成】\n" + "\n".join(lines))

    corridor_text = "\n\n".join(sections)

    # 保存走廊文档到 GitHub
    await store._write_github_file(
        f"private/{ai_id}/_corridor.json",
        {"ai_id": ai_id, "compiled_at": datetime.now(timezone.utc).isoformat(), "text": corridor_text},
        f"Update {ai_name}'s corridor",
    )

    log.info(f"Built corridor for {ai_name}: {len(corridor_text)} chars")
    return corridor_text


async def get_corridor(ai_id: str) -> str:
    """获取走廊文档（优先从缓存读，超过6小时自动重建）"""
    ai_id = {"cloudy": "claude"}.get(ai_id, ai_id)
    cached = await store._read_github_file(f"private/{ai_id}/_corridor.json")
    if cached and isinstance(cached, dict) and cached.get("text"):
        try:
            compiled = datetime.fromisoformat(cached.get("compiled_at", ""))
            age_hours = (datetime.now(timezone.utc) - compiled).total_seconds() / 3600
            if age_hours <= 1:
                return cached["text"]
            log.info(f"Corridor for {ai_id} is {age_hours:.1f}h old, rebuilding")
        except Exception:
            pass
    return await build_corridor(ai_id)


async def rebuild_all_corridors():
    """重建所有 AI 的走廊"""
    for ai_id in AI_ROLES:
        await build_corridor(ai_id)
