"""
对话导入：从 JSON 或纯文本对话记录中自动提取记忆 + 用户画像

支持格式：
1. JSON：[{"role": "user/assistant", "content": "..."}, ...]
   或 {"messages": [...]} 或 Telegram 导出格式
2. TXT：自动识别 "用户:" / "AI:" / "小猫:" / "小克:" 等前缀

流程：
1. 解析对话为统一格式
2. 分块（每 30 轮一块，防止 prompt 太长）
3. 小模型提取：事实性记忆 + 用户画像
4. 提取的记忆走 remember() 流程
"""
import json
import re
import logging
from datetime import datetime, timezone

import httpx
from config import LLM_API_KEY, LLM_MODEL, LLM_BASE_URL, ROOMS
import memory_ops

logger = logging.getLogger("memory_hub.import")

CHUNK_SIZE = 30  # 每块对话轮数


async def _call_llm(prompt: str) -> str:
    if not LLM_API_KEY:
        return ""
    url = f"{LLM_BASE_URL}/chat/completions"
    try:
        async with httpx.AsyncClient(timeout=90) as client:
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
        logger.error(f"Import LLM error: {e}")
        return ""


def _parse_conversation(content: str, fmt: str = "auto") -> list[dict]:
    """解析对话为 [{"role": "user"/"assistant", "content": "..."}] 格式"""

    if fmt == "auto":
        content_stripped = content.strip()
        if content_stripped.startswith("[") or content_stripped.startswith("{"):
            fmt = "json"
        else:
            fmt = "txt"

    if fmt == "json":
        return _parse_json(content)
    else:
        return _parse_txt(content)


def _parse_json(content: str) -> list[dict]:
    """解析 JSON 格式对话"""
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return []

    messages = []

    # 格式1: 直接是数组 [{"role": "user", "content": "..."}]
    if isinstance(data, list):
        items = data
    # 格式2: {"messages": [...]}
    elif isinstance(data, dict) and "messages" in data:
        items = data["messages"]
    # 格式3: Telegram 导出 {"chats": {"list": [{"messages": [...]}]}}
    elif isinstance(data, dict) and "chats" in data:
        try:
            chat_list = data["chats"]["list"]
            items = []
            for chat in chat_list:
                items.extend(chat.get("messages", []))
        except (KeyError, TypeError):
            return []
    else:
        return []

    for item in items:
        if not isinstance(item, dict):
            continue

        # 标准 OpenAI 格式
        if "role" in item and "content" in item:
            role = "user" if item["role"] in ("user", "human") else "assistant"
            messages.append({"role": role, "content": str(item["content"])})

        # Telegram 导出格式
        elif "from" in item and ("text" in item or "text_entities" in item):
            text = item.get("text", "")
            if isinstance(text, list):
                text = "".join(
                    part if isinstance(part, str) else part.get("text", "")
                    for part in text
                )
            if not text:
                continue
            # 简单判断：bot 名字包含 AI 相关关键词 → assistant
            sender = str(item.get("from", ""))
            role = "assistant" if any(k in sender.lower() for k in ("bot", "claude", "gpt", "gemini", "小克", "lucien", "jasper")) else "user"
            messages.append({"role": role, "content": text, "_sender": sender})

    return messages


def _parse_txt(content: str) -> list[dict]:
    """解析纯文本对话"""
    messages = []
    user_patterns = re.compile(
        r'^(?:用户|user|human|小猫|主人|猫猫|ceci)\s*[:：]\s*',
        re.IGNORECASE
    )
    ai_patterns = re.compile(
        r'^(?:AI|assistant|bot|小克|cloudy|lucien|jasper|gemini|gpt|claude)\s*[:：]\s*',
        re.IGNORECASE
    )

    lines = content.strip().split("\n")
    current_role = None
    current_text = []

    for line in lines:
        line = line.strip()
        if not line:
            continue

        if user_patterns.match(line):
            if current_role and current_text:
                messages.append({"role": current_role, "content": "\n".join(current_text)})
            current_role = "user"
            current_text = [user_patterns.sub("", line)]
        elif ai_patterns.match(line):
            if current_role and current_text:
                messages.append({"role": current_role, "content": "\n".join(current_text)})
            current_role = "assistant"
            current_text = [ai_patterns.sub("", line)]
        elif current_role:
            current_text.append(line)
        else:
            # 没有前缀，猜测交替（第一条是 user）
            role = "user" if len(messages) % 2 == 0 else "assistant"
            messages.append({"role": role, "content": line})

    if current_role and current_text:
        messages.append({"role": current_role, "content": "\n".join(current_text)})

    return messages


