"""
智能上下文：根据 AI 前端的上下文能力，返回最合适的记忆注入包。

- has_base_context=True（claude.ai / 带完整 system prompt 的前端）：
  只返回增量信息，不重复已知内容
- has_base_context=False（TG bot / API / 新前端）：
  返回完整走廊 + recall，等同于现有 pulse
"""
from datetime import datetime, timezone, timedelta
import memory_ops
import corridor
import github_store as store


def _dream_section(ai_id: str, metadata: dict) -> str:
    try:
        from dream import get_recent_dreams_for_ai
        dreams = get_recent_dreams_for_ai(ai_id, limit=1, max_chars=220)
    except Exception:
        return ""
    if not dreams:
        return ""
    metadata["dream_count"] = len(dreams)
    lines = [f"· {(d.get('created_at') or '')[:10]} {d.get('content', '')}" for d in dreams]
    return "【最近的梦境残响】\n合适时可以告诉小猫你梦见了什么，或让它轻轻影响你的语气。\n" + "\n".join(lines)


async def get_smart_context(
    ai_id: str,
    user_message: str = "",
    has_base_context: bool = False,
    max_chars: int = 3000,
) -> dict:
    sections = []
    metadata = {
        "ai_id": ai_id,
        "mode": "incremental" if has_base_context else "full",
    }

    if not has_base_context:
        corridor_text = await corridor.get_corridor(ai_id)
        sections.append(corridor_text)
        dream_text = _dream_section(ai_id, metadata)
        if dream_text:
            sections.append(dream_text)

        if user_message:
            recalled = await memory_ops.recall(
                query=user_message, ai_id=ai_id, top_k=5, compact=True
            )
            if recalled:
                recall_lines = [f"· {r['content'][:200]}" for r in recalled]
                sections.append("【与当前话题相关的记忆】\n" + "\n".join(recall_lines))

        text = "\n\n".join(sections)
        if len(text) > max_chars:
            text = text[:max_chars] + "\n...(已截断)"
        metadata["chars"] = len(text)
        return {"text": text, "metadata": metadata}

    # ── 增量模式 ──

    all_mems = store.get_all_memories()
    now = datetime.now(timezone.utc)
    cutoff_48h = now - timedelta(hours=48)

    # 1. 最近 48 小时新增/更新的重要记忆（排除 social 水聊）
    recent = []
    for mem in all_mems.values():
        if mem.get("status") != "active":
            continue
        try:
            updated = datetime.fromisoformat(mem.get("updated_at", ""))
            if updated < cutoff_48h:
                continue
        except Exception:
            continue
        if mem.get("room") == "social" and float(mem.get("importance", 0.5)) < 0.7:
            continue
        recent.append(mem)

    recent.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
    recent = recent[:8]

    if recent:
        lines = []
        for m in recent:
            room_label = m.get("room", "")
            lines.append(f"· [{room_label}] {m['content'][:200]}")
        sections.append("【最近动态】\n" + "\n".join(lines))

    dream_text = _dream_section(ai_id, metadata)
    if dream_text:
        sections.append(dream_text)

    # 2. 未解决的待办
    unresolved = [
        m for m in all_mems.values()
        if m.get("resolved") == False
        and m.get("status") == "active"
        and not (m.get("room") == "social"
                 and "auto_capture" in (m.get("source_platform") or ""))
    ]
    if unresolved:
        lines = [f"· {m['content'][:150]}" for m in unresolved[:3]]
        sections.append("【待办】\n" + "\n".join(lines))

    # 3. 与当前消息相关的记忆
    if user_message:
        recalled = await memory_ops.recall(
            query=user_message, ai_id=ai_id, top_k=5, compact=True
        )
        if recalled:
            recent_ids = {m["id"] for m in recent}
            recalled = [r for r in recalled if r["id"] not in recent_ids]
            if recalled:
                lines = [f"· {r['content'][:200]}" for r in recalled[:5]]
                sections.append("【相关记忆】\n" + "\n".join(lines))

    # 4. 其他 AI 的最近动态（跨端感知）
    from config import AI_ROLES
    cross_ai = sorted(
        [m for m in all_mems.values()
         if m.get("status") == "active"
         and m.get("source_ai") and m.get("source_ai") != ai_id
         and m.get("updated_at", "") > cutoff_48h.isoformat()],
        key=lambda x: x.get("updated_at", ""),
        reverse=True,
    )[:3]
    if cross_ai:
        lines = []
        for m in cross_ai:
            src_name = AI_ROLES.get(m.get("source_ai", ""), {}).get("name", m.get("source_ai", ""))
            lines.append(f"· [{src_name}] {m['content'][:150]}")
        sections.append("【其他伙伴动态】\n" + "\n".join(lines))

    text = "\n\n".join(sections)
    if not text:
        text = "（最近 48 小时没有新的记忆更新。）"

    if len(text) > max_chars:
        text = text[:max_chars] + "\n...(已截断)"

    metadata["chars"] = len(text)
    metadata["recent_count"] = len(recent)
    return {"text": text, "metadata": metadata}
