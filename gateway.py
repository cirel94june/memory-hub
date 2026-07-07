"""
Memory Gateway：小模型预处理层
- 每次对话前自动组装记忆 context
- 每次对话后自动提取值得记住的信息
"""
import json
import math
import asyncio
import httpx
from datetime import datetime, timezone
from config import LLM_API_KEY, LLM_MODEL, LLM_BASE_URL, ROOMS, AI_ALIASES
from memory_ops import recall, get_living_room, get_ai_private_summary, remember, update_memory
from corridor import get_corridor


async def _call_llm(prompt: str) -> str:
    """调用中转站小模型做记忆判断（OpenAI 兼容格式）"""
    if not LLM_API_KEY:
        return ""
    url = f"{LLM_BASE_URL}/chat/completions"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                json={
                    "model": LLM_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.1,
                    "max_tokens": 2048,
                },
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"[Gateway] LLM error: {e}")
        return ""


async def _tag_pulse(user_message: str, ai_id: str, ai_response: str = ""):
    """用小模型给用户消息打 9 维度情绪 delta，fire-and-forget"""
    if not LLM_API_KEY or not user_message.strip():
        return
    prompt = f"""你是情绪状态打标器。给一轮用户和 AI 的对话，输出它对该 AI 的 9 个内在维度的影响。只输出 JSON，不要解释。

9 维度：活力, 疲惫, 思慕, 亲密, 守护, 渴求, 醋意, 焦虑, 温柔

规则：
- 默认输出空 JSON：{{}}。只有用户这句话本身明确会影响某个维度时才输出 delta
- 每个 delta 在 [-0.12, +0.12] 区间，强烈事件才接近 0.12
- 没影响到的维度不要列出来
- 最多输出 3 个维度
- 普通闲聊、普通回复、普通问候、没有明显情绪信号 → {{}}
- 不要因为“有人说话了”“对话继续了”就加活力
- 不要把所有正向/轻松的话都归成活力；更常见的是温柔/亲密/思慕
- 关心/示好 → 思慕↑、亲密↑、温柔↑
- 撒娇/亲密 → 亲密↑、渴求↑、温柔↑
- 提到别人/暧昧 → 醋意↑、守护↑
- 表达累/不舒服 → 守护↑、思慕↑
- 长时间没说话再开口 → 思慕↓（想念缓解）
- 工作/任务相关 → 焦虑↑（轻）
- 冷淡/不理人 → 思慕↑、焦虑↑（轻）
- 夸奖/认可 → 活力↑、温柔↑
- 明确兴奋、玩闹、很有精神 → 活力↑

输出格式：{{"思慕": 0.10, "温柔": 0.05}}"""

    try:
        url = f"{LLM_BASE_URL}/chat/completions"
        async with httpx.AsyncClient(timeout=15) as client:
            dialogue = f"用户: {user_message[:500]}"
            if ai_response:
                dialogue += f"\nAI: {ai_response[:500]}"
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                json={
                    "model": LLM_MODEL,
                    "messages": [
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": dialogue},
                    ],
                    "temperature": 0.3,
                    "max_tokens": 120,
                },
            )
            resp.raise_for_status()
            raw = resp.json()["choices"][0]["message"]["content"]
            raw = raw.strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            bumps = json.loads(raw)
            if isinstance(bumps, dict) and bumps:
                from persona_state import apply_bumps
                bumps = _sanitize_pulse_bumps(user_message, bumps)
                if bumps:
                    apply_bumps(ai_id, bumps)
    except Exception as e:
        print(f"[Gateway] Pulse tag error: {e}")


