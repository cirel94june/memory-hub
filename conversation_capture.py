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
import asyncio
import json
import time
import logging
from datetime import datetime, timezone
from time_utils import local_now, local_today
from typing import Optional

from config import LLM_API_KEY, LLM_MODEL, LLM_BASE_URL

logger = logging.getLogger("memory_hub.capture")

# 内存中的对话缓冲（按 ai_id:chat_id 分组）
_conversation_buffers: dict[str, list[dict]] = {}
# 每个缓冲区的聊天类型
_buffer_chat_types: dict[str, str] = {}
# 每个缓冲区上次提取的时间（节流用）
_last_extract_time: dict[str, float] = {}
# 防止同一 buffer 并发提取的锁
_extract_locks: dict[str, asyncio.Lock] = {}

# 按聊天类型的触发阈值
CHUNK_SIZES = {
    "private": 30,
    "private_group": 40,
    "public_group": 80,
}
MAX_BUFFER_SIZE = 150
EXTRACT_COOLDOWN = 600  # 同一个 chat 提取后冷却10分钟


def _touch_pulse(user_message: str, ai_response: str, ai_id: str):
    """Let the pulse panel react to captured Telegram conversations.

    Telegram bots use /api/capture/log for cheap buffered memory extraction.
    Pulse updates should still happen per actual bot reply, without forcing the
    full post-process memory extraction path on every message.
    """
    if not ai_response.strip() or not user_message.strip():
        return
    try:
        from config import AI_ALIASES
        from gateway import _tag_pulse

        canonical = AI_ALIASES.get(ai_id, ai_id)
        asyncio.create_task(_tag_pulse(user_message, canonical, ai_response))
        logger.info(f"[Pulse] queued update for {canonical} from capture/log")
    except Exception as e:
        logger.warning(f"[Pulse] capture update skipped: {e}")

# ── 基础提取 prompt（私聊 + 通用） ──
EXTRACT_PROMPT_BASE = """你是记忆提取专家。从对话中提取值得**长期**记住的信息。

## 身份（最高优先级）：
- "用户"/"主人" = ceci（ID:8749953218），也叫小猫
- YanYan/燕燕（ID:8618367675）不是用户！是另一个人
- S哥哥、Jasper/狗蛋、Lucien、Cloudy/小克、D叔、师兄、Evan、Gale、Nicole 都是bot或群友
- **只提取跟 ceci 相关的信息**

## 核心规则（违反=废弃）：
1. **谁说的写谁**：别人说的不能写成"ceci说/认为"。多人对话必须分开归因
2. **禁止脑补**：不能编造对话中没有的情感/动机（"她享受""她认为"需有原文依据）
3. **禁止压缩**：不要把多人互动压成"ceci在群里讨论了XX"，写清每个人说了什么
4. **不用泛称"AI"**：必须写具体名字（Jasper/Cloudy/Lucien/S哥哥等）

## 提取标准：
✅ ceci亲口说的事实、偏好、经历、情绪
✅ 有趣的互动场景（写清谁说了什么）、梗、外号
✅ 身份变化、人际关系、重要事件
❌ 闲聊寒暄、技术调试、纯灌水、AI自我限制
❌ 跟ceci无关的事、脱水流水账、无依据推测

## 记忆格式：
- 每条 = 一个原子事实，≤200字
- 必须脱离原对话后仍能看懂（谁做了什么、为什么、什么场景）
- 保留具体的梗、台词、有趣细节

## about 字段：
- "user" = 仅限ceci本人的事实/心情/偏好
- "interaction" = 群聊互动/梗/别人说的话/多人讨论
- "ai" = AI自省（极少用）

## 时态：
- "以前做过XX" → 写"曾经做过"
- 用户纠正错误认知 → importance ≥ 0.8"""

# ── 小群专用追加 prompt（强调梗和互动细节） ──
EXTRACT_PROMPT_PRIVATE_GROUP = """

## 小群规则：
重点提取：梗/外号/暗语、有趣互动场景（谁怼谁/金句）、群内角色动态、ceci透露的真实信息。
玩笑如果形成了"梗"也值得记。不记纯刷屏水群。"""

# ── 大群专用追加 prompt（严格过滤） ──
EXTRACT_PROMPT_PUBLIC_GROUP = """

## 大群规则：
极其严格筛选。只记ceci亲口说的重要信息和重大事件。bot互动/群友闲聊/灌水一律不记。
80条消息里可能一条都不值得记——输出 [] 完全OK。"""

# ── 输出格式（所有类型共用） ──
EXTRACT_OUTPUT_FORMAT = """

输出 JSON 数组（空数组表示没有值得记的）：
[
  {
    "content": "一个原子事实（≤200字）。写清楚是谁说的/做的，不要用'用户'指代ceci以外的人，不要用泛指'AI'而要写具体bot名字",
    "about": "user 或 interaction 或 ai",
    "room": "最合适的房间ID",
    "importance": 0.0到1.0（闲聊寒暄0.1，临时话题0.3，有信息量的事实0.5+，重要事件/情感/身份变化0.7+）,
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

只输出 JSON。

## 🔴 最后提醒（输出前必检）：
1. 检查每条记忆的主语——是谁说的就写谁，不要全写"ceci"或"用户"
2. 群聊中A说的话不能写成"ceci说"，B的观点不能写成"ceci认为"
3. 不要把多条消息压缩成"ceci在群里讨论了XX"——分开写每个人说了什么
4. 不要脑补对话中没有的情感/动机（"她享受""她认为"需要有原文依据）
5. 如果整段对话没有值得记的，输出 []"""


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


