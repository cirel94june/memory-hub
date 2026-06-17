"""
对话自动捕获 + 分块总结（借鉴 imprint-memory）

流程：
1. 每轮对话通过 API 存入 conversation_log
2. 攒够 N 条后（或定时触发），小模型自动提取结构化记忆
3. 提取的记忆走正常 remember() 流程（自动打标、合并、supersede）

按聊天类型区分策略：
- private: 20条触发，标准提取
- private_group: 25条触发，偏向记梗/互动细节
- public_group: 80条触发，只捞有信息量的
"""
import json
import time
import logging
from datetime import datetime, timezone
from typing import Optional

from config import LLM_API_KEY, LLM_MODEL, LLM_BASE_URL

logger = logging.getLogger("memory_hub.capture")

# 内存中的对话缓冲（按 ai_id:chat_id 分组）
_conversation_buffers: dict[str, list[dict]] = {}
# 每个缓冲区的聊天类型
_buffer_chat_types: dict[str, str] = {}

# 按聊天类型的触发阈值
CHUNK_SIZES = {
    "private": 20,
    "private_group": 25,
    "public_group": 80,
}
MAX_BUFFER_SIZE = 150

# ── 基础提取 prompt（私聊 + 通用） ──
EXTRACT_PROMPT_BASE = """你是一个**极其严格**的记忆提取专家。从以下对话记录中，提取值得**长期**记住的信息。

## 🚨 最重要的规则——忠实提取，禁止脑补：
记忆必须在对话中有**明确依据**。可以是用户说的，也可以是对话中能直接观察到的。

### ✅ 可以记（有依据）：
- 用户亲口说的事实（"我今天面试了" → 记"用户今天去面试了"）
- 对话中能直接观察到的情绪（用户多次表达疲惫和沮丧 → 记"用户近期情绪低落"）
- 用户和AI之间有意义的互动（深入讨论了某话题、一起完成了某事）

### ❌ 绝对不能记（无依据/歪曲）：
- 把模糊对话总结成极端结论（聊了AI话题 → ❌ "用户对AI毫无兴趣"）
- 角色扮演/玩笑当成真实态度
- 编造对话中没出现的信息

判断标准：能否在对话中找到这条记忆的直接依据？找不到就不记。

## 提取规则（宁缺毋滥）：
1. 提取对话中有依据的事实性信息：身份变化、偏好、重要事件、人际关系、情绪状态
2. 如果用户提到了时间相关的事（"上周"、"明天"），尽量推算出具体日期
3. 提炼但不歪曲——记忆必须忠实于对话内容

## ⚡ 记忆原子化：
每条记忆 = 一个独立的原子事实，不超过200字。
- ✅ "小猫在杭州做UX设计" — 一条
- ✅ "小猫说《间谍过家家》里安尼亚说'哇酷哇酷'那段让她笑了半小时，还模仿了好几遍" — 一条（保留具体细节）
- ❌ "小猫在杭州工作，做UX设计，最近在学日语" — 应该拆成多条
保留具体的梗、笑话、特定台词、有趣的细节——这些是记忆的灵魂，不要抽象成"用户喜欢XX"。

## 绝对不提取：
- 闲聊、寒暄、问候（早安晚安吃了吗）
- 技术调试过程、代码讨论（除非用户说了"我在做XX项目"这类身份信息）
- 没有实质信息的情绪表达（哈哈、无聊、啊啊啊）
- 没有对话依据的推测和脑补
- AI 的自我限制/拒绝（"我是AI不能XX"、"作为语言模型"等）"""

