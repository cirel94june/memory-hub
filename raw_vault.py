"""
原文保险箱（Raw Event Vault，参考 Ombre Brain 二改 raw_events.py）
只存用户和 AI 的原始对话文本，永不加工、永不合并、永不总结。

用途：
- 记忆漂移时（总结/合并把细节改错）能找回当时的原话
- 体检（memory_doctor）对照原文验证记忆归属是否张冠李戴
- 人工审计："这条记忆当时到底是怎么说的？"

存储：data/raw_events.db（独立 SQLite，不进 git，不参与记忆召回）
保留：默认 120 天，daemon 定期清理
"""
import sqlite3
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

log = logging.getLogger("raw_vault")

DB_PATH = Path(__file__).parent / "data" / "raw_events.db"


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _init_db():
    conn = _connect()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS raw_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ai_id TEXT NOT NULL DEFAULT '',
            platform TEXT NOT NULL DEFAULT '',
            chat_id TEXT NOT NULL DEFAULT '',
            chat_type TEXT NOT NULL DEFAULT '',
            user_text TEXT NOT NULL DEFAULT '',
            ai_text TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_raw_time ON raw_events(created_at DESC)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_raw_ai ON raw_events(ai_id, created_at DESC)")
    conn.commit()
    conn.close()


_init_db()


def log_turn(user_message: str, ai_response: str, ai_id: str = "",
             platform: str = "", chat_id: str = "", chat_type: str = ""):
    """记录一轮原始对话。任何失败都不往外抛——保险箱故障不能影响聊天。"""
    if not (user_message or "").strip() and not (ai_response or "").strip():
        return
    try:
        conn = _connect()
        conn.execute(
            "INSERT INTO raw_events (ai_id, platform, chat_id, chat_type, user_text, ai_text, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ai_id, platform, str(chat_id), chat_type,
             (user_message or "")[:4000], (ai_response or "")[:4000],
             datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning(f"raw_vault log failed: {e}")


_LIKE_ESCAPE_TABLE = str.maketrans({"%": "\\%", "_": "\\_", "\\": "\\\\"})
_PUBLIC_CHAT_TYPES = ("public_group", "group", "supergroup")
_VALID_SPEAKER_FILTERS = {"", "user", "ai"}
_LIMIT_MAX = 50


def search(query: str, ai_id: str = "", limit: int = 10,
           speaker_filter: str = "") -> list[dict]:
    """按关键词查原话（LIKE 匹配 + 同义词展开，新的在前）。

    speaker_filter: "" → 搜全部, "user" → 只搜 user_text, "ai" → 只搜 ai_text
    ai_id: 必须是服务端验证过的身份。传值 → 含该 AI 私聊；空 → 只搜公开对话。
    """
    if not (query or "").strip():
        return []

    speaker_filter = (speaker_filter or "").strip().casefold()
    if speaker_filter not in _VALID_SPEAKER_FILTERS:
        return []

    limit = max(1, min(int(limit) if isinstance(limit, (int, float)) else 10, _LIMIT_MAX))

    import synonym_pairs
    keywords = synonym_pairs.expand(query.strip())

    conn = _connect()
    conn.row_factory = sqlite3.Row
    params: list = []

    text_clauses = []
    for kw in keywords:
        escaped = kw.translate(_LIKE_ESCAPE_TABLE)
        like = f"%{escaped}%"
        if speaker_filter == "user":
            text_clauses.append("user_text LIKE ? ESCAPE '\\'")
            params.append(like)
        elif speaker_filter == "ai":
            text_clauses.append("ai_text LIKE ? ESCAPE '\\'")
            params.append(like)
        else:
            text_clauses.append("(user_text LIKE ? ESCAPE '\\' OR ai_text LIKE ? ESCAPE '\\')")
            params.extend([like, like])

    text_where = "(" + " OR ".join(text_clauses) + ")"

    extra = ""
    if ai_id:
        extra += " AND ai_id = ? "
        params.append(ai_id)
    else:
        placeholders = ",".join("?" for _ in _PUBLIC_CHAT_TYPES)
        extra += f" AND LOWER(TRIM(COALESCE(chat_type, ''))) IN ({placeholders}) "
        params.extend(_PUBLIC_CHAT_TYPES)

    params.append(limit)
    cur = conn.execute(
        "SELECT id, ai_id, platform, chat_id, chat_type, user_text, ai_text, created_at "
        f"FROM raw_events WHERE {text_where}{extra}"
        "ORDER BY created_at DESC LIMIT ?",
        tuple(params),
    )
    rows = [dict(r) for r in cur]
    conn.close()
    return rows


def stats() -> dict:
    conn = _connect()
    cur = conn.execute("SELECT COUNT(*), MIN(created_at), MAX(created_at) FROM raw_events")
    count, oldest, newest = cur.fetchone()
    conn.close()
    return {"count": count or 0, "oldest": oldest or "", "newest": newest or ""}


def prune(keep_days: int = 120) -> int:
    """清理超过保留期的原文，返回删除条数。"""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat(timespec="seconds")
    conn = _connect()
    cur = conn.execute("DELETE FROM raw_events WHERE created_at < ?", (cutoff,))
    deleted = cur.rowcount
    conn.commit()
    conn.close()
    if deleted:
        log.info(f"raw_vault pruned {deleted} events older than {keep_days}d")
    return deleted
