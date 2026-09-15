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
    try:
        conn.execute("BEGIN IMMEDIATE")
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
        existing = {row[1] for row in conn.execute("PRAGMA table_info(raw_events)").fetchall()}
        if "turn_id" not in existing:
            conn.execute("ALTER TABLE raw_events ADD COLUMN turn_id TEXT NOT NULL DEFAULT ''")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_raw_time ON raw_events(created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_raw_ai ON raw_events(ai_id, created_at DESC)")
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


_init_db()


def log_turn(user_message: str, ai_response: str, ai_id: str = "",
             platform: str = "", chat_id: str = "", chat_type: str = "",
             turn_id: str = ""):
    """记录一轮原始对话。任何失败都不往外抛——保险箱故障不能影响聊天。"""
    if not (user_message or "").strip() and not (ai_response or "").strip():
        return
    try:
        conn = _connect()
        conn.execute(
            "INSERT INTO raw_events (ai_id, platform, chat_id, chat_type, "
            "user_text, ai_text, created_at, turn_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ai_id, platform, str(chat_id), chat_type,
             (user_message or "")[:4000], (ai_response or "")[:4000],
             datetime.now(timezone.utc).isoformat(timespec="seconds"),
             turn_id or ""),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning(f"raw_vault log failed: {e}")


_LIKE_ESCAPE_TABLE = str.maketrans({"%": "\\%", "_": "\\_", "\\": "\\\\"})
_GROUP_CHAT_TYPES = ("public_group", "group", "supergroup", "private_group")
_VALID_SPEAKER_FILTERS = {"", "user", "ai"}
_LIMIT_MAX = 50


def _split_words(query: str) -> list[str]:
    parts = query.strip().split()
    return [p for p in parts if p]


def _like_clauses_for_keyword(kw: str, speaker_filter: str,
                              params: list) -> str:
    escaped = kw.translate(_LIKE_ESCAPE_TABLE)
    like = f"%{escaped}%"
    if speaker_filter == "user":
        params.append(like)
        return "user_text LIKE ? ESCAPE '\\'"
    elif speaker_filter == "ai":
        params.append(like)
        return "ai_text LIKE ? ESCAPE '\\'"
    else:
        params.extend([like, like])
        return "(user_text LIKE ? ESCAPE '\\' OR ai_text LIKE ? ESCAPE '\\')"


def search(query: str, ai_id: str = "", limit: int = 10,
           speaker_filter: str = "") -> list[dict]:
    """按关键词查原话（自动拆词 + 同义词展开 + 多词命中排序）。

    隔离策略：
    - 私聊 (chat_type='private')：按 ai_id 过滤，每个 AI 只看自己的
    - 群聊 (private_group/public_group/group/supergroup)：所有 AI 都能搜
    - ai_id 为空时：只搜群聊（不含私聊）
    """
    if not (query or "").strip():
        return []

    speaker_filter = (speaker_filter or "").strip().casefold()
    if speaker_filter not in _VALID_SPEAKER_FILTERS:
        return []

    limit = max(1, min(int(limit) if isinstance(limit, (int, float)) else 10, _LIMIT_MAX))

    import synonym_pairs
    words = _split_words(query)
    if not words:
        return []

    word_synonym_groups: list[list[str]] = [synonym_pairs.expand(w) for w in words]

    conn = _connect()
    conn.row_factory = sqlite3.Row

    where_params: list = []
    word_clauses = []
    for kw_group in word_synonym_groups:
        syn_clauses = [_like_clauses_for_keyword(kw, speaker_filter, where_params)
                       for kw in kw_group]
        word_clauses.append("(" + " OR ".join(syn_clauses) + ")")

    any_word_where = "(" + " OR ".join(word_clauses) + ")"

    if ai_id:
        gp = ",".join("?" for _ in _GROUP_CHAT_TYPES)
        isolation = (
            f" AND (LOWER(TRIM(COALESCE(chat_type,''))) IN ({gp})"
            " OR (LOWER(TRIM(COALESCE(chat_type,''))) = 'private' AND ai_id = ?))"
        )
        where_params.extend(_GROUP_CHAT_TYPES)
        where_params.append(ai_id)
    else:
        gp = ",".join("?" for _ in _GROUP_CHAT_TYPES)
        isolation = f" AND LOWER(TRIM(COALESCE(chat_type,''))) IN ({gp}) "
        where_params.extend(_GROUP_CHAT_TYPES)

    if len(word_synonym_groups) > 1:
        score_params: list = []
        score_parts = []
        for kw_group in word_synonym_groups:
            syn_parts = [_like_clauses_for_keyword(kw, speaker_filter, score_params)
                         for kw in kw_group]
            score_parts.append("CASE WHEN " + " OR ".join(syn_parts) + " THEN 1 ELSE 0 END")
        score_expr = " + ".join(score_parts)
        all_params = score_params + where_params + [limit]
        sql = (
            f"SELECT *, ({score_expr}) AS _match_score "
            f"FROM raw_events WHERE {any_word_where}{isolation} "
            "ORDER BY _match_score DESC, created_at DESC LIMIT ?"
        )
    else:
        all_params = where_params + [limit]
        sql = (
            "SELECT id, ai_id, platform, chat_id, chat_type, "
            "user_text, ai_text, created_at "
            f"FROM raw_events WHERE {any_word_where}{isolation} "
            "ORDER BY created_at DESC LIMIT ?"
        )

    cur = conn.execute(sql, tuple(all_params))
    rows = []
    for r in cur:
        d = dict(r)
        d.pop("_match_score", None)
        rows.append(d)
    conn.close()
    return rows


def get_recent_turns(ai_id: str, limit: int = 4) -> list[dict]:
    """获取该 AI 最近几轮原始对话（用于走廊 fallback，截断到合理长度）。
    自动展开别名组：cloudy/claude 视为同一 AI。"""
    if not ai_id:
        return []
    limit = max(1, min(limit, 10))
    try:
        from config import AI_ALIASES, AI_ALIAS_GROUPS
        canonical = AI_ALIASES.get(ai_id, ai_id)
        ai_ids = AI_ALIAS_GROUPS.get(canonical, [ai_id])
    except Exception:
        ai_ids = [ai_id]
    conn = _connect()
    conn.row_factory = sqlite3.Row
    placeholders = ",".join("?" for _ in ai_ids)
    cur = conn.execute(
        f"SELECT user_text, ai_text, created_at, "
        f"COALESCE(turn_id, '') AS turn_id FROM raw_events "
        f"WHERE ai_id IN ({placeholders}) ORDER BY created_at DESC LIMIT ?",
        (*ai_ids, limit),
    )
    rows = []
    for r in cur:
        user = (r["user_text"] or "")[:120]
        ai = (r["ai_text"] or "")[:120]
        rows.append({"user": user, "ai": ai, "created_at": r["created_at"],
                      "turn_id": r["turn_id"]})
    conn.close()
    return rows


def stats(public_only: bool = False, ai_id: str = "") -> dict:
    """统计原文条数。

    隔离口径与 search() 一致：
    - ai_id 非空：群聊全部 + 该 AI 的私聊
    - ai_id 为空 / public_only=True：只统计群聊
    - 两者都为空且 public_only=False：统计全部（doctor_report 用）
    """
    conn = _connect()
    if ai_id:
        gp = ",".join("?" for _ in _GROUP_CHAT_TYPES)
        cur = conn.execute(
            "SELECT COUNT(*), MIN(created_at), MAX(created_at) FROM raw_events "
            f"WHERE (LOWER(TRIM(COALESCE(chat_type,''))) IN ({gp})"
            " OR (LOWER(TRIM(COALESCE(chat_type,'')) ) = 'private' AND ai_id = ?))",
            (*_GROUP_CHAT_TYPES, ai_id),
        )
    elif public_only:
        gp = ",".join("?" for _ in _GROUP_CHAT_TYPES)
        cur = conn.execute(
            "SELECT COUNT(*), MIN(created_at), MAX(created_at) FROM raw_events "
            f"WHERE LOWER(TRIM(COALESCE(chat_type, ''))) IN ({gp})",
            _GROUP_CHAT_TYPES,
        )
    else:
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