# ── 小群专用追加 prompt（强调梗和互动细节） ──
EXTRACT_PROMPT_PRIVATE_GROUP = """

## 🎯 小群特别规则（非常重要）：
这是私密小群的聊天记录，里面有用户和多个AI的互动。请特别注意提取：

### 必须记的（小群精华）：
- **梗和暗号**：群里产生的独特梗、外号、暗语、内部笑话，要记录具体内容（不是"他们有个梗"而是"小猫叫小克大蟑螂，因为觉得能缓解恐惧症"）
- **有趣的互动场景**：谁怼了谁、谁说了什么金句、什么事让大家笑了
- **群内角色动态**：谁在群里是什么角色/画风，有什么互动模式
- **共同记忆**：一起玩了什么、讨论了什么有意思的话题
- **用户透露的真实信息**：工作、心情、生活事件（即使是在玩笑中提到的）

### 小群里可以放宽的：
- 玩笑和段子如果形成了"梗"（被反复提起或特别好笑），值得记
- AI 之间的互动如果有趣/有代表性，也值得记
- 不要求每条都是"严肃事实"——有趣的互动细节也是记忆的灵魂

### 小群里不记的：
- 纯刷屏/水群（没有信息量的来回）
- 重复的日常寒暄"""

# ── 大群专用追加 prompt（严格过滤） ──
EXTRACT_PROMPT_PUBLIC_GROUP = """

## 🔍 大群特别规则：
这是公开大群的聊天记录，消息量大、bot刷屏多。请极其严格地筛选：

### 大群只记这些：
- 用户（小猫）亲自说的重要信息（工作变动、生活事件、明确表达的观点）
- 用户和特定群友之间的重要互动
- 群里发生的重大事件或重要讨论结论

### 大群绝对不记：
- bot 之间的对话/互动（除非用户参与且有信息量）
- 群友之间的闲聊（跟用户无关的）
- 任何纯灌水内容
- 80条消息里可能一条都不值得记——这很正常"""

# ── 输出格式（所有类型共用） ──
EXTRACT_OUTPUT_FORMAT = """

输出 JSON 数组（空数组表示没有值得记的）：
[
  {
    "content": "一个原子事实（≤200字，保留具体细节如台词、梗、笑点）",
    "room": "最合适的房间ID",
    "importance": 0.4到1.0,
    "event_date": "事件日期（如 2026-06-08，推算不出就留空）",
    "resolved": null 或 false。⚠️ 极少使用 false！只有用户明确说了"要做某事""还没做完""待办""记得提醒我"时才设为 false。群聊梗、知识、互动记录、情绪、观点 → 一律 null。90%以上的记忆应该是 null。
  }
]

房间可选：
  living_room(核心身份), career(职业), psychology(心理), health(健康),
  learning(学习), relationships(人际关系), preferences(兴趣偏好),
  work_tasks(工作事务), infra(基建), social(社交动态/群聊),
  relationship(和AI的关系), diary(AI日记), personality(AI自我认知),
  game_room(游戏/角色扮演)

提示：群聊中的梗、外号、暗号、互动细节 → social 房间

只输出 JSON。"""


def _get_extract_prompt(chat_type: str) -> str:
    """根据聊天类型组装提取 prompt"""
    base = EXTRACT_PROMPT_BASE
    if chat_type == "private_group":
        base += EXTRACT_PROMPT_PRIVATE_GROUP
    elif chat_type == "public_group":
        base += EXTRACT_PROMPT_PUBLIC_GROUP
    return base + EXTRACT_OUTPUT_FORMAT


async def _call_llm(prompt: str) -> str:
    """调用小模型"""
    import httpx
    if not LLM_API_KEY:
        return ""
    url = f"{LLM_BASE_URL}/chat/completions"
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                json={
                    "model": LLM_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.1,
                    "max_tokens": 4096,
                },
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        logger.error(f"Capture LLM error: {e}")
        return ""


def _buffer_key(ai_id: str, chat_id: str = "", platform: str = "") -> str:
    """按 ai_id + chat_id 分组（同一个聊天窗口的消息攒在一起）"""
    if chat_id:
        return f"{ai_id}:chat:{chat_id}"
    return f"{ai_id}:{platform or 'unknown'}"


