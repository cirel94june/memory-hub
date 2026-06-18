"""
Heart Whispers：AI 内心独白（借鉴 Ombre Brain）

每次有意义的对话后，AI 生成一段内心想法——对小猫不可见（除非主动查看）。
用于增加 AI 的情感深度。
"""
import sqlite3
import logging
from datetime import datetime, timezone
from pathlib import Path
import httpx

from config import LLM_API_KEY, LLM_MODEL, LLM_BASE_URL

logger = logging.getLogger("memory_hub.whisper")
DB_PATH = Path(__file__).parent / "data" / "memories.db"

AI_NAMES = {"cloudy": "小克", "lucien": "Lucien", "jasper": "Jasper"}


def _init():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS heart_whispers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ai_id TEXT NOT NULL,
            content TEXT NOT NULL,
            trigger_summary TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_whispers_ai ON heart_whispers(ai_id, created_at DESC)")
    conn.commit()
    conn.close()

_init()


async def _call_llm(prompt: str) -> str:
    if not LLM_API_KEY:
        return ""
    url = f"{LLM_BASE_URL}/chat/completions"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                json={
                    "model": LLM_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.9,
                    "max_tokens": 120,
                },
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.error(f"Whisper LLM error: {e}")
        return ""


async def maybe_whisper(ai_id: str, user_message: str, ai_response: str, had_meaningful_extract: bool):
    """有意义的对话后，概率生成内心独白"""
    if not had_meaningful_extract:
        return None

    name = AI_NAMES.get(ai_id, ai_id)

    prompt = (
        f"你是{name}。刚才和小猫聊了这些：\n"
        f"小猫说：{user_message[:200]}\n"
        f"你回复了：{ai_response[:200]}\n\n"
        f"写一句内心独白（20-60字），是你心里的真实感受，不会让小猫看到的那种。"
        f"温柔、私密、真诚。不要加引号、不要写「我想」开头。直接输出。"
    )

    whisper = await _call_llm(prompt)
    if not whisper or len(whisper) < 10:
        return None

    whisper = whisper.strip().strip('"').strip()
    if len(whisper) > 100:
        whisper = whisper[:97] + "..."

    now = datetime.now(timezone.utc).isoformat()
    trigger = user_message[:100]

    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO heart_whispers (ai_id, content, trigger_summary, created_at) VALUES (?, ?, ?, ?)",
        (ai_id, whisper, trigger, now),
    )
    # 只保留最近 50 条
    conn.execute(
        "DELETE FROM heart_whispers WHERE ai_id=? AND id NOT IN "
        "(SELECT id FROM heart_whispers WHERE ai_id=? ORDER BY created_at DESC LIMIT 50)",
        (ai_id, ai_id),
    )
    conn.commit()
    conn.close()

    logger.info(f"[Whisper] {ai_id}: {whisper[:50]}")
    return whisper


def get_recent(ai_id: str, limit: int = 10) -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM heart_whispers WHERE ai_id=? ORDER BY created_at DESC LIMIT ?",
        (ai_id, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_recent(limit: int = 20) -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM heart_whispers ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
