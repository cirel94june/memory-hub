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
import database

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
EXTRACT_PROMPT_BASE = """记忆提取：从对话中提取值得长期记住的原子事实。

## 身份：
- "用户"/"主人" = ceci（也叫小猫）。只提取跟 ceci 相关的信息
- Jasper/狗蛋、Lucien、Cloudy/小克 是AI bot，其他人是群友
- 不用泛称"AI"，写具体名字。谁说的写谁，别人说的不能写成"ceci说"

## 三条铁律：
1. **只记原文说了什么，不分析为什么** — 禁止"表现出对XX的喜爱""显示出XX倾向"这类分析
2. **玩笑/角色扮演/打闹 → speech_mode="playful"或"fictional"** — 不当事实记
3. **一条记忆 = 一个原子事实（≤200字）** — 记发生了什么，不归纳性格

## 记什么：ceci 亲口说的事实、偏好、重要事件、纠正(importance≥0.8)
## 不记什么：闲聊寒暄、玩笑段子、无依据推测、AI自我限制

## about 字段：
- "user" = ceci本人的事实/偏好
- "interaction" = 互动事件（写清谁和谁）
- "ai" = AI自省（极少）

## 出处（provenance）：
- "user_statement" = ceci亲口说的
- "user_correction" = ceci在纠正错误（附 corrects_old_value 字段）
- "ai_summary" = bot复述（bot说的永远不能标user_statement）
- "roleplay_meme" = 玩梗/角色扮演

## 纠正识别：
ceci 说"不是X是Y""你记错了"→ provenance="user_correction"，附 corrects_old_value。"""

# ── 小群专用追加 prompt（强调梗和互动细节） ──
EXTRACT_PROMPT_PRIVATE_GROUP = """

## 小群规则：
重点提取：ceci透露的真实信息（状态变化/事实更新/待办）优先于一切娱乐内容。
梗/外号/暗语、有趣互动场景（谁怼谁/金句）也值得记，但有配额：
**同一场对话的玩梗/社交互动最多输出 1 条聚合摘要**（把这场的梗合并成一条写），
不要把一场玩梗拆成多条记忆。状态变化、纠正、待办不受此限制。"""

# ── 大群专用追加 prompt（严格过滤） ──
EXTRACT_PROMPT_PUBLIC_GROUP = """

## 大群规则：
极其严格筛选。只记ceci亲口说的重要信息和重大事件。bot互动/群友闲聊/灌水一律不记。
80条消息里可能一条都不值得记——输出 [] 完全OK。"""

# ── 输出格式（所有类型共用） ──
EXTRACT_OUTPUT_FORMAT = """

输出 JSON 数组（空=没有值得记的）：
[
  {
    "content": "原子事实（≤200字），记发生了什么，不分析性格。写具体名字不用泛称'AI'",
    "about": "user/interaction/ai",
    "room": "房间ID",
    "importance": 0.0到1.0,
    "event_date": "事件日期或留空",
    "provenance": "user_statement/user_correction/ai_summary/ai_speculation/roleplay_meme",
    "claim_type": "fact（亲口说）/observation（观察到）/hypothesis（推测）",
    "speech_mode": "literal（正经）/playful（玩笑打闹）/fictional（角色扮演）/uncertain",
    "subject_name": "关于谁（人名，多人留空）",
    "speaker_name": "谁说的（人名）",
    "corrects_old_value": "仅user_correction时填",
    "resolved": null（默认）或 false（仅"要做某事/待办/提醒我"时用）
  }
]

房间：living_room/career/psychology/health/learning/relationships/preferences/work_tasks/infra/social/relationship/diary/personality/game_room
preferences 只收长期稳定的偏好（"怕蜘蛛"级别），一次性事件不进 preferences。

犹豫就不记。只输出 JSON。"""


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

        try:
            from main import enqueue_background
            enqueue_background(_bg_extract(), f"extract/{key}")
        except ImportError:
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
        entry_ai = entry.get('ai_id', 'AI')
        if is_group:
            # 群聊：消息里已有发言人名字如 "YanYan(ID:xxx): ..."，不要再加"用户:"前缀
            if ai_short:
                line = f"[{ts}] {user_short}\n  → {entry_ai}: {ai_short}"
            else:
                line = f"[{ts}] {user_short}"
        else:
            # 私聊：只有用户和AI
            if ai_short:
                line = f"[{ts}] ceci: {user_short} | {entry_ai}: {ai_short}"
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

        # 出处标注：bot 复述与用户陈述分开，防止 AI 错误版本覆盖用户事实
        valid_prov = {"user_statement", "user_correction", "user_quote",
                      "ai_summary", "ai_speculation", "roleplay_meme"}
        provenance = item.get("provenance", "")
        if provenance not in valid_prov:
            provenance = ""

        # 玩梗封顶：一次性场景幽默不允许以高 importance 永久驻留
        # （decay 侧对 roleplay_meme 也走快衰减，双保险）
        if provenance == "roleplay_meme":
            raw_importance = min(raw_importance, 0.55)

        # 附上LLM实际看到的对话原文作为源追溯
        source_ctx = conversation_text[:1500]
        is_private_memory = chat_type == "private"

        if provenance == "user_correction":
            # 用户在纠正错误信息：错误版失效，纠正版成为 canonical
            result = await memory_ops.apply_user_correction(
                corrected_value=content,
                old_value=str(item.get("corrects_old_value", "")).strip(),
                source_ai=ai_id,
                room=item.get("room", "living_room"),
                source_context=source_ctx,
                layer="private" if is_private_memory else "shared",
                owner_ai=ai_id if is_private_memory else "",
            )
            memories.append(result)
            continue

        subj_name = item.get("subject_name", "")
        spkr_name = item.get("speaker_name", "")
        subject_id = database.resolve_alias(subj_name) or "" if subj_name else ""
        source_speaker_id = database.resolve_alias(spkr_name) or "" if spkr_name else ""

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
            provenance_type=provenance,
            claim_type=item.get("claim_type", ""),
            speech_mode=item.get("speech_mode", ""),
            subject_id=subject_id,
            source_speaker_id=source_speaker_id,
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

        # 出处 + 纠错路由（与自动捕获管线一致）
        valid_prov = {"user_statement", "user_correction", "user_quote",
                      "ai_summary", "ai_speculation", "roleplay_meme"}
        provenance = item.get("provenance", "")
        if provenance not in valid_prov:
            provenance = ""

        if provenance == "user_correction":
            result = await memory_ops.apply_user_correction(
                corrected_value=content,
                old_value=str(item.get("corrects_old_value", "")).strip(),
                source_ai=ai_id,
                room=item.get("room", "living_room"),
                source_context=conversation_text[:1500],
                layer="private" if chat_type == "private" else "shared",
                owner_ai=ai_id if chat_type == "private" else "",
            )
            memories.append(result)
            continue

        subj_name = item.get("subject_name", "")
        spkr_name = item.get("speaker_name", "")
        subject_id = database.resolve_alias(subj_name) or "" if subj_name else ""
        source_speaker_id = database.resolve_alias(spkr_name) or "" if spkr_name else ""

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
            provenance_type=provenance,
            claim_type=item.get("claim_type", ""),
            speech_mode=item.get("speech_mode", ""),
            subject_id=subject_id,
            source_speaker_id=source_speaker_id,
        )
        memories.append(result)

    return memories