async def log_conversation(
    user_message: str,
    ai_response: str = "",
    ai_id: str = "claude",
    platform: str = "",
    chat_id: str = "",
    chat_type: str = "private",
) -> dict:
    """记录一轮对话到缓冲区。如果缓冲区满了，自动触发总结。

    chat_type: "private" / "private_group" / "public_group"

    返回：
    - {"status": "buffered", "buffer_size": N} — 正常缓存
    - {"status": "extracted", "memories": [...]} — 触发了总结并提取了记忆
    """
    key = _buffer_key(ai_id, chat_id, platform)
    if key not in _conversation_buffers:
        _conversation_buffers[key] = []

    # 记住这个 buffer 的聊天类型
    if chat_type:
        _buffer_chat_types[key] = chat_type

    now = datetime.now(timezone.utc).isoformat()
    _conversation_buffers[key].append({
        "user": user_message[:1000],
        "ai": ai_response[:1000] if ai_response else "",
        "ai_id": ai_id,
        "platform": platform,
        "chat_type": chat_type,
        "timestamp": now,
    })

    # 防止内存爆
    if len(_conversation_buffers[key]) > MAX_BUFFER_SIZE:
        _conversation_buffers[key] = _conversation_buffers[key][-MAX_BUFFER_SIZE:]

    buffer_size = len(_conversation_buffers[key])

    # 按聊天类型决定触发阈值
    chunk_size = CHUNK_SIZES.get(chat_type, 30)

    if buffer_size >= chunk_size:
        extracted = await _extract_and_remember(key)
        return {"status": "extracted", "memories": extracted, "buffer_size": 0}

    return {"status": "buffered", "buffer_size": buffer_size}


async def force_extract(ai_id: str = "", platform: str = "") -> dict:
    """手动触发对话总结（不等缓冲区满）。

    如果不指定 ai_id，则处理所有缓冲区。
    """
    results = {}
    if ai_id:
        # 尝试找匹配的 buffer
        for key in list(_conversation_buffers.keys()):
            if key.startswith(f"{ai_id}:") and _conversation_buffers[key]:
                extracted = await _extract_and_remember(key)
                results[key] = extracted
    else:
        for key in list(_conversation_buffers.keys()):
            if _conversation_buffers[key]:
                extracted = await _extract_and_remember(key)
                results[key] = extracted

    return {"status": "extracted", "results": results}


async def get_buffer_status() -> dict:
    """查看当前缓冲区状态"""
    status = {}
    for key, buf in _conversation_buffers.items():
        chat_type = _buffer_chat_types.get(key, "unknown")
        chunk_size = CHUNK_SIZES.get(chat_type, 30)
        status[key] = {
            "size": len(buf),
            "chat_type": chat_type,
            "chunk_size": chunk_size,
            "oldest": buf[0]["timestamp"] if buf else None,
            "newest": buf[-1]["timestamp"] if buf else None,
        }
    return {"buffers": status, "chunk_sizes": CHUNK_SIZES}


