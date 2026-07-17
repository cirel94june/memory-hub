"""
智能上下文：根据 AI 前端的上下文能力，返回最合适的记忆注入包。

- has_base_context=True（claude.ai / 带完整 system prompt 的前端）：
  只返回增量信息，不重复已知内容
- has_base_context=False（TG bot / API / 新前端）：
  返回完整走廊 + recall，等同于现有 pulse

隐私：所有候选记忆在生成阶段就用 visibility.can_view 过滤
（private 只有 owner 本人可见），输出前再做一道最终保险。
预算：与当前问题相关的记忆有保留预算，固定走廊不允许占满整个 max_chars。
"""
from datetime import datetime, timezone, timedelta
import memory_ops
import corridor
import github_store as store
from visibility import can_view, filter_visible


def _dream_section(ai_id: str, metadata: dict) -> str:
    try:
        from dream import get_recent_dreams_for_ai
        dreams = get_recent_dreams_for_ai(ai_id, limit=1, max_chars=600)
    except Exception:
        return ""
    if not dreams:
        return ""
    # 梦是 private 材料，get_recent_dreams_for_ai 按 owner 查询；这里再保险一次
    dreams = filter_visible(dreams, ai_id)
    if not dreams:
        return ""
    metadata["dream_count"] = len(dreams)
    lines = [f"· {(d.get('created_at') or '')[:10]} {d.get('content', '')}" for d in dreams]
    return "【最近的梦境残响】\n合适时可以告诉小猫你梦见了什么，或让它轻轻影响你的语气。\n" + "\n".join(lines)


def _fit_sections(sections: list[str], max_chars: int) -> str:
    """按顺序装入 sections，装不下的整段丢弃（保序，先来的优先级高）。
    避免旧行为：拼完再一刀切，把排在末尾的重要内容砍成半句。"""
    out = []
    used = 0
    for s in sections:
        if not s:
            continue
        cost = len(s) + (2 if out else 0)
        if used + cost <= max_chars:
            out.append(s)
            used += cost
        elif not out:
            # 第一段就超预算：截断保底，保证至少有内容
            out.append(s[:max_chars] + "\n...(已截断)")
            used = max_chars
            break
    return "\n\n".join(out)


async def get_smart_context(
    ai_id: str,
    user_message: str = "",
    has_base_context: bool = False,
    max_chars: int = 3000,
) -> dict:
    metadata = {
        "ai_id": ai_id,
        "mode": "incremental" if has_base_context else "full",
    }

    # ── 全量模式（无基础上下文的前端）──
    if not has_base_context:
        # 先算「与当前问题相关」区，为它保留预算——这是新窗口接上话头的关键，
        # 不能让固定走廊占满后被截掉
        recall_text = ""
        if user_message:
            recalled = await memory_ops.recall(
                query=user_message, ai_id=ai_id, top_k=5
            )
            recalled = filter_visible(recalled, ai_id)
            if recalled:
                recall_lines = [f"· {r['content'][:200]}" for r in recalled]
                recall_text = "【与当前话题相关的记忆】\n" + "\n".join(recall_lines)

        dream_text = _dream_section(ai_id, metadata)

        reserved = len(recall_text) + len(dream_text) + 32  # 含分隔符余量
        corridor_budget = max(max_chars - reserved, max_chars // 2)
        corridor_text = await corridor.get_corridor(ai_id)
        if corridor_text and len(corridor_text) > corridor_budget:
            marker = "\n...(走廊已截断)"
            corridor_text = corridor_text[:max(corridor_budget - len(marker), 0)] + marker

        text = _fit_sections([corridor_text, recall_text, dream_text], max_chars)
        metadata["chars"] = len(text)
        return {"text": text, "metadata": metadata}

    # ── 增量模式 ──

    all_mems = store.get_all_memories()
    now = datetime.now(timezone.utc)
    cutoff_48h = now - timedelta(hours=48)

    # 1. 与当前消息相关的记忆（最优先——回答「用户刚才让我做什么」）
    recall_section = ""
    recalled_ids: set = set()
    if user_message:
        recalled = await memory_ops.recall(
            query=user_message, ai_id=ai_id, top_k=5
        )
        recalled = filter_visible(recalled, ai_id)
        if recalled:
            recalled_ids = {r["id"] for r in recalled}
            lines = [f"· {r['content'][:200]}" for r in recalled[:5]]
            recall_section = "【相关记忆】\n" + "\n".join(lines)

    # 2. 未解决的待办（可见性过滤）
    unresolved = [
        m for m in all_mems.values()
        if m.get("resolved") == False
        and m.get("status") == "active"
        and can_view(m, ai_id)
        and not (m.get("room") == "social"
                 and "auto_capture" in (m.get("source_platform") or ""))
    ]
    todo_section = ""
    if unresolved:
        lines = [f"· {m['content'][:150]}" for m in unresolved[:3]]
        todo_section = "【待办】\n" + "\n".join(lines)

    # 3. 最近 48 小时新增/更新的重要记忆（排除 social 水聊；私有只看自己的）
    recent = []
    for mem in all_mems.values():
        if mem.get("status") != "active":
            continue
        if not can_view(mem, ai_id):
            continue
        try:
            updated = datetime.fromisoformat(mem.get("updated_at", ""))
            if updated < cutoff_48h:
                continue
        except Exception:
            continue
        if mem.get("room") == "social" and float(mem.get("importance", 0.5)) < 0.7:
            continue
        if mem["id"] in recalled_ids:
            continue
        recent.append(mem)

    recent.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
    recent = recent[:8]

    recent_section = ""
    if recent:
        lines = []
        for m in recent:
            room_label = m.get("room", "")
            lines.append(f"· [{room_label}] {m['content'][:200]}")
        recent_section = "【最近动态】\n" + "\n".join(lines)

    dream_text = _dream_section(ai_id, metadata)

    # 4. 其他 AI 的最近动态（跨端感知）——只允许 shared 层，
    #    别人的 private（梦/日记）绝不能出现在这里
    from config import AI_ROLES
    cross_ai = sorted(
        [m for m in all_mems.values()
         if m.get("status") == "active"
         and (m.get("layer") or "shared") == "shared"
         and m.get("source_ai") and m.get("source_ai") != ai_id
         and m.get("updated_at", "") > cutoff_48h.isoformat()],
        key=lambda x: x.get("updated_at", ""),
        reverse=True,
    )[:3]
    cross_section = ""
    if cross_ai:
        lines = []
        for m in cross_ai:
            src_name = AI_ROLES.get(m.get("source_ai", ""), {}).get("name", m.get("source_ai", ""))
            lines.append(f"· [{src_name}] {m['content'][:150]}")
        cross_section = "【其他伙伴动态】\n" + "\n".join(lines)

    # 最终保险：任何进入输出的记忆再验一遍可见性（候选阶段已过滤，这里断言性质）
    for m in recent + unresolved[:3]:
        if not can_view(m, ai_id):
            raise RuntimeError(f"visibility leak blocked: {m.get('id')} -> {ai_id}")

    # 优先级：相关记忆 > 待办 > 最近动态 > 梦 > 伙伴动态
    text = _fit_sections(
        [recall_section, todo_section, recent_section, dream_text, cross_section],
        max_chars,
    )
    if not text:
        text = "（最近 48 小时没有新的记忆更新。）"

    metadata["chars"] = len(text)
    metadata["recent_count"] = len(recent)
    return {"text": text, "metadata": metadata}
