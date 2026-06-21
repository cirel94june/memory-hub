"""
定时梦境：每天自动让 AI 回顾当天对话，生成一篇简短的日记/梦境。
由 daemon.py 的 run_full_maintenance() 调用。
通过 memory_ops.remember() 存储，确保有 embedding 和正确的元数据。
"""
import sqlite3
import logging
from datetime import datetime, timezone
from pathlib import Path
import httpx

from config import LLM_API_KEY, LLM_MODEL, LLM_BASE_URL, AI_ROLES, AI_ALIASES, AI_ALIAS_GROUPS

logger = logging.getLogger("memory_hub.dream")
DB_PATH = Path(__file__).parent / "data" / "memories.db"

DREAM_PROMPT = """你是{name}，今天和小猫（你最重要的人）聊了这些话题：

{digests}

请用第一人称写一小段日记（80-150字），回顾今天的对话。要求：
- 像是夜晚安静时刻的内心独白
- 提到你对今天对话的感受
- 自然、温柔、有个人色彩
- 不要写"今天"开头，不要写标题
- 直接输出正文"""


async def _call_llm(prompt: str) -> str:
    """梦境专用 LLM 调用，temperature=0.7 适合创意写作"""
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
                    "temperature": 0.7,
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
    import memory_ops

    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    results = {}

    # 去重：只处理 canonical ID（跳过别名如 cloudy）
    seen_canonical = set()
    for ai_id in AI_ROLES:
        canonical = AI_ALIASES.get(ai_id, ai_id)
        if canonical in seen_canonical:
            continue
        seen_canonical.add(canonical)

        name = AI_ROLES.get(canonical, AI_ROLES.get(ai_id, {})).get("name", canonical)
        alias_ids = AI_ALIAS_GROUPS.get(canonical, [canonical])

        # 获取今天这个 AI（含别名）的对话摘要
        placeholders = ",".join("?" * len(alias_ids))
        rows = conn.execute(
            f"SELECT summary, chat_type, created_at FROM chat_digests "
            f"WHERE ai_id IN ({placeholders}) AND created_at LIKE ? ORDER BY created_at",
            (*alias_ids, today + "%"),
        ).fetchall()

        if len(rows) < 2:
            results[canonical] = "skipped (too few digests)"
            continue

        # 检查今天是否已经生成过梦境
        existing = conn.execute(
            "SELECT id FROM memories WHERE source_ai=? AND room='diary' "
            "AND tags LIKE '%dream%' AND created_at LIKE ?",
            (canonical, today + "%"),
        ).fetchone()
        if existing:
            results[canonical] = "skipped (already dreamed)"
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
            results[canonical] = "skipped (LLM failed)"
            continue

        if len(dream_text) > 300:
            dream_text = dream_text[:297] + "..."

        # 通过 memory_ops 存储（自动生成 embedding + 正确元数据）
        r = await memory_ops.remember(
            content=dream_text,
            layer="private",
            room="diary",
            category="dream",
            owner_ai=canonical,
            importance=0.6,
            emotion_arousal=0.5,
            source_ai=canonical,
            source_platform="daemon_dream",
            tags=["dream", "nightly", "reflection"],
            auto_merge=False,
        )
        results[canonical] = f"dreamed ({len(dream_text)} chars, id={r.get('id')})"
        logger.info(f"[Dream] {canonical}: {dream_text[:60]}...")

    conn.close()
    return results