async def _extract_and_remember(buffer_key: str) -> list[dict]:
    """从对话缓冲区提取记忆，走 remember 流程"""
    import memory_ops

    buffer = _conversation_buffers.get(buffer_key, [])
    if not buffer:
        return []

    chat_type = _buffer_chat_types.get(buffer_key, "private")

    # 把对话格式化给小模型
    lines = []
    char_budget = 5500
    for entry in buffer:
        user_short = entry['user'][:200]
        ai_short = entry['ai'][:200] if entry['ai'] else ""
        if ai_short:
            line = f"[{entry['timestamp'][:16]}] 用户: {user_short} | AI: {ai_short}"
        else:
            # 旁听模式：只有用户消息没有AI回复
            line = f"[{entry['timestamp'][:16]}] {user_short}"
        if sum(len(l) for l in lines) + len(line) > char_budget:
            break
        lines.append(line)
    conversation_text = "\n".join(lines)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    context_label = {"private": "私聊", "private_group": "私密小群", "public_group": "公开大群"}.get(chat_type, "对话")
    prompt = f"今天日期：{today}\n来源：{context_label}，共{len(buffer)}条消息（展示了{len(lines)}条）：\n\n{conversation_text}"

    extract_prompt = _get_extract_prompt(chat_type)
    raw = await _call_llm(extract_prompt + "\n\n" + prompt)
    if not raw:
        logger.warning(f"Extract LLM returned empty for {buffer_key}, keeping buffer for retry")
        return []

    # 解析结果
    try:
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0]
        items = json.loads(raw)
        if not isinstance(items, list):
            items = []
    except Exception as e:
        logger.warning(f"Extract parse failed for {buffer_key}: {e}, keeping buffer for retry")
        return []

    # 提取的记忆走 remember 流程
    ai_id = buffer[0].get("ai_id", "claude") if buffer else "claude"
    platform = buffer[0].get("platform", "") if buffer else ""
    # 小群允许更多条记忆
    max_items = {"private": 8, "private_group": 12, "public_group": 5}.get(chat_type, 8)
    memories = []

    for item in items[:max_items]:
        content = str(item.get("content", "")).strip()
        if not content or len(content) < 10:
            continue

        # 附上原始对话片段作为源追溯
        source_ctx = "\n".join([
            f"用户: {e['user'][:250]}\nAI: {e['ai'][:250]}" if e['ai'] else f"用户: {e['user'][:250]}"
            for e in buffer[-5:]
        ])[:1200]

        result = await memory_ops.remember(
            content=content,
            room=item.get("room", "living_room"),
            importance=max(0.1, min(1.0, float(item.get("importance", 0.5)))),
            event_date=item.get("event_date", ""),
            source_ai=ai_id,
            source_platform=f"auto_capture:{platform}:{chat_type}",
            source_context=source_ctx,
        )
        memories.append(result)

        if item.get("resolved") == False:
            mem_id = result.get("id")
            if mem_id:
                await memory_ops.resolve_memory(mem_id, resolved=False)

    # 清空缓冲区
    _conversation_buffers[buffer_key] = []
    if memories:
        logger.info(f"Auto-captured {len(memories)} memories from {buffer_key} [{chat_type}] ({len(buffer)} messages)")
    else:
        logger.info(f"No memories worth keeping from {buffer_key} [{chat_type}] ({len(buffer)} messages)")

    return memories


async def extract_from_messages(
    messages: list[dict],
    ai_id: str = "claude",
    chat_type: str = "private",
    quick: bool = True,
) -> list[dict]:
    """从一组消息中提取记忆。

    messages 格式：[{"role": "user"/"assistant", "content": "..."}, ...]
    quick=True 时走 remember(quick=True)，不阻塞。
    """
    import memory_ops

    lines = []
    for msg in messages[-30:]:
        role = "用户" if msg.get("role") == "user" else "AI"
        content = msg.get("content", "")[:300]
        lines.append(f"{role}: {content}")

    conversation_text = "\n".join(lines)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    prompt = f"今天日期：{today}\n来源：MCP 前端对话，共 {len(messages)} 条消息：\n\n{conversation_text}"

    extract_prompt = _get_extract_prompt(chat_type)
    raw = await _call_llm(extract_prompt + "\n\n" + prompt)
    if not raw:
        return []

    try:
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0]
        items = json.loads(raw)
        if not isinstance(items, list):
            items = []
    except Exception:
        return []

    memories = []
    for item in items[:10]:
        content = str(item.get("content", "")).strip()
        if not content or len(content) < 10:
            continue
        result = await memory_ops.remember(
            content=content,
            room=item.get("room", "living_room"),
            importance=max(0.1, min(1.0, float(item.get("importance", 0.5)))),
            event_date=item.get("event_date", ""),
            source_ai=ai_id,
            source_platform="mcp_extract",
            quick=quick,
        )
        memories.append(result)

    return memories
