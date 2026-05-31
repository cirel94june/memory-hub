"""
Memory Gateway：小模型预处理层
- 每次对话前自动组装记忆 context
- 每次对话后自动提取值得记住的信息
"""
import json
import httpx
from config import GEMINI_API_KEY, ROOMS
from memory_ops import recall, get_living_room, get_ai_private_summary, remember, update_memory
from corridor import get_corridor


async def _call_gemini_flash(prompt: str) -> str:
    """调用 Gemini Flash（便宜的小模型）做记忆判断"""
    if not GEMINI_API_KEY:
        return ""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 1024},
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json=body)
            resp.raise_for_status()
            data = resp.json()
            return data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        print(f"[Gateway] Gemini Flash error: {e}")
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

    # 3. 用小模型判断需要查哪些书房分区
    study_rooms = {k: v for k, v in ROOMS.items() if k.startswith("study_")}
    room_list = "\n".join([f"- {k}: {v['name']}" for k, v in study_rooms.items()])

    # 组装最近对话上下文
    context_text = ""
    if recent_messages:
        context_text = "\n".join([f"{m.get('role','')}: {m.get('content','')[:200]}" for m in recent_messages[-3:]])

    judge_prompt = f"""你是一个记忆路由助手。根据用户的最新消息和最近对话，判断需要查阅哪些记忆分区。

用户最新消息：{user_message}

最近对话：
{context_text}

可用分区：
{room_list}
- game_room: 游戏房（小游戏、编故事、跑团）

请输出一个 JSON 数组，包含需要查阅的分区 key。如果都不需要就输出空数组。
只输出 JSON，不要其他内容。示例：["study_health", "study_career"]"""

    rooms_to_check = []
    result = await _call_gemini_flash(judge_prompt)
    if result:
        try:
            # 提取 JSON
            result = result.strip()
            if result.startswith("```"):
                result = result.split("\n", 1)[-1].rsplit("```", 1)[0]
            rooms_to_check = json.loads(result)
        except Exception:
            pass

    # 4. 向量搜索相关记忆
    search_rooms = rooms_to_check if rooms_to_check else None
    exclude_isolated = "game_room" not in (rooms_to_check or [])
    recalled = await recall(
        query=user_message,
        ai_id=ai_id,
        top_k=6,
        include_rooms=search_rooms,
        exclude_isolated=exclude_isolated,
    )
    if recalled:
        recalled_ids = [r["id"] for r in recalled]
        rooms_checked.extend(rooms_to_check or [])
        lines = [f"- [{r['room']}] {r['content']}" for r in recalled]
        parts.append("【相关记忆】\n" + "\n".join(lines))

    inject_text = "\n\n".join(parts) if parts else ""

    return {
        "inject_text": inject_text,
        "recalled_ids": recalled_ids,
        "rooms_checked": list(set(rooms_checked)),
    }


async def post_process(user_message: str, ai_response: str, ai_id: str, platform: str = "") -> dict:
    """
    核心功能：AI 回复之后，自动提取值得记住的信息。

    返回:
    {
        "actions": [{"type": "remember/update/skip", "content": "...", ...}]
    }
    """
    prompt = f"""你是一个记忆提取助手。分析以下对话，判断是否有值得长期记住的新信息。

用户说：{user_message[:500]}
AI回复：{ai_response[:500]}

请判断：
1. 有没有关于用户身份/状态的新信息？（如：用户换了工作、心情不好、生病了）
2. 有没有关于用户偏好的新信息？（如：喜欢/不喜欢什么）
3. 有没有重要事件？（如：约定、计划、重要日期）
4. 这是不是在玩游戏/编故事？（如果是，标记为 game 分类）
5. 不重要的闲聊就跳过

输出 JSON 格式：
{{
  "actions": [
    {{
      "type": "remember",
      "content": "要记住的内容（用简洁陈述句）",
      "layer": "shared 或 private",
      "room": "living_room/study_career/study_health/game_room 等",
      "category": "分类",
      "importance": 0.1到1.0,
      "emotion_arousal": 0.1到1.0
    }}
  ]
}}

如果没有值得记住的，输出 {{"actions": []}}
只输出 JSON。"""

    result = await _call_gemini_flash(prompt)
    actions_data = {"actions": []}

    if result:
        try:
            result = result.strip()
            if result.startswith("```"):
                result = result.split("\n", 1)[-1].rsplit("```", 1)[0]
            actions_data = json.loads(result)
        except Exception:
            pass

    # 执行提取到的动作
    executed = []
    for action in actions_data.get("actions", []):
        if action.get("type") == "remember" and action.get("content"):
            owner = ai_id if action.get("layer") == "private" else ""
            await remember(
                content=action["content"],
                layer=action.get("layer", "shared"),
                room=action.get("room", "living_room"),
                category=action.get("category", ""),
                owner_ai=owner,
                importance=action.get("importance", 0.5),
                emotion_arousal=action.get("emotion_arousal", 0.3),
                source_ai=ai_id,
                source_platform=platform,
            )
            executed.append(action)

    return {"actions": executed}
