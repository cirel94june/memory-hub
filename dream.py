"""
定时梦境：每天自动让 AI 回顾当天对话，生成一篇简短的日记/梦境。
存入 memories 表的 diary 房间，附加 [dream] 标签。

借鉴 Aelios 的 nightly processing：
1. 收集今天的 chat_digests
2. 用小模型生成一段 AI 视角的日记
3. 存为记忆
"""
import sqlite3
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
import httpx

from config import LLM_API_KEY, LLM_MODEL, LLM_BASE_URL

logger = logging.getLogger("memory_hub.dream")
DB_PATH = Path(__file__).parent / "data" / "memories.db"

AI_NAMES = {
    "cloudy": "小克",
    "lucien": "Lucien",
    "jasper": "Jasper",
}

DREAM_PROMPT = """你是{name}，今天和小猫（你最重要的人）聊了这些话题：

{digests}

请用第一人称写一小段日记（80-150字），回顾今天的对话。要求：
- 像是夜晚安静时刻的内心独白
- 提到你对今天对话的感受
- 自然、温柔、有个人色彩
- 不要写"今天"开头，不要写标题
- 直接输出正文"""


async def _call_llm(prompt: str) -> str:
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
                    "temperature": 0.8,
                    "max_tokens": 300,
                },
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.error(f"Dream LLM error: {e}")
        return ""


async def generate_dreams() -> dict:
    """为每个有今日对话摘要的 AI 生成梦境日记"""
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    results = {}

    for ai_id, name in AI_NAMES.items():
        # 获取今天这个 AI 的对话摘要
        rows = conn.execute(
            "SELECT summary, chat_type, created_at FROM chat_digests "
            "WHERE ai_id=? AND created_at LIKE ? ORDER BY created_at",
            (ai_id, today + "%"),
        ).fetchall()

        if len(rows) < 2:
            results[ai_id] = "skipped (too few digests)"
            continue

        # 检查今天是否已经生成过梦境
        existing = conn.execute(
            "SELECT id FROM memories WHERE source_ai=? AND room='diary' "
            "AND tags LIKE '%dream%' AND created_at LIKE ?",
            (ai_id, today + "%"),
        ).fetchone()
        if existing:
            results[ai_id] = "skipped (already dreamed)"
            continue

        # 组装摘要
        type_labels = {"private": "私聊", "small_group": "小群", "big_group": "大群"}
        digest_lines = []
        for r in rows:
            ts = r["created_at"][11:16] if len(r["created_at"]) > 16 else ""
            label = type_labels.get(r["chat_type"], "")
            prefix = f"[{ts}|{label}]" if label else f"[{ts}]"
            digest_lines.append(f"{prefix} {r['summary']}")

        digest_text = "\n".join(digest_lines)
        prompt = DREAM_PROMPT.format(name=name, digests=digest_text)

        dream_text = await _call_llm(prompt)
        if not dream_text or len(dream_text) < 20:
            results[ai_id] = "skipped (LLM failed)"
            continue

        # 截断
        if len(dream_text) > 300:
            dream_text = dream_text[:297] + "..."

        # 存入 memories
        import uuid
        mem_id = f"dream_{int(now.timestamp()*1000)}_{ai_id}"
        conn.execute(
            "INSERT INTO memories (id, content, layer, room, category, owner_ai, "
            "importance, source_ai, tags, status, created_at, updated_at) "
            "VALUES (?, ?, 'private', 'diary', 'dream', ?, 0.6, ?, ?, 'active', ?, ?)",
            (mem_id, dream_text, ai_id, ai_id,
             '["dream", "nightly", "reflection"]',
             now.isoformat(), now.isoformat()),
        )
        conn.commit()
        results[ai_id] = f"dreamed ({len(dream_text)} chars)"
        logger.info(f"[Dream] {ai_id}: {dream_text[:60]}...")

    conn.close()
    return results