def _buffer_key(ai_id: str, chat_id: str = "", platform: str = "", chat_type: str = "") -> str:
    """按聊天窗口分组。群聊用共享 key 防止三个 bot 各提取一次"""
    if chat_id:
        if chat_type in ("private_group", "public_group"):
            return f"group:chat:{chat_id}"
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
    key = _buffer_key(ai_id, chat_id, platform, chat_type)
    if key not in _conversation_buffers:
        _conversation_buffers[key] = []

    # 记住这个 buffer 的聊天类型
    if chat_type:
        _buffer_chat_types[key] = chat_type

    now = local_now().isoformat()
    _conversation_buffers[key].append({
        "user": user_message[:1000],
        "ai": ai_response[:1000] if ai_response else "",
        "ai_id": ai_id,
        "platform": platform,
        "chat_type": chat_type,
        "timestamp": now,
    })
    _touch_pulse(user_message, ai_response, ai_id)

    # 原文保险箱：不加工的原始对话留档，记忆漂移时可以找回原话
    try:
        import raw_vault
        raw_vault.log_turn(user_message, ai_response, ai_id=ai_id,
                           platform=platform, chat_id=chat_id, chat_type=chat_type)
    except Exception:
        pass

    # 防止内存爆
    if len(_conversation_buffers[key]) > MAX_BUFFER_SIZE:
        _conversation_buffers[key] = _conversation_buffers[key][-MAX_BUFFER_SIZE:]

    buffer_size = len(_conversation_buffers[key])

    # 按聊天类型决定触发阈值
    chunk_size = CHUNK_SIZES.get(chat_type, 30)

    if buffer_size >= chunk_size:
        now_ts = time.time()
        last_ts = _last_extract_time.get(key, 0)
        if now_ts - last_ts < EXTRACT_COOLDOWN:
            return {"status": "buffered", "buffer_size": buffer_size, "cooldown": True}
        if key not in _extract_locks:
            _extract_locks[key] = asyncio.Lock()
        if _extract_locks[key].locked():
            return {"status": "buffered", "buffer_size": buffer_size, "extracting": True}
        # 提取放后台，接口立即返回（不阻塞 bot 回消息）
        _last_extract_time[key] = now_ts

        async def _bg_extract():
            try:
                async with _extract_locks[key]:
                    await _extract_and_remember(key)
            except Exception as e:
                logger.warning(f"background extraction failed for {key}: {e}")

        asyncio.create_task(_bg_extract())
        return {"status": "extracting_async", "buffer_size": buffer_size}

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
    is_group = chat_type in ("private_group", "public_group")
    for entry in buffer:
        user_short = entry['user'][:200]
        ai_short = entry['ai'][:200] if entry['ai'] else ""
        ts = entry['timestamp'][:16]
        if is_group:
            # 群聊：消息里已有发言人名字如 "YanYan(ID:xxx): ..."，不要再加"用户:"前缀
            if ai_short:
                line = f"[{ts}] {user_short}\n  → AI回复: {ai_short}"
            else:
                line = f"[{ts}] {user_short}"
        else:
            # 私聊：只有用户和AI
            if ai_short:
                line = f"[{ts}] ceci: {user_short} | AI: {ai_short}"
            else:
                line = f"[{ts}] ceci: {user_short}"
        if sum(len(l) for l in lines) + len(line) > char_budget:
            break
        lines.append(line)
    conversation_text = "\n".join(lines)

    today = local_today()
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
    max_items = {"private": 5, "private_group": 6, "public_group": 2}.get(chat_type, 8)
    memories = []

    valid_about = {"user", "interaction", "ai"}
    for item in items[:max_items]:
        content = str(item.get("content", "")).strip()
        if not content or len(content) < 10:
            continue
        # 服务端兜底：importance < 0.5 的不存（模型现在可以打0.0-1.0）
        raw_importance = float(item.get("importance", 0.5))
        if raw_importance < 0.5:
            logger.debug(f"Skipping low-importance memory: {content[:50]}...")
            continue

        # about 前缀（防身份混淆）
        about = item.get("about", "user")
        if about not in valid_about:
            about = "user"
        if about == "user" and not content.startswith("[用户]"):
            content = f"[用户] {content}"
        elif about == "interaction" and not content.startswith("[互动]"):
            content = f"[互动] {content}"
        elif about == "ai" and not content.startswith("[AI]"):
            content = f"[AI] {content}"

        # 附上LLM实际看到的对话原文作为源追溯
        source_ctx = conversation_text[:1500]
        is_private_memory = chat_type == "private"
        result = await memory_ops.remember(
            content=content,
            layer="private" if is_private_memory else "shared",
            room=item.get("room", "living_room"),
            owner_ai=ai_id if is_private_memory else "",
            importance=max(0.4, min(1.0, raw_importance)),
            event_date=item.get("event_date", ""),
            source_ai=ai_id,
            source_platform=f"auto_capture:{platform}:{chat_type}",
            source_context=source_ctx,
            auto_analyze=False,
            quick=True,
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
    today = local_today()
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
        # about 前缀
        about = item.get("about", "user")
        if about not in ("user", "interaction", "ai"):
            about = "user"
        if about == "user" and not content.startswith("[用户]"):
            content = f"[用户] {content}"
        elif about == "interaction" and not content.startswith("[互动]"):
            content = f"[互动] {content}"
        elif about == "ai" and not content.startswith("[AI]"):
            content = f"[AI] {content}"
        result = await memory_ops.remember(
            content=content,
            layer="private" if chat_type == "private" else "shared",
            room=item.get("room", "living_room"),
            owner_ai=ai_id if chat_type == "private" else "",
            importance=max(0.1, min(1.0, float(item.get("importance", 0.5)))),
            event_date=item.get("event_date", ""),
            source_ai=ai_id,
            source_platform="mcp_extract",
            auto_analyze=False,
            quick=quick,
        )
        memories.append(result)

    return memories

