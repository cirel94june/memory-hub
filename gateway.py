"""
Memory Gateway：小模型预处理层
- 每次对话前自动组装记忆 context
- 每次对话后自动提取值得记住的信息
"""
import json
import math
import httpx
from datetime import datetime, timezone
from config import LLM_API_KEY, LLM_MODEL, LLM_BASE_URL, ROOMS
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


async def build_context(user_message: str, ai_id: str, recent_messages: list[dict] = None) -> dict:
    """
    核心功能：在 AI 回复之前，自动组装要注入的记忆 context。

    返回:
    {
        "inject_text": "要注入到 system prompt 的记忆文本",
        "recalled_ids": ["被召回的记忆ID列表"],
        "rooms_checked": ["检查了哪些房间"]
    }
    """
    parts = []
    recalled_ids = []
    rooms_checked = ["living_room"]

    # 1. 注入走廊（已经包含客厅精华 + 关系 + 跨端动态）
    corridor_text = await get_corridor(ai_id)
    if corridor_text:
        parts.append(corridor_text)

    # 3. 用小模型判断需要查哪些房间
    on_demand_rooms = {k: v for k, v in ROOMS.items()
                       if v.get("type") == "on_demand" and v.get("scope") == "shared"}
    room_list = "\n".join([f"- {k}: {v['name']}（{v.get('description','')}）" for k, v in on_demand_rooms.items()])

    context_text = ""
    if recent_messages:
        context_text = "\n".join([f"{m.get('role','')}: {m.get('content','')[:200]}" for m in recent_messages[-3:]])

    judge_prompt = f"""你是一个记忆路由助手。根据用户消息判断需要查哪些记忆房间。

用户消息：{user_message}

最近对话：
{context_text}

可用房间：
{room_list}
- game_room: 游戏房（小游戏、编故事、跑团）

输出 JSON 数组，包含需要查的房间 key。不需要就输出空数组。
只输出 JSON。示例：["psychology", "health"]"""

    rooms_to_check = []
    result = await _call_llm(judge_prompt)
    if result:
        try:
            result = result.strip()
            if result.startswith("```"):
                result = result.split("\n", 1)[-1].rsplit("```", 1)[0]
            rooms_to_check = json.loads(result)
        except Exception:
            pass

    # 4. 三级记忆过滤（借鉴 Aelios）
    #    L1: 混合搜索粗筛 → 12 条候选
    #    L2: 小模型 reranker → 精筛 5 条
    #    L3: 压缩到 ≤300 字/条（注入时）
    exclude_isolated = "game_room" not in (rooms_to_check or [])
    recalled = await recall(
        query=user_message,
        ai_id=ai_id,
        top_k=12,  # L1: 粗筛 12 条
        include_rooms=rooms_to_check if rooms_to_check else None,
        exclude_isolated=exclude_isolated,
    )
    if recalled:
        # L2: Reranker — 小模型精筛到 5 条
        recalled = await _rerank_memories(user_message, recalled, top_k=5)
        recalled_ids = [r["id"] for r in recalled]
        rooms_checked.extend(rooms_to_check or [])
        # L3: 压缩 — 每条 ≤400 字，附带原始语境帮助回忆细节
        lines = []
        for r in recalled:
            content = r["content"]
            if len(content) > 400:
                content = content[:380] + "..."
            room_tag = r["room"]
            if r.get("resolved") == False:
                room_tag += "/待办"
            time_label = _relative_time(r.get("created_at", ""))
            if time_label:
                room_tag += f"/{time_label}"
            line = f"- [{room_tag}] {content}"
            src = r.get("source_context", "")
            if src:
                preview = src.replace("\n", " ")[:120]
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
        "recalled_ids": recalled_ids,
        "rooms_checked": list(set(rooms_checked)),
        "recall_summary": recall_summary,
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


async def post_process(user_message: str, ai_response: str, ai_id: str, platform: str = "") -> dict:
    """
    核心功能：AI 回复之后，自动提取值得记住的信息。

    返回:
    {
        "actions": [{"type": "remember/update/skip", "content": "...", ...}]
    }
    """
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
**重要**：用户的工作困难、情绪状态、生活事件 = about:"user"，不是AI自己的经历！

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
    _refusal_kw = ["i can't", "i cannot", "as an ai", "作为ai", "我是ai", "作为语言模型", "无法扮演"]
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
            owner = ai_id if action.get("layer") == "private" else ""
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
                layer=action.get("layer", "shared"),
                room=room,
                category=action.get("category", ""),
                owner_ai=owner,
                importance=imp,
                emotion_arousal=action.get("emotion_arousal", 0.3),
                source_ai=ai_id,
                source_platform=platform,
                source_context=source_ctx,
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

    return {"actions": executed, "store_summary": store_summary}