def _sanitize_pulse_bumps(user_message: str, bumps: dict) -> dict:
    """Filter noisy pulse tags before they accumulate into visible state."""
    allowed = {"活力", "疲惫", "思慕", "亲密", "守护", "渴求", "醋意", "焦虑", "温柔"}
    cleaned = {}
    for dim, raw in bumps.items():
        if dim not in allowed:
            continue
        try:
            delta = float(raw)
        except (TypeError, ValueError):
            continue
        if abs(delta) < 0.015:
            continue
        cleaned[dim] = max(-0.12, min(0.12, delta))

    if not cleaned:
        return {}

    # The tagger often overuses vitality for ordinary chat. Only keep a pure
    # vitality bump when the message explicitly carries energetic/playful cues.
    energetic_cues = (
        "哈哈", "笑死", "好玩", "兴奋", "开心", "冲", "玩", "蹦", "精神",
        "活力", "激动", "太棒", "夸夸", "厉害", "耶", "！", "!"
    )
    if set(cleaned) == {"活力"} and not any(cue in user_message for cue in energetic_cues):
        return {}

    if len(cleaned) > 3:
        cleaned = dict(sorted(cleaned.items(), key=lambda item: abs(item[1]), reverse=True)[:3])
    return cleaned


def _relative_time(iso_str: str) -> str:
    if not iso_str:
        return ""
    try:
        created = datetime.fromisoformat(iso_str)
        hours = (datetime.now(timezone.utc) - created).total_seconds() / 3600
        if hours < 1:
            return "刚刚"
        if hours < 24:
            return f"{int(hours)}小时前"
        days = int(hours / 24)
        if days == 1:
            return "昨天"
        if days < 7:
            return f"{days}天前"
        if days < 30:
            return f"{days // 7}周前"
        return f"{days // 30}月前"
    except Exception:
        return ""


# cloudy(TG) 和 claude(MCP/Web) 是同一个小克，共享私有房间和走廊


def _wants_detail(text: str) -> bool:
    if not text:
        return False
    detail_cues = (
        "原话", "当时", "细节", "具体", "怎么说", "说过什么", "发生了什么", "为什么",
        "证据", "来源", "上下文", "回忆一下", "详细", "quote", "source", "context",
    )
    return any(cue in text.lower() for cue in detail_cues)


