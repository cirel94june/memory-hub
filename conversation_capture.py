"""
对话自动捕获 + 分块总结（借鉴 imprint-memory）

流程：
1. 每轮对话通过 API 存入 conversation_log
2. 攒够 N 条后（或定时触发），小模型自动提取结构化记忆
3. 提取的记忆走正常 remember() 流程（自动打标、合并、supersede）

这样记忆不再依赖 AI 主动调 remember，系统自动攒。
"""
import json
import time
import logging
from datetime import datetime, timezone
from typing import Optional

from config import LLM_API_KEY, LLM_MODEL, LLM_BASE_URL

logger = logging.getLogger("memory_hub.capture")

# 内存中的对话缓冲（按 ai_id + platform 分组）
_conversation_buffers: dict[str, list[dict]] = {}

# 触发总结的阈值
CHUNK_SIZE = 20  # 每 20 条对话触发一次总结
MAX_BUFFER_SIZE = 100  # 缓冲区最大条数（防止内存爆）

EXTRACT_PROMPT = """你是一个记忆提取专家。从以下对话记录中，提取值得长期记住的信息。

提取规则：
1. 只提取关于用户的事实、状态变化、偏好、重要事件
2. 忽略闲聊、寒暄、技术调试过程
3. 如果用户提到了时间相关的事（"上周"、"明天"），尽量推算出具体日期
4. 每条记忆用简洁陈述句，一条 = 一个独立事实
5. 不要重复对话原文，要提炼

输出 JSON 数组（空数组表示没有值得记的）：
[
  {
    "content": "提炼后的记忆内容",
    "room": "最合适的房间ID",
    "importance": 0.1到1.0,
    "event_date": "事件日期（如 2026-06-08，推算不出就留空）",
    "resolved": null或false（如果是待办/未完成的事就 false）
  }
]

房间可选：
  living_room(核心身份), career(职业), psychology(心理), health(健康),
  learning(学习), relationships(人际), preferences(偏好),
  work_tasks(工作事务), infra(基建)

只输出 JSON。"""


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


def _buffer_key(ai_id: str, platform: str = "") -> str:
    return f"{ai_id}:{platform or 'unknown'}"


async def log_conversation(
    user_message: str,
    ai_response: str,
    ai_id: str = "claude",
    platform: str = "",
) -> dict:
    """记录一轮对话到缓冲区。如果缓冲区满了，自动触发总结。

    返回：
    - {"status": "buffered", "buffer_size": N} — 正常缓存
    - {"status": "extracted", "memories": [...]} — 触发了总结并提取了记忆
    """
    key = _buffer_key(ai_id, platform)
    if key not in _conversation_buffers:
        _conversation_buffers[key] = []

    now = datetime.now(timezone.utc).isoformat()
    _conversation_buffers[key].append({
        "user": user_message[:1000],  # 截断防止太长
        "ai": ai_response[:1000],
        "ai_id": ai_id,
        "platform": platform,
        "timestamp": now,
    })

    # 防止内存爆
    if len(_conversation_buffers[key]) > MAX_BUFFER_SIZE:
        _conversation_buffers[key] = _conversation_buffers[key][-MAX_BUFFER_SIZE:]

    buffer_size = len(_conversation_buffers[key])

    # 检查是否触发总结
    if buffer_size >= CHUNK_SIZE:
        extracted = await _extract_and_remember(key)
        return {"status": "extracted", "memories": extracted, "buffer_size": 0}

    return {"status": "buffered", "buffer_size": buffer_size}


async def force_extract(ai_id: str = "", platform: str = "") -> dict:
    """手动触发对话总结（不等缓冲区满）。

    如果不指定 ai_id，则处理所有缓冲区。
    """
    results = {}
    if ai_id:
        key = _buffer_key(ai_id, platform)
        if key in _conversation_buffers and _conversation_buffers[key]:
            extracted = await _extract_and_remember(key)
            results[key] = extracted
    else:
        # 处理所有缓冲区
        for key in list(_conversation_buffers.keys()):
            if _conversation_buffers[key]:
                extracted = await _extract_and_remember(key)
                results[key] = extracted

    return {"status": "extracted", "results": results}


async def get_buffer_status() -> dict:
    """查看当前缓冲区状态"""
    status = {}
    for key, buf in _conversation_buffers.items():
        status[key] = {
            "size": len(buf),
            "oldest": buf[0]["timestamp"] if buf else None,
            "newest": buf[-1]["timestamp"] if buf else None,
        }
    return {"buffers": status, "chunk_size": CHUNK_SIZE}


async def _extract_and_remember(buffer_key: str) -> list[dict]:
    """从对话缓冲区提取记忆，走 remember 流程"""
    import memory_ops

    buffer = _conversation_buffers.get(buffer_key, [])
    if not buffer:
        return []

    # 把对话格式化给小模型
    conversation_text = "\n".join([
        f"[{entry['timestamp'][:16]}]\n用户: {entry['user']}\nAI: {entry['ai']}"
        for entry in buffer
    ])

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    prompt = f"今天日期：{today}\n\n以下是最近的对话记录：\n\n{conversation_text[:6000]}"

    raw = await _call_llm(EXTRACT_PROMPT + "\n\n" + prompt)
    if not raw:
        logger.warning(f"Extract LLM returned empty for {buffer_key}")
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
        logger.warning(f"Extract parse failed: {e}")
        items = []

    # 提取的记忆走 remember 流程
    ai_id = buffer[0].get("ai_id", "claude") if buffer else "claude"
    platform = buffer[0].get("platform", "") if buffer else ""
    memories = []

    for item in items[:8]:  # 最多 8 条
        content = str(item.get("content", "")).strip()
        if not content or len(content) < 10:
            continue

        # 附上原始对话片段作为源追溯（最近3轮，截断）
        source_ctx = "\n".join([
            f"用户: {e['user'][:100]}\nAI: {e['ai'][:100]}"
            for e in buffer[-3:]
        ])[:500]

        result = await memory_ops.remember(
            content=content,
            room=item.get("room", "living_room"),
            importance=max(0.1, min(1.0, float(item.get("importance", 0.5)))),
            event_date=item.get("event_date", ""),
            source_ai=ai_id,
            source_platform=f"auto_capture:{platform}",
            source_context=source_ctx,
        )
        memories.append(result)

        # 如果标记为未解决，设置 resolved=False
        if item.get("resolved") == False:
            mem_id = result.get("id")
            if mem_id:
                await memory_ops.resolve_memory(mem_id, resolved=False)

    # 清空已处理的缓冲区
    _conversation_buffers[buffer_key] = []

    logger.info(f"Auto-captured {len(memories)} memories from {buffer_key} ({len(buffer)} messages)")
    return memories
