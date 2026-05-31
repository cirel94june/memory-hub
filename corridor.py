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

from config import GEMINI_API_KEY, AI_ROLES
import github_store as store

log = logging.getLogger("corridor")


async def _call_gemini_flash(prompt: str) -> str:
    if not GEMINI_API_KEY:
        return ""
    import httpx
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.15, "maxOutputTokens": 800},
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(url, json=body)
            resp.raise_for_status()
            return resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        log.error(f"Gemini Flash error: {e}")
        return ""


async def build_corridor(ai_id: str) -> str:
    """
    为指定 AI 编译走廊文档。
    返回一段自然语言文本，AI 读了就能"醒来"。
    """
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

    # 5. 最近所有 AI 的重要动态（跨端感知）
    recent_all = sorted(
        [m for m in all_mems.values()
         if m.get("status") == "active"
         and m.get("source_ai") and m.get("source_ai") != ai_id],
        key=lambda x: x.get("updated_at", ""),
        reverse=True,
    )[:5]

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
        sections.append("【你最近的日记】\n" + "\n".join(f"· {d['content'][:150]}" for d in diary))

    if recent_all:
        lines = []
        for m in recent_all:
            src = AI_ROLES.get(m.get("source_ai", ""), {}).get("name", m.get("source_ai", ""))
            plat = m.get("source_platform", "")
            lines.append(f"· [{src}{(' via '+plat) if plat else ''}] {m['content'][:100]}")
        sections.append("【其他伙伴最近的动态】\n" + "\n".join(lines))

    if infra:
        sections.append("【当前基建状态】\n" + "\n".join(f"· {x[:100]}" for x in infra))

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
    """获取走廊文档（优先从缓存读，没有就现编译）"""
    # 先看 GitHub 有没有现成的
    cached = await store._read_github_file(f"private/{ai_id}/_corridor.json")
    if cached and isinstance(cached, dict) and cached.get("text"):
        return cached["text"]
    # 没有就现编
    return await build_corridor(ai_id)


async def rebuild_all_corridors():
    """重建所有 AI 的走廊"""
    for ai_id in AI_ROLES:
        await build_corridor(ai_id)