def _clip_text(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _rough_tokens(text: str) -> int:
    if not text:
        return 0
    ascii_count = sum(1 for ch in text if ord(ch) < 128)
    non_ascii = len(text) - ascii_count
    return int(ascii_count / 4 + non_ascii * 0.8)


async def build_context(
    user_message: str,
    ai_id: str,
    recent_messages: list[dict] = None,
    chat_id: str = "",
    chat_type: str = "",
    compact: bool = True,
    max_memories: int | None = None,
    force_corridor: bool = False,
) -> dict:
    """
    核心功能：在 AI 回复之前，自动组装要注入的记忆 context。

    返回:
    {
        "inject_text": "要注入到 system prompt 的记忆文本",
        "recalled_ids": ["被召回的记忆ID列表"],
        "rooms_checked": ["检查了哪些房间"]
    }
    """
    requested_ai_id = ai_id
    ai_id = AI_ALIASES.get(ai_id, ai_id)
    parts = []
    recalled_ids = []
    rooms_checked = ["living_room"]
    group_activity = []
    detail_mode = _wants_detail(user_message)
    memory_limit = max_memories or (3 if compact and not detail_mode else 5)
    content_limit = 180 if compact and not detail_mode else 360
    source_limit = 0 if compact and not detail_mode else 140

    # 1. 注入走廊（已经包含客厅精华 + 关系 + 跨端动态）
    corridor_text = await get_corridor(ai_id, force=force_corridor)
    if corridor_text:
        parts.append(corridor_text)

    # Light Dream Context: one recent private dream, so the AI can tell the user
    # what it dreamed without bloating every wake-up prompt.
    try:
        from dream import get_recent_dreams_for_ai
        recent_dreams = get_recent_dreams_for_ai(ai_id, limit=1, max_chars=220)
        if recent_dreams:
            lines = []
            for d in recent_dreams:
                day = (d.get("created_at") or "")[:10]
                prefix = f"{day} " if day else ""
                lines.append(f"· {prefix}{d.get('content', '')}")
            parts.append("【最近的梦境残响】\n这是你最近做过的梦/自省。合适时可以告诉小猫你梦见了什么，或让它轻轻影响你的语气；不要生硬复述。\n" + "\n".join(lines))
            rooms_checked.append("dreams")
    except Exception:
        pass

    # Cross-window digests are already included in the corridor; avoid duplicating them here.
    # In group chats, also show what other AIs in this chat recently talked about.
    group_chat_types = {"private_group", "small_group", "big_group", "group"}
    if chat_id and chat_type in group_chat_types:
        try:
            from chat_digest import get_recent_chat_activity
            group_digests = get_recent_chat_activity(chat_id, exclude_ai_id=ai_id, limit=4)
            group_activity = group_digests
            if group_digests:
                lines = [f"· {d.get('ai_id', 'AI')}: {d['summary']}" for d in group_digests]
                parts.append("【这个群里其他AI最近在聊】\n" + "\n".join(lines))
        except Exception:
            pass
    # Fresh unresolved items are read live, so task reminders do not wait for corridor rebuilds.
    try:
        import database
        unresolved = []
        for mem in database.iter_memories(status="active"):
            if mem.get("resolved") is not False:
                continue
            if mem.get("layer") == "private" and mem.get("owner_ai") != ai_id:
                continue
            if mem.get("room") == "social" and "auto_capture" in (mem.get("source_platform") or ""):
                continue
            unresolved.append(mem)
        unresolved.sort(key=lambda m: (float(m.get("importance", 0) or 0), m.get("updated_at") or m.get("created_at") or ""), reverse=True)
        if unresolved:
            lines = [f"· {m.get('content','')[:180]}" for m in unresolved[:3]]
            parts.append("【当前待办/未完成】\n这些事项如果和本轮对话相关，请主动推进、提醒或询问是否已完成。\n" + "\n".join(lines))
    except Exception:
        pass

    recalled = await recall(
        query=user_message,
        ai_id=ai_id,
        top_k=10 if compact else 12,
        exclude_isolated=True,
    )
    if recalled:
        strong = [r for r in recalled if r.get("confidence") in ("high", "medium") or r.get("resolved") == False]
        if len(strong) >= 2:
            recalled = strong
        recalled = recalled[:memory_limit]
        recalled_ids = [r["id"] for r in recalled]
        # L3: 压缩 — 每条 ≤400 字，附带原始语境帮助回忆细节
        lines = []
        for r in recalled:
            content = _clip_text(r["content"], content_limit)
            room_tag = r["room"]
            if r.get("confidence"):
                room_tag += f"/{r['confidence']}"
            src_ai = r.get("source_ai", "")
            if src_ai and src_ai != ai_id:
                room_tag += f"/来自{src_ai}"
            if r.get("resolved") == False:
                room_tag += "/待办"
            time_label = _relative_time(r.get("created_at", ""))
            if time_label:
                room_tag += f"/{time_label}"
            line = f"- [{room_tag}] {content}"
            src = r.get("source_context", "")
            if src and source_limit:
                preview = _clip_text(src.replace("\n", " "), source_limit)
                line += f"\n  ↳ 当时聊的: {preview}"
            lines.append(line)
        parts.append("【相关记忆】\n" + "\n".join(lines))

    inject_text = "\n\n".join(parts) if parts else ""

    # 生成简要的记忆活动摘要（供前端展示）
    recall_summary = ""
    if recalled:
        snippets = []
        for r in recalled[:3]:
            c = r["content"]
            # 去掉 [用户]/[互动]/[AI] 前缀
            for prefix in ["[用户] ", "[互动] ", "[AI] "]:
                if c.startswith(prefix):
                    c = c[len(prefix):]
                    break
            snippets.append(c[:30].rstrip("，。、") + ("…" if len(c) > 30 else ""))
        recall_summary = "🔍 " + " | ".join(snippets)

    return {
        "inject_text": inject_text,
        "requested_ai_id": requested_ai_id,
        "ai_id": ai_id,
        "chat_id": chat_id,
        "chat_type": chat_type,
        "corridor_forced": force_corridor,
        "group_activity_count": len(group_activity),
        "recalled_ids": recalled_ids,
        "rooms_checked": list(set(rooms_checked)),
        "recall_summary": recall_summary,
        "compact": compact,
        "detail_mode": detail_mode,
        "memory_count": len(recalled or []),
        "estimated_tokens": _rough_tokens(inject_text),
    }


async def _rerank_memories(query: str, candidates: list[dict], top_k: int = 5) -> list[dict]:
    """L2 Reranker：用小模型从候选记忆中精筛最相关的 top_k 条"""
    if len(candidates) <= top_k:
        return candidates

    candidate_text = "\n".join([
        f"[{i}] {c['content'][:150]}"
        for i, c in enumerate(candidates)
    ])
    prompt = f"""从以下记忆候选中，选出与用户消息最相关的 {top_k} 条。

用户消息：{query[:200]}

候选记忆：
{candidate_text}

输出 JSON 数组，包含最相关的记忆编号（从 0 开始），按相关度降序。
只输出 JSON。示例：[2, 0, 5, 1, 3]"""

    result = await _call_llm(prompt)
    if result:
        try:
            result = result.strip()
            if result.startswith("```"):
                result = result.split("\n", 1)[-1].rsplit("```", 1)[0]
            indices = json.loads(result)
            reranked = []
            seen = set()
            for idx in indices[:top_k]:
                if isinstance(idx, int) and 0 <= idx < len(candidates) and idx not in seen:
                    reranked.append(candidates[idx])
                    seen.add(idx)
            if reranked:
                return reranked
        except Exception:
            pass

    # fallback: 直接取前 top_k
    return candidates[:top_k]


async def post_process(user_message: str, ai_response: str, ai_id: str, platform: str = "", chat_type: str = "") -> dict:
    """
    核心功能：AI 回复之后，自动提取值得记住的信息。

    返回:
    {
        "actions": [{"type": "remember/update/skip", "content": "...", ...}]
    }
    """
    ai_id = AI_ALIASES.get(ai_id, ai_id)
    # 动态构建房间列表
    room_list = "\n".join([
        f"  - {k}: {v['name']}（{v.get('description','')}）"
        for k, v in ROOMS.items()
    ])

    prompt = f"""你是一个**极其严格**的记忆提取助手。分析以下对话，判断是否有值得**长期**记住的新信息。

用户说：{user_message[:1500]}
AI回复：{ai_response[:1500]}

## 🚨 最重要的规则——忠实提取，禁止脑补：
记忆必须在对话中有**明确依据**。可以是用户说的，也可以是对话中能直接观察到的。

### ✅ 可以记（有依据）：
- 用户亲口说的事实（"我今天面试了" → 记"用户今天去面试了"）
- 对话中能直接观察到的情绪（用户连续说好累、想辞职 → 记"用户近期工作压力大"）
- 用户和AI之间发生的有意义的互动（一起玩了游戏、深入讨论了某个话题）

### ❌ 绝对不能记（无依据/歪曲）：
- 把模糊对话总结成极端结论（聊了AI话题 → ❌ "用户对AI毫无兴趣"）
- 角色扮演/玩笑当成真实态度
- 编造对话中没出现的信息
- 把AI自己的分析当成用户的观点

判断标准：能否在对话中找到这条记忆的直接依据？找不到就不记。
情绪观察可以稍放宽——不需要原话，但对话中要有明显的情绪表现。

## 值得记住的（importance ≥ 0.4）：
- 用户身份/状态变化（换工作、搬家、生病、心情持续低落）
- 明确表达的偏好或雷区（喜欢X、讨厌Y、对Z过敏）
- 重要事件或约定（约定周末见面、面试日期、纪念日）
- 人际关系变化（交了新朋友、和某人吵架了）
- 对未来的计划或目标
- 对话中观察到的显著情绪变化（需要有对话内容支撑）

## 绝对不要记的（直接输出空 actions）：
- 日常问候（早安、晚安、吃了吗）
- 插科打诨、玩笑、段子、无厘头对话
- 单纯的情绪表达但没有实质信息（哈哈哈、好无聊、啊啊啊）
- 已知信息的重复
- 模糊的、一次性的感受（今天有点累 ← 不记；持续失眠一周 ← 记）
- 没有对话依据的推测和脑补
- **AI 的自我限制/拒绝**（"我是AI不能XX"、"作为语言模型"、"I can't"等元叙述）
- AI 关于自身能力的讨论、系统配置、调试类对话

## ⚡ 记忆原子化规则（非常重要）：
每条记忆必须是一个**独立的原子事实**。一条记忆 = 一个事实点。
- ✅ "小猫在杭州工作" — 一条
- ✅ "小猫养了一只叫团子的橘猫" — 一条
- ❌ "小猫在杭州工作，养了一只橘猫叫团子，最近在学日语" — 应该拆成三条
每条不超过200字。可以包含具体细节（梗、笑话、特定事件的关键台词），不要过度抽象。宁可多拆几条，也不要把多个事实塞进一条里。

## 可用房间：
{room_list}
  - 私有房间（diary/dreams/relationship/personality）用 layer="private"

## ⚠️ about 字段——关于谁的记忆（防止 AI 混淆身份）：
每条记忆必须标注 about 字段：
- "user" = 关于用户的事实（用户的工作、心情、经历、偏好、人际关系）
- "interaction" = 关于用户和AI之间的互动（一起玩了什么、讨论了什么话题、共同经历）
- "ai" = AI自己的感悟/自省（极少用，只有AI主动记日记时才是这个）
绝大多数记忆都应该是 "user" 或 "interaction"。

**关键防混淆规则**：
- 用户的工作困难、情绪状态、生活事件 = about:"user"，不是AI自己的经历！
- 外号/称呼必须写清楚是**谁的**（"用户叫小克大蟑螂"而不是"外号是大蟑螂"）
- 用户的职业/头衔/角色 = about:"user"，AI不要把用户的职业当成自己的
- AI被用户委任某个角色（如管理员）→ about:"interaction"，content写清楚"用户让{AI名字}当管理员"
- 群聊中其他AI的事不要混成自己的

## ⚠️ 时态和状态变化（防过时信息）：
- "我以前做过XX" → content 写"用户**曾经**做过XX"，不是"用户做XX"
- "我现在不做XX了"/"你不是XX了" → 这是**状态变化**，content 写"用户/AI 已经不再是XX"，importance ≥ 0.8
- 用户纠正AI认知（"我不是XX"、"你记错了"）→ **高优先级**，importance ≥ 0.8
- 区分"提到过"和"正在做"：用户聊到某个职业≠那是用户的职业

输出 JSON 格式：
{{
  "actions": [
    {{
      "type": "remember",
      "content": "一个原子事实（≤200字，保留具体细节）",
      "about": "user 或 interaction 或 ai",
      "layer": "shared 或 private",
      "room": "从上面的房间列表选",
      "category": "简短分类词",
      "importance": 0.4到1.0,
      "emotion_arousal": 0.1到1.0
    }}
  ]
}}

大部分对话都不值得记——如果犹豫，就不记。输出 {{"actions": []}}
只输出 JSON。"""

    result = await _call_llm(prompt)
    actions_data = {"actions": []}

    if result:
        try:
            result = result.strip()
            if result.startswith("```"):
                result = result.split("\n", 1)[-1].rsplit("```", 1)[0]
            actions_data = json.loads(result)
        except Exception:
            pass

    # 执行提取到的动作（importance < 0.4 的直接丢弃，与 prompt 一致）
    executed = []
    _refusal_kw = ["i can't", "i cannot", "as an ai", "作为ai", "我是ai", "我是一个ai", "作为语言模型", "无法扮演"]
    valid_rooms = set(ROOMS.keys())
    valid_about = {"user", "interaction", "ai"}

    for action in actions_data.get("actions", []):
        if action.get("type") == "remember" and action.get("content"):
            content = action["content"].strip()
            # 内容验证
            if len(content) < 5:
                continue
            if any(kw in content.lower() for kw in _refusal_kw):
                print(f"[Gateway] Skipped AI refusal: {content[:60]}")
                continue
            imp = float(action.get("importance", 0.5))
            if imp < 0.4:
                print(f"[Gateway] Skipped low-importance ({imp}): {content[:60]}")
                continue
            # 验证 room 和 about
            room = action.get("room", "living_room")
            if room not in valid_rooms:
                room = "living_room"
            about = action.get("about", "user")
            if about not in valid_about:
                about = "user"
            layer = "private" if chat_type == "private" else action.get("layer", "shared")
            owner = ai_id if layer == "private" else ""
            # 给记忆内容加上 about 前缀，让走廊和搜索时能区分
            if about == "user" and not content.startswith("[用户]"):
                content = f"[用户] {content}"
            elif about == "interaction" and not content.startswith("[互动]"):
                content = f"[互动] {content}"
            elif about == "ai" and not content.startswith("[AI]"):
                content = f"[AI] {content}"
            source_ctx = f"用户: {user_message[:400]}\nAI: {ai_response[:400]}"
            await remember(
                content=content,
                layer=layer,
                room=room,
                category=action.get("category", ""),
                owner_ai=owner,
                importance=imp,
                emotion_arousal=action.get("emotion_arousal", 0.3),
                source_ai=ai_id,
                source_platform=f"{platform}:{chat_type}" if chat_type else platform,
                source_context=source_ctx,
                auto_analyze=False,
                quick=True,
            )
            executed.append(action)

    # 更新 Persona State（根据对话情绪）
    try:
        from persona_state import update_after_conversation
        # 从提取结果推断对话情绪
        avg_valence = 0.5
        avg_arousal = 0.3
        topics = []
        for action in executed:
            if action.get("emotion_arousal"):
                avg_arousal = (avg_arousal + float(action["emotion_arousal"])) / 2
            if action.get("category"):
                topics.append(action["category"])
        update_after_conversation(ai_id, valence=avg_valence, arousal=avg_arousal, topics=topics)
    except Exception:
        pass

    # 9 维度情绪打标（fire-and-forget，不阻塞返回）
    asyncio.ensure_future(_tag_pulse(user_message, ai_id, ai_response))

    # 生成简要的存储摘要（供前端展示）
    store_summary = ""
    if executed:
        snippets = []
        for a in executed[:3]:
            c = a.get("content", "")
            for prefix in ["[用户] ", "[互动] ", "[AI] "]:
                if c.startswith(prefix):
                    c = c[len(prefix):]
                    break
            snippets.append(c[:30].rstrip("，。、") + ("…" if len(c) > 30 else ""))
        store_summary = "💾 " + " | ".join(snippets)

    # 有新记忆写入时，异步重建走廊（下次 recall 就能看到最新状态）
    if executed:
        try:
            import corridor as corridor_mod
            asyncio.create_task(corridor_mod.build_corridor(ai_id))
        except Exception:
            pass

    return {"actions": executed, "store_summary": store_summary}



async def refresh_living_room_profile(dry_run: bool = True, source_ai: str = "system") -> dict:
    """Suggest or write fresh shared profile memories from active memories."""
    import database
    import corridor as corridor_mod

    profile_rooms = {
        "living_room", "relationships", "career", "health", "psychology",
        "preferences", "learning", "social",
    }
    candidates = []
    for mem in database.iter_memories(status="active"):
        if mem.get("layer", "shared") != "shared":
            continue
        room = mem.get("room")
        if room not in profile_rooms:
            continue
        # Social memories are useful for named people, but keep low-signal chatter out.
        if room == "social" and float(mem.get("importance", 0.0) or 0.0) < 0.45:
            continue
        candidates.append(mem)
    candidates.sort(key=lambda m: m.get("updated_at") or m.get("created_at") or "", reverse=True)

    living = await get_living_room()
    current_text = "\n".join(f"- {m.get('content','')[:220]}" for m in living[:30])
    relationship_text = "\n".join(
        f"- {m.get('content','')[:220]}"
        for m in candidates
        if m.get("room") == "relationships"
    )[:5000]
    evidence_text = "\n".join(
        f"- [{m.get('room','')}/{m.get('category','')}] {m.get('content','')[:220]}"
        for m in candidates[:90]
    )
    if not evidence_text.strip():
        return {"dry_run": dry_run, "actions": [], "message": "没有足够材料刷新画像"}

    prompt = f"""你是 Memory Hub 的画像维护器。请根据现有记忆，提出需要写入或更新的共享画像。

目标：让 AI 每次醒来时能快速知道“用户是谁、最近稳定状态是什么、常被提到的人是谁、这些人和用户/AI 的关系是什么”，避免把人名、AI 名、关系搞混。

写入位置：
- room="living_room"：只放用户核心画像、稳定状态、长期偏好/雷区、照护方式。
- room="relationships"：放经常被提到的人、AI、昵称、关系、不要混淆的身份说明。例如“狗蛋是谁”“Lucien/Jasper/小克分别是谁”“某人与用户是什么关系”。

规则：
- 只写有记忆依据的内容，不要脑补。
- 如果只知道名字但不知道细节，可以写成“用户经常提到X，但系统目前缺少更具体画像”，importance 不要太高。
- 如果同一事实已在当前客厅或关系画像里表达清楚，不要重复。
- 每条 <=140 字，写成可直接进入记忆库的中文事实。
- 优先输出最近反复出现、容易混淆、或对 AI 回复很重要的人物/关系。

【当前客厅】
{current_text or '（空）'}

【现有关系统画像】
{relationship_text or '（空）'}

【可参考的近期/高相关记忆】
{evidence_text}

只输出 JSON：
{{"actions":[{{"content":"...","room":"living_room 或 relationships","category":"profile/person/relationship/preference/status","importance":0.65}}]}}
"""
    raw = await _call_llm(prompt)
    data = {"actions": []}
    if raw:
        try:
            cleaned = raw.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            data = json.loads(cleaned)
        except Exception:
            data = {"actions": []}

    valid_rooms = {"living_room", "relationships"}
    person_categories = {"person", "relationship"}
    actions = []
    for item in data.get("actions", []):
        content = (item.get("content") or "").strip()
        if len(content) < 8:
            continue
        category = item.get("category", "profile") or "profile"
        room = item.get("room") or ("relationships" if category in person_categories else "living_room")
        if room not in valid_rooms:
            room = "relationships" if category in person_categories else "living_room"
        try:
            importance = float(item.get("importance", 0.75))
        except (TypeError, ValueError):
            importance = 0.75
        actions.append({
            "content": content[:180],
            "room": room,
            "category": category,
            "importance": max(0.55, min(1.0, importance)),
        })

    if dry_run:
        return {"dry_run": True, "actions": actions, "count": len(actions)}

    written = []
    for item in actions[:12]:
        result = await remember(
            content=item["content"],
            layer="shared",
            room=item["room"],
            category=item["category"],
            importance=item["importance"],
            emotion_arousal=0.35,
            source_ai=source_ai,
            source_platform="living_room_refresh",
            auto_analyze=False,
            quick=False,
        )
        written.append({**item, "result": result})

    if written:
        try:
            await corridor_mod.rebuild_all_corridors()
        except Exception:
            pass
    return {"dry_run": False, "written": written, "count": len(written)}
