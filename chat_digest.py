"""
跨窗口对话摘要：让同一个 AI 知道自己在其他聊天窗口聊了什么

每次对话后生成一句话摘要，存入 chat_digests 表。
下次在别的窗口开聊时，自动注入最近几条其他窗口的摘要。
"""
import sqlite3
import logging
from datetime import datetime, timezone
from pathlib import Path

import httpx
from config import LLM_API_KEY, LLM_MODEL, LLM_BASE_URL

logger = logging.getLogger("memory_hub.chat_digest")

DB_PATH = Path(__file__).parent / "data" / "memories.db"


def _connect() -> sqlite3.Connection:
    """独立连接（主连接在 database.py）；加 busy_timeout 防并发写时 database is locked。"""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout=5000")
    return conn

# 按窗口类型分别限制保留条数
RETENTION_LIMITS = {
    "private": 25,
    "small_group": 35,
    "private_group": 35,
    "big_group": 15,
    "public_group": 15,
    "group": 20,
}
RETENTION_DEFAULT = 30


def _init_table():
    conn = _connect()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_digests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ai_id TEXT NOT NULL,
            chat_id TEXT NOT NULL DEFAULT '',
            chat_type TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    try:
        conn.execute("ALTER TABLE chat_digests ADD COLUMN chat_type TEXT NOT NULL DEFAULT ''")
    except Exception:
        pass
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_digests_ai_time "
        "ON chat_digests(ai_id, created_at DESC)"
    )
    conn.commit()
    conn.close()


_init_table()


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
                    "temperature": 0.1,
                    "max_tokens": 150,
                },
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        logger.error(f"Digest LLM error: {e}")
        return ""


async def generate_and_save(
    user_message: str, ai_response: str, ai_id: str,
    chat_id: str = "", chat_type: str = "", reply_reason: str = "",
):
    """生成对话摘要并保存。

    chat_type: private / small_group / big_group
    reply_reason: mentioned / replied / random / ceci / trigger

    大群 + 随机插嘴 → 跳过不记
    """
    if not chat_id or len(user_message.strip()) < 15:
        return

    if chat_type == "big_group" and reply_reason in ("random", ""):
        logger.info(f"[Digest] skip big_group random: {ai_id}@{chat_id}")
        return

    speaker_label = "对方消息" if chat_type == "private" else "群内消息/触发消息"
    prompt = (
        "用一句话（20-50字）概括这段对话的核心话题。"
        "不要加前缀，直接输出；如果是群聊，不要把群内其他人的话概括成用户本人说的。"
        + chr(10) + chr(10)
        + f"{speaker_label}: {user_message[:300]}"
        + chr(10)
        + f"AI: {ai_response[:300]}"
    )
    summary = await _call_llm(prompt)
    if not summary or len(summary) < 5:
        return

    summary = summary.strip().strip('"').strip("。").strip()
    if len(summary) > 100:
        summary = summary[:97] + "..."

    now = datetime.now(timezone.utc).isoformat()
    conn = _connect()
    conn.execute(
        "INSERT INTO chat_digests (ai_id, chat_id, chat_type, summary, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (ai_id, chat_id, chat_type, summary, now),
    )

    limit = RETENTION_LIMITS.get(chat_type, RETENTION_DEFAULT)
    conn.execute(
        "DELETE FROM chat_digests WHERE ai_id = ? AND chat_type = ? AND id NOT IN "
        "(SELECT id FROM chat_digests WHERE ai_id = ? AND chat_type = ? "
        "ORDER BY created_at DESC LIMIT ?)",
        (ai_id, chat_type, ai_id, chat_type, limit),
    )
    conn.commit()
    conn.close()
    logger.info(f"[Digest] {ai_id}@{chat_id}({chat_type}): {summary[:50]}")


def get_recent_digests(
    ai_id: str,
    exclude_chat_id: str = "",
    limit: int = 5,
    include_types: list[str] | None = None,
) -> list[dict]:
    """获取最近的对话摘要，可按聊天类型过滤。"""
    conn = _connect()
    conn.row_factory = sqlite3.Row

    params: list = [ai_id]
    type_filter = ""
    if include_types:
        placeholders = ", ".join(["?"] * len(include_types))
        type_filter = f" AND chat_type IN ({placeholders}) "
        params.extend(include_types)

    exclude_filter = ""
    if exclude_chat_id:
        exclude_filter = " AND chat_id != ? "
        params.append(exclude_chat_id)

    params.append(limit)
    cur = conn.execute(
        "SELECT chat_id, chat_type, summary, created_at FROM chat_digests "
        "WHERE ai_id = ? " + type_filter + exclude_filter +
        "ORDER BY "
        "  CASE chat_type "
        "    WHEN 'private' THEN 0 "
        "    WHEN 'small_group' THEN 1 "
        "    WHEN 'big_group' THEN 2 "
        "    ELSE 1 "
        "  END, "
        "  created_at DESC "
        "LIMIT ?",
        tuple(params),
    )
    results = [dict(r) for r in cur]
    conn.close()
    return results


def get_recent_chat_activity(
    chat_id: str,
    exclude_ai_id: str = "",
    limit: int = 5,
    include_types: list[str] | None = None,
) -> list[dict]:
    """Get recent digests from other AIs in the same chat."""
    if not chat_id:
        return []

    excluded = {exclude_ai_id}
    if exclude_ai_id == "claude":
        excluded.add("cloudy")
    elif exclude_ai_id == "cloudy":
        excluded.add("claude")
    excluded.discard("")

    conn = _connect()
    conn.row_factory = sqlite3.Row

    params: list = [chat_id]
    type_filter = ""
    if include_types:
        placeholders = ", ".join(["?"] * len(include_types))
        type_filter = f" AND chat_type IN ({placeholders}) "
        params.extend(include_types)

    exclude_filter = ""
    if excluded:
        placeholders = ", ".join(["?"] * len(excluded))
        exclude_filter = f" AND ai_id NOT IN ({placeholders}) "
        params.extend(sorted(excluded))

    params.append(limit)
    cur = conn.execute(
        "SELECT ai_id, chat_id, chat_type, summary, created_at FROM chat_digests "
        "WHERE chat_id = ? " + type_filter + exclude_filter +
        "ORDER BY created_at DESC LIMIT ?",
        tuple(params),
    )
    results = [dict(r) for r in cur]
    conn.close()
    return results


def list_recent_digest_threads(limit: int = 20, include_types: list[str] | None = None) -> list[dict]:
    """List recent chat ids that have digests, for wake-preview selection."""
    conn = _connect()
    conn.row_factory = sqlite3.Row
    params: list = []
    type_filter = ""
    if include_types:
        placeholders = ", ".join(["?"] * len(include_types))
        type_filter = f"WHERE chat_type IN ({placeholders})"
        params.extend(include_types)
    params.append(limit)
    cur = conn.execute(
        "SELECT chat_id, chat_type, MAX(created_at) AS last_at, COUNT(*) AS digest_count "
        "FROM chat_digests " + type_filter +
        " GROUP BY chat_id, chat_type ORDER BY last_at DESC LIMIT ?",
        tuple(params),
    )
    rows = [dict(r) for r in cur if r["chat_id"]]
    conn.close()
    return rows