def _chunk_messages(messages: list[dict], chunk_size: int = CHUNK_SIZE) -> list[list[dict]]:
    """把消息分块"""
    chunks = []
    for i in range(0, len(messages), chunk_size):
        chunks.append(messages[i:i + chunk_size])
    return chunks


async def _extract_from_chunk(chunk: list[dict], ai_id: str, chunk_index: int, total_chunks: int) -> list[dict]:
    """从一块对话中提取记忆 + 用户画像"""

    room_list = "\n".join([
        f"  - {k}: {v['name']}（{v.get('description', '')}）"
        for k, v in ROOMS.items()
        if v.get("type") != "isolated"
    ])

    conversation_text = "\n".join([
        f"{'用户' if m['role'] == 'user' else 'AI'}: {m['content'][:300]}"
        for m in chunk
    ])

    is_first = chunk_index == 0

    profile_instruction = ""
    if is_first:
        profile_instruction = """
## 额外任务：用户画像
这是导入对话的第一块。请同时提取用户画像信息：
- 用户的身份特征（年龄、性别、职业、所在城市等）
- 性格特点和沟通风格
- 核心关系（和 AI 的关系、提到的重要人物）
把画像信息也作为记忆条目输出，room 用 living_room，importance 给 0.8+。
"""

    prompt = f"""你是一个严格的记忆提取专家。从以下导入的对话记录（第 {chunk_index+1}/{total_chunks} 块）中，提取值得**长期**记住的信息。
{profile_instruction}
## 提取规则（宁缺毋滥）：
- 只提取用户透露的事实性信息：身份、偏好、重要事件、人际关系、计划
- 忽略闲聊、玩笑、日常问候、无实质内容的对话
- ⚡ 记忆原子化：每条记忆 = 一个独立的原子事实，≤200字（保留具体细节如台词、梗）
- 宁可多拆几条，不要把多个事实塞进一条
- 大部分对话不值得记——这很正常

## 可用房间：
{room_list}

对话内容：
{conversation_text[:5000]}

输出 JSON 数组（空数组 = 没有值得记的）：
[
  {{
    "content": "一个原子事实（≤200字，保留具体细节）",
    "room": "房间ID",
    "importance": 0.4到1.0,
    "event_date": "事件日期或空字符串"
  }}
]
只输出 JSON。"""

    raw = await _call_llm(prompt)
    if not raw:
        return []

    try:
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0]
        items = json.loads(raw)
        if not isinstance(items, list):
            return []
    except Exception:
        return []

    # 存入 Memory Hub
    memories = []
    for item in items[:10]:
        content = str(item.get("content", "")).strip()
        if not content or len(content) < 5:
            continue
        imp = float(item.get("importance", 0.5))
        if imp < 0.3:
            continue

        result = await memory_ops.remember(
            content=content,
            room=item.get("room", "living_room"),
            importance=imp,
            event_date=item.get("event_date", ""),
            source_ai=ai_id,
            source_platform="import",
        )
        memories.append({"content": content, "room": item.get("room"), **result})

    return memories


async def import_conversation(
    content: str,
    format: str = "auto",
    ai_id: str = "claude",
    platform: str = "import",
) -> dict:
    """主入口：导入对话并自动提取记忆"""

    # 1. 解析
    messages = _parse_conversation(content, format)
    if not messages:
        return {"status": "error", "message": "无法解析对话内容，请检查格式"}

    user_count = sum(1 for m in messages if m["role"] == "user")
    ai_count = len(messages) - user_count

    # 2. 分块
    chunks = _chunk_messages(messages)

    # 3. 逐块提取
    all_memories = []
    for i, chunk in enumerate(chunks):
        chunk_memories = await _extract_from_chunk(chunk, ai_id, i, len(chunks))
        all_memories.extend(chunk_memories)

    return {
        "status": "success",
        "parsed_messages": len(messages),
        "user_messages": user_count,
        "ai_messages": ai_count,
        "chunks_processed": len(chunks),
        "memories_extracted": len(all_memories),
        "memories": all_memories,
    }
