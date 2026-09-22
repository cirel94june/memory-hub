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
import math
import struct
import sqlite3
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

from config import EMBEDDING_DIM

log = logging.getLogger("raw_vault")

_EMBED_TEXT_LIMIT = 500

DB_PATH = Path(__file__).parent / "data" / "raw_events.db"


def _connect(load_vec: bool = False) -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    if load_vec:
        try:
            import sqlite_vec
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
        except Exception:
            pass
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
        if "embedding" not in existing:
            conn.execute("ALTER TABLE raw_events ADD COLUMN embedding BLOB")
        for col in ("thread_id", "message_id", "sender_id", "sender_type", "reply_to_id"):
            if col not in existing:
                conn.execute(f"ALTER TABLE raw_events ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
        if "extract_status" not in existing:
            conn.execute("ALTER TABLE raw_events ADD COLUMN extract_status INTEGER NOT NULL DEFAULT 0")
        if "extract_batch" not in existing:
            conn.execute("ALTER TABLE raw_events ADD COLUMN extract_batch TEXT NOT NULL DEFAULT ''")
        if "extract_retries" not in existing:
            conn.execute("ALTER TABLE raw_events ADD COLUMN extract_retries INTEGER NOT NULL DEFAULT 0")
        if "last_attempt_at" not in existing:
            conn.execute("ALTER TABLE raw_events ADD COLUMN last_attempt_at TEXT NOT NULL DEFAULT ''")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_raw_time ON raw_events(created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_raw_ai ON raw_events(ai_id, created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_raw_window ON raw_events(chat_id, thread_id, created_at DESC)")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_raw_dedup ON raw_events(ai_id, chat_id, message_id) WHERE message_id != ''")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_raw_extract ON raw_events(extract_status, created_at DESC)")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS raw_vec_id_map (
                vec_rowid  INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id   INTEGER NOT NULL UNIQUE
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS _migrations (
                name TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS extract_batches (
                batch_id TEXT PRIMARY KEY,
                row_ids TEXT NOT NULL DEFAULT '[]',
                llm_result TEXT,
                items_written INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'processing'
            )
        """)

        # Migration: mark old rows as legacy (-1) so recovery only processes recent ones
        applied = {r[0] for r in conn.execute("SELECT name FROM _migrations").fetchall()}
        if "extract_status_backfill" not in applied:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
            conn.execute("UPDATE raw_events SET extract_status = -1 WHERE extract_status = 0 AND created_at < ?", (cutoff,))
            conn.execute("INSERT OR IGNORE INTO _migrations (name, applied_at) VALUES ('extract_status_backfill', ?)",
                         (datetime.now(timezone.utc).isoformat(timespec="seconds"),))
        conn.execute("COMMIT")

        try:
            import sqlite_vec
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            conn.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS raw_events_vec "
                f"USING vec0(embedding float[{EMBEDDING_DIM}])"
            )
        except Exception:
            pass
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


_init_db()


def _embed_text(user_text: str, ai_text: str) -> str:
    """拼接用于 embedding 的文本（各截 500 字）。"""
    u = (user_text or "")[:_EMBED_TEXT_LIMIT].strip()
    a = (ai_text or "")[:_EMBED_TEXT_LIMIT].strip()
    return f"{u} {a}".strip()


def _l2_normalize(vec: list[float]) -> list[float] | None:
    """L2 归一化。零向量返回 None（调用方应拒绝）。"""
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0:
        return None
    return [x / norm for x in vec]


def _store_embedding(event_id: int, embedding: list[float]) -> bool:
    """把 embedding L2 归一化后写入 raw_events + vec 索引。单事务，失败回滚。"""
    normalized = _l2_normalize(embedding)
    if normalized is None:
        return False
    blob = struct.pack(f"{len(normalized)}f", *normalized)
    conn = None
    try:
        conn = _connect(load_vec=True)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE raw_events SET embedding = ? WHERE id = ?", (blob, event_id))
        conn.execute(
            "INSERT OR IGNORE INTO raw_vec_id_map (event_id) VALUES (?)", (event_id,)
        )
        vec_rowid = conn.execute(
            "SELECT vec_rowid FROM raw_vec_id_map WHERE event_id = ?", (event_id,)
        ).fetchone()[0]
        try:
            conn.execute(
                "INSERT INTO raw_events_vec (rowid, embedding) VALUES (?, ?)",
                (vec_rowid, blob),
            )
        except sqlite3.Error:
            conn.execute(
                "UPDATE raw_events_vec SET embedding = ? WHERE rowid = ?",
                (blob, vec_rowid),
            )
        conn.execute("COMMIT")
        return True
    except Exception as e:
        if conn and conn.in_transaction:
            conn.execute("ROLLBACK")
        log.warning(f"raw_vault store_embedding failed: {e}")
        return False
    finally:
        if conn:
            conn.close()


async def _async_embed_and_store(event_id: int, text: str):
    """后台异步：算 embedding 并存储。受 Semaphore 限制最多 4 个并发。"""
    async with _get_embed_semaphore():
        try:
            from embedding import get_embedding
            vec = await get_embedding(text)
            if vec and len(vec) == EMBEDDING_DIM:
                _store_embedding(event_id, vec)
        except Exception as e:
            log.debug(f"raw_vault async embed failed for event {event_id}: {e}")


_bg_tasks: set[asyncio.Task] = set()
_embed_semaphores: dict[int, asyncio.Semaphore] = {}


def _get_embed_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    key = id(loop)
    sem = _embed_semaphores.get(key)
    if sem is None:
        sem = asyncio.Semaphore(4)
        _embed_semaphores[key] = sem
    return sem


def clear_embed_semaphores() -> None:
    _embed_semaphores.clear()


def log_turn(user_message: str, ai_response: str, ai_id: str = "",
             platform: str = "", chat_id: str = "", chat_type: str = "",
             turn_id: str = "", *,
             thread_id: str = "", message_id: str = "",
             sender_id: str = "", sender_type: str = "",
             reply_to_id: str = "") -> int | None:
    """记录一轮原始对话。任何失败都不往外抛——保险箱故障不能影响聊天。

    去重：当 ai_id + chat_id + message_id 三元组重复时跳过插入（message_id 非空时）。
    返回 event_id（成功）或 None（失败/去重跳过）。
    """
    if not (user_message or "").strip() and not (ai_response or "").strip():
        return None
    try:
        conn = _connect()
        try:
            cur = conn.execute(
                "INSERT INTO raw_events (ai_id, platform, chat_id, chat_type, "
                "user_text, ai_text, created_at, turn_id, "
                "thread_id, message_id, sender_id, sender_type, reply_to_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ai_id, platform, str(chat_id), chat_type,
                 (user_message or "")[:4000], (ai_response or "")[:4000],
                 datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 turn_id or "",
                 str(thread_id or ""), str(message_id or ""),
                 str(sender_id or ""), str(sender_type or ""),
                 str(reply_to_id or "")),
            )
        except sqlite3.IntegrityError:
            conn.close()
            return None
        event_id = cur.lastrowid
        conn.commit()
        conn.close()

        embed_text = _embed_text(user_message, ai_response)
        if embed_text:
            try:
                loop = asyncio.get_running_loop()
                task = loop.create_task(_async_embed_and_store(event_id, embed_text))
                _bg_tasks.add(task)
                task.add_done_callback(_bg_tasks.discard)
            except RuntimeError:
                pass
        return event_id
    except Exception as e:
        log.warning(f"raw_vault log failed: {e}")
        return None


_LIKE_ESCAPE_TABLE = str.maketrans({"%": "\\%", "_": "\\_", "\\": "\\\\"})
_PUBLIC_CHAT_TYPES = ("public_group", "group", "supergroup")
_HOUSEHOLD_CHAT_TYPES = ("private_group",)
_ALL_GROUP_TYPES = _PUBLIC_CHAT_TYPES + _HOUSEHOLD_CHAT_TYPES
_VALID_SPEAKER_FILTERS = {"", "user", "ai"}
_LIMIT_MAX = 50
_MAX_SEARCH_WORDS = 16

_SELECT_COLS = "id, ai_id, platform, chat_id, chat_type, user_text, ai_text, created_at"


def _resolve_ai_ids(ai_id: str) -> list[str]:
    """别名归一化：cloudy/claude 等视为同一 AI。"""
    if not ai_id:
        return []
    try:
        from config import AI_ALIASES, AI_ALIAS_GROUPS
        canonical = AI_ALIASES.get(ai_id, ai_id)
        return AI_ALIAS_GROUPS.get(canonical, [ai_id])
    except Exception:
        return [ai_id]


def _split_words(query: str) -> list[str]:
    parts = query.strip().split()
    seen: set[str] = set()
    result: list[str] = []
    for p in parts:
        if p and p not in seen:
            seen.add(p)
            result.append(p)
    return result[:_MAX_SEARCH_WORDS]


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
    - 私聊 (chat_type='private')：按 ai_id（含别名）过滤
    - 群聊 (private_group/public_group/group/supergroup)：任何人可搜
    - ai_id 为空时：搜所有群聊（不含私聊）
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
    try:
        where_params: list = []
        word_clauses = []
        for kw_group in word_synonym_groups:
            syn_clauses = [_like_clauses_for_keyword(kw, speaker_filter, where_params)
                           for kw in kw_group]
            word_clauses.append("(" + " OR ".join(syn_clauses) + ")")

        any_word_where = "(" + " OR ".join(word_clauses) + ")"

        if ai_id:
            ai_ids = _resolve_ai_ids(ai_id)
            all_group = _ALL_GROUP_TYPES
            gp = ",".join("?" for _ in all_group)
            ai_ph = ",".join("?" for _ in ai_ids)
            isolation = (
                f" AND (LOWER(TRIM(COALESCE(chat_type,''))) IN ({gp})"
                f" OR (LOWER(TRIM(COALESCE(chat_type,''))) = 'private' AND ai_id IN ({ai_ph})))"
            )
            where_params.extend(all_group)
            where_params.extend(ai_ids)
        else:
            gp = ",".join("?" for _ in _ALL_GROUP_TYPES)
            isolation = f" AND LOWER(TRIM(COALESCE(chat_type,''))) IN ({gp}) "
            where_params.extend(_ALL_GROUP_TYPES)

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
                f"SELECT {_SELECT_COLS}, ({score_expr}) AS _match_score "
                f"FROM raw_events WHERE {any_word_where}{isolation} "
                "ORDER BY _match_score DESC, created_at DESC LIMIT ?"
            )
        else:
            all_params = where_params + [limit]
            sql = (
                f"SELECT {_SELECT_COLS} "
                f"FROM raw_events WHERE {any_word_where}{isolation} "
                "ORDER BY created_at DESC LIMIT ?"
            )

        cur = conn.execute(sql, tuple(all_params))
        rows = []
        for r in cur:
            d = dict(r)
            d.pop("_match_score", None)
            rows.append(d)
        return rows
    finally:
        conn.close()


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


_WINDOW_SELECT = (
    "id, ai_id, platform, chat_id, chat_type, user_text, ai_text, "
    "created_at, turn_id, thread_id, message_id, sender_id, sender_type, reply_to_id"
)


def get_window_context(
    ai_id: str,
    chat_id: str,
    thread_id: str = "",
    max_turns: int = 10,
    max_chars: int = 6000,
) -> dict:
    """按窗口恢复最近原始对话，用于 bot 重启后续聊。

    隔离策略：
    - 必须提供 chat_id（不允许退化为全窗口读取）
    - 私聊额外按 ai_id 过滤
    - thread_id 非空时精确匹配，空值只返回无话题的消息

    返回 turns 按时间正序，每条包含完整消息内容。
    总字符预算 max_chars：按完整轮次装入，装不下则停止。
    单条超预算时截断并标记 truncated=true。
    """
    if not chat_id:
        return {"error": "chat_id_required", "turns": [], "truncated": False}

    max_turns = max(1, min(max_turns, 30))
    max_chars = max(500, min(max_chars, 20000))

    conn = _connect()
    conn.row_factory = sqlite3.Row
    try:
        where = "chat_id = ? AND thread_id = ?"
        params: list = [str(chat_id), str(thread_id or "")]

        chat_type_row = conn.execute(
            "SELECT chat_type FROM raw_events WHERE chat_id = ? ORDER BY id DESC LIMIT 1",
            (str(chat_id),),
        ).fetchone()
        is_private = chat_type_row and (chat_type_row["chat_type"] or "") == "private"

        if is_private:
            if not ai_id:
                return {"error": "ai_id_required_for_private", "turns": [], "truncated": False}
            ai_ids = _resolve_ai_ids(ai_id)
            ph = ",".join("?" for _ in ai_ids)
            where += f" AND ai_id IN ({ph})"
            params.extend(ai_ids)

        cur = conn.execute(
            f"SELECT {_WINDOW_SELECT} FROM raw_events "
            f"WHERE {where} ORDER BY created_at DESC, id DESC LIMIT ?",
            (*params, max_turns),
        )
        rows_desc = [dict(r) for r in cur]
    finally:
        conn.close()

    # rows_desc is newest-first from the query; iterate newest→oldest to
    # keep the most recent turns within the char budget, then reverse to
    # chronological order for the caller.
    selected = []
    used_chars = 0
    result_truncated = False
    for row in rows_desc:
        row.pop("embedding", None)
        user_len = len(row.get("user_text") or "")
        ai_len = len(row.get("ai_text") or "")
        turn_chars = user_len + ai_len

        if used_chars + turn_chars > max_chars:
            if not selected:
                row["user_text"] = (row.get("user_text") or "")[:max_chars // 2]
                row["ai_text"] = (row.get("ai_text") or "")[:max_chars // 2]
                row["truncated"] = True
                selected.append(row)
                result_truncated = True
            else:
                result_truncated = True
            break
        selected.append(row)
        used_chars += turn_chars

    selected.reverse()
    turns = selected

    return {
        "turns": turns,
        "count": len(turns),
        "total_available": len(rows_desc),
        "truncated": result_truncated,
        "chat_id": str(chat_id),
        "thread_id": str(thread_id or ""),
    }


def stats(public_only: bool = False, ai_id: str = "") -> dict:
    """统计原文条数。隔离口径与 search() 一致：

    - public_only=True：只统计群聊（优先于 ai_id，不含私聊）
    - ai_id 非空且 public_only=False：群聊 + 该 AI（含别名）的私聊
    - 两者都为空：统计全部（doctor_report 用）
    """
    conn = _connect()
    try:
        if public_only:
            gp = ",".join("?" for _ in _ALL_GROUP_TYPES)
            cur = conn.execute(
                "SELECT COUNT(*), MIN(created_at), MAX(created_at) FROM raw_events "
                f"WHERE LOWER(TRIM(COALESCE(chat_type, ''))) IN ({gp})",
                _ALL_GROUP_TYPES,
            )
        elif ai_id:
            ai_ids = _resolve_ai_ids(ai_id)
            all_group = _ALL_GROUP_TYPES
            gp = ",".join("?" for _ in all_group)
            ai_ph = ",".join("?" for _ in ai_ids)
            cur = conn.execute(
                "SELECT COUNT(*), MIN(created_at), MAX(created_at) FROM raw_events "
                f"WHERE (LOWER(TRIM(COALESCE(chat_type,''))) IN ({gp})"
                f" OR (LOWER(TRIM(COALESCE(chat_type,''))) = 'private' AND ai_id IN ({ai_ph})))",
                (*all_group, *ai_ids),
            )
        else:
            cur = conn.execute("SELECT COUNT(*), MIN(created_at), MAX(created_at) FROM raw_events")
        count, oldest, newest = cur.fetchone()
        return {"count": count or 0, "oldest": oldest or "", "newest": newest or ""}
    finally:
        conn.close()


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


# ── 语义搜索 ──

def _cosine_sim(a: bytes, b: bytes) -> float:
    """从 packed bytes 算余弦相似度。"""
    n = len(a) // 4
    va = struct.unpack(f"{n}f", a)
    vb = struct.unpack(f"{n}f", b)
    dot = sum(x * y for x, y in zip(va, vb))
    na = math.sqrt(sum(x * x for x in va))
    nb = math.sqrt(sum(x * x for x in vb))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


_SQLITE_PARAM_LIMIT = 900


def semantic_search(
    query_vec: list[float],
    ai_id: str = "",
    days: int = 7,
    limit: int = 8,
) -> list[dict]:
    """在最近 N 天的 raw_events 里做向量语义搜索。

    返回按 cosine_sim × recency_weight 排序的结果。
    隔离策略与 search() / stats() 一致（allowlist _ALL_GROUP_TYPES）。
    查询向量自动 L2 归一化。候选池按 vec 表实际行数动态扩展。
    """
    if not query_vec:
        return []

    query_vec = _l2_normalize(query_vec)
    if query_vec is None:
        return []
    query_blob = struct.pack(f"{EMBEDDING_DIM}f", *query_vec)

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    now_ts = datetime.now(timezone.utc)
    decay_hours = days * 24.0

    conn = _connect(load_vec=True)
    conn.row_factory = sqlite3.Row
    try:
        total_count = conn.execute(
            "SELECT COUNT(*) FROM raw_events_vec"
        ).fetchone()[0]
        if total_count == 0:
            return []

        ai_ids = _resolve_ai_ids(ai_id) if ai_id else []
        if ai_ids:
            gp = ",".join("?" for _ in _ALL_GROUP_TYPES)
            ai_ph = ",".join("?" for _ in ai_ids)
            vis_where = (
                f"(LOWER(TRIM(COALESCE(e.chat_type,''))) IN ({gp})"
                f" OR (LOWER(TRIM(COALESCE(e.chat_type,''))) = 'private'"
                f" AND e.ai_id IN ({ai_ph})))"
            )
            vis_params = list(_ALL_GROUP_TYPES) + list(ai_ids)
        else:
            gp = ",".join("?" for _ in _ALL_GROUP_TYPES)
            vis_where = f"LOWER(TRIM(COALESCE(e.chat_type,''))) IN ({gp})"
            vis_params = list(_ALL_GROUP_TYPES)

        fetch_k = min(limit * 4, total_count)
        results: list[dict] = []

        while True:
            vec_rows = conn.execute(
                "SELECT rowid, distance FROM raw_events_vec "
                "WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
                (query_blob, fetch_k),
            ).fetchall()

            if not vec_rows:
                break

            dist_map = {vr["rowid"]: vr["distance"] for vr in vec_rows}
            vec_rowids = list(dist_map.keys())

            results = []
            for chunk_start in range(0, len(vec_rowids), _SQLITE_PARAM_LIMIT):
                chunk = vec_rowids[chunk_start:chunk_start + _SQLITE_PARAM_LIMIT]
                ph = ",".join("?" for _ in chunk)
                sql = (
                    f"SELECT e.id, e.ai_id, e.platform, e.chat_id, e.chat_type, "
                    f"e.user_text, e.ai_text, e.created_at, m.vec_rowid "
                    f"FROM raw_vec_id_map m "
                    f"JOIN raw_events e ON e.id = m.event_id "
                    f"WHERE m.vec_rowid IN ({ph}) "
                    f"AND e.created_at >= ? AND {vis_where}"
                )
                params = list(chunk) + [cutoff] + vis_params
                for row in conn.execute(sql, params):
                    ev = dict(row)
                    vec_rowid = ev.pop("vec_rowid")
                    distance = dist_map[vec_rowid]

                    cos_sim = max(0.0, min(1.0, 1.0 - distance * distance / 2.0))

                    try:
                        evt = datetime.fromisoformat(ev["created_at"])
                        if evt.tzinfo is None:
                            evt = evt.replace(tzinfo=timezone.utc)
                        hours_ago = max(0, (now_ts - evt).total_seconds() / 3600)
                    except Exception:
                        hours_ago = decay_hours

                    recency = math.exp(-hours_ago / decay_hours)

                    ev["_score"] = round(cos_sim * 0.7 + recency * 0.3, 4)
                    ev["_cosine"] = round(cos_sim, 4)
                    ev["_recency"] = round(recency, 4)
                    results.append(ev)

            if len(results) >= limit or fetch_k >= total_count:
                break
            fetch_k = min(fetch_k * 2, total_count)

        results.sort(key=lambda x: x["_score"], reverse=True)
        return results[:limit]
    except Exception as e:
        log.warning(f"raw_vault semantic_search failed: {e}")
        return []
    finally:
        conn.close()


async def backfill_raw_embeddings(batch: int = 100) -> dict:
    """给缺失 embedding 的 raw_events 补算向量。daemon 调用。"""
    from embedding import get_embedding

    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, user_text, ai_text FROM raw_events "
            "WHERE embedding IS NULL ORDER BY id DESC LIMIT ?",
            (batch,),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return {"backfilled": 0, "failed": 0, "total_missing": 0}

    done = 0
    failed = 0
    for row_id, user_text, ai_text in rows:
        text = _embed_text(user_text, ai_text)
        if not text:
            continue
        try:
            vec = await get_embedding(text)
            if vec and len(vec) == EMBEDDING_DIM:
                if _store_embedding(row_id, vec):
                    done += 1
                else:
                    failed += 1
            else:
                failed += 1
        except Exception:
            failed += 1

    conn2 = _connect()
    try:
        missing = conn2.execute(
            "SELECT COUNT(*) FROM raw_events WHERE embedding IS NULL"
        ).fetchone()[0]
    finally:
        conn2.close()

    if done:
        log.info(f"raw_vault backfilled {done} embeddings ({failed} failed), {missing} still missing")
    return {"backfilled": done, "failed": failed, "total_missing": missing}


_MIGRATION_NAME = "l2_normalize_embeddings_v1"


def _migration_applied(name: str) -> bool:
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM _migrations WHERE name = ?", (name,)
        ).fetchone()
        return row is not None
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()


def _mark_migration(name: str):
    conn = _connect()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO _migrations (name, applied_at) VALUES (?, ?)",
            (name, datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )
        conn.commit()
    finally:
        conn.close()


def _renormalize_one_atomic(event_id: int) -> str:
    """在同一事务内确保 raw→map→vec 三者一致且已归一化。

    返回 "updated" / "skipped" / "failed"。
    即使 raw embedding 已归一化，也必须检查并修复缺失的 map/vec 行。
    只有三者都一致时才返回 "skipped"。
    """
    conn = None
    try:
        conn = _connect(load_vec=True)
        conn.execute("BEGIN IMMEDIATE")

        row = conn.execute(
            "SELECT embedding FROM raw_events WHERE id = ?", (event_id,)
        ).fetchone()
        if not row or not row[0]:
            conn.execute("ROLLBACK")
            return "skipped"

        blob = row[0]
        if len(blob) % 4 != 0:
            conn.execute("ROLLBACK")
            return "failed"

        n = len(blob) // 4
        vec = list(struct.unpack(f"{n}f", blob))
        norm = math.sqrt(sum(x * x for x in vec))

        if norm == 0:
            conn.execute("ROLLBACK")
            return "failed"

        already_normalized = abs(norm - 1.0) < 1e-6

        if already_normalized:
            final_blob = blob
        else:
            normalized = [x / norm for x in vec]
            final_blob = struct.pack(f"{n}f", *normalized)
            conn.execute(
                "UPDATE raw_events SET embedding = ? WHERE id = ?",
                (final_blob, event_id),
            )

        conn.execute(
            "INSERT OR IGNORE INTO raw_vec_id_map (event_id) VALUES (?)",
            (event_id,),
        )
        vec_rowid = conn.execute(
            "SELECT vec_rowid FROM raw_vec_id_map WHERE event_id = ?",
            (event_id,),
        ).fetchone()[0]

        vec_row = conn.execute(
            "SELECT rowid FROM raw_events_vec WHERE rowid = ?", (vec_rowid,)
        ).fetchone()
        if vec_row:
            conn.execute(
                "UPDATE raw_events_vec SET embedding = ? WHERE rowid = ?",
                (final_blob, vec_rowid),
            )
        else:
            conn.execute(
                "INSERT INTO raw_events_vec (rowid, embedding) VALUES (?, ?)",
                (vec_rowid, final_blob),
            )

        needs_write = not already_normalized or not vec_row
        conn.execute("COMMIT")
        return "updated" if needs_write else "skipped"
    except Exception as e:
        if conn and conn.in_transaction:
            conn.execute("ROLLBACK")
        log.warning(f"renormalize_one_atomic failed for event {event_id}: {e}")
        return "failed"
    finally:
        if conn:
            conn.close()


def renormalize_all_embeddings(batch: int = 500) -> dict:
    """一次性重新归一化所有已存 embedding。幂等，可中断后重跑。

    每行在同一事务内读取当前 blob → 归一化 → 写回（原子操作，
    不会覆盖并发产生的新 embedding）。
    failed > 0 时不写完成 marker，下次调用会重试。
    """
    if _migration_applied(_MIGRATION_NAME):
        return {"status": "already_applied", "renormalized": 0, "skipped": 0, "failed": 0}

    conn = _connect()
    try:
        total = conn.execute(
            "SELECT COUNT(*) FROM raw_events WHERE embedding IS NOT NULL"
        ).fetchone()[0]
        if total == 0:
            _mark_migration(_MIGRATION_NAME)
            return {"status": "done", "renormalized": 0, "skipped": 0, "failed": 0, "total": 0}
    finally:
        conn.close()

    updated = 0
    skipped = 0
    failed = 0
    offset = 0
    while offset < total:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT id FROM raw_events "
                "WHERE embedding IS NOT NULL ORDER BY id LIMIT ? OFFSET ?",
                (batch, offset),
            ).fetchall()
        finally:
            conn.close()

        if not rows:
            break

        for (event_id,) in rows:
            try:
                result = _renormalize_one_atomic(event_id)
            except Exception as e:
                log.warning(f"renormalize event {event_id} crashed: {e}")
                result = "failed"
            if result == "updated":
                updated += 1
            elif result == "skipped":
                skipped += 1
            else:
                failed += 1

        offset += len(rows)

    if failed > 0:
        log.warning(
            f"renormalize_all_embeddings incomplete: {updated} updated, "
            f"{failed} failed, {skipped} skipped — marker NOT written"
        )
        return {
            "status": "incomplete", "renormalized": updated,
            "skipped": skipped, "failed": failed, "total": total,
        }

    _mark_migration(_MIGRATION_NAME)
    log.info(f"renormalize_all_embeddings: {updated} updated, {skipped} skipped, {total} total")
    return {
        "status": "done", "renormalized": updated,
        "skipped": skipped, "failed": 0, "total": total,
    }


# ════════════════════════════════════════════
#  Extraction status tracking
# ════════════════════════════════════════════
# extract_status: -1=legacy(skip), 0=pending, 1=processing, 2=done, 3=failed
EXTRACT_PENDING = 0
EXTRACT_PROCESSING = 1
EXTRACT_DONE = 2
EXTRACT_FAILED = 3
EXTRACT_LEGACY = -1


_MAX_EXTRACT_RETRIES = 3
_RETRY_BACKOFF_SECONDS = 1800  # 30 minutes


_CLAIM_STALE_SECONDS = 300  # 5 minutes: status=1 rows older than this are considered abandoned

def get_unprocessed_chunks(max_chunks: int = 3, chunk_size: int = 30) -> list[dict]:
    """Find unprocessed raw_events grouped by ai_id+chat_id+thread_id.

    Returns up to max_chunks groups, each with row IDs, chat metadata,
    and formatted conversation text. Includes:
    - status 0 (pending)
    - status 1 (processing) only if last_attempt_at is stale (>5 min, abandoned claim)
    - status 3 (failed) with retries < 3 and 30min backoff from last_attempt_at
    Private chats are isolated per ai_id.
    """
    now = datetime.now(timezone.utc)
    retry_cutoff = (now - timedelta(seconds=_RETRY_BACKOFF_SECONDS)).isoformat(timespec="seconds")
    claim_cutoff = (now - timedelta(seconds=_CLAIM_STALE_SECONDS)).isoformat(timespec="seconds")
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT id, ai_id, platform, chat_id, chat_type, user_text, ai_text, "
            "created_at, thread_id, sender_id, sender_type, extract_status, "
            "extract_batch "
            "FROM raw_events "
            "WHERE extract_status = 0 "
            "   OR (extract_status = 1 AND last_attempt_at < ?) "
            "   OR (extract_status = 3 AND extract_retries < ? AND last_attempt_at < ?) "
            "ORDER BY created_at ASC "
            "LIMIT ?",
            (claim_cutoff, _MAX_EXTRACT_RETRIES, retry_cutoff, max_chunks * chunk_size * 2),
        ).fetchall()
    finally:
        conn.close()

    groups: dict[str, list[dict]] = {}
    for row in rows:
        key = f"{row[1]}:{row[3]}:{row[8]}"  # ai_id:chat_id:thread_id
        if key not in groups:
            groups[key] = []
        groups[key].append({
            "id": row[0], "ai_id": row[1], "platform": row[2],
            "chat_id": row[3], "chat_type": row[4],
            "user_text": row[5], "ai_text": row[6],
            "created_at": row[7], "thread_id": row[8],
            "sender_id": row[9], "sender_type": row[10],
            "extract_status": row[11],
            "extract_batch": row[12],
        })

    chunks = []
    for key, events in list(groups.items())[:max_chunks]:
        events_to_process = events[:chunk_size]
        remaining = events[chunk_size:]
        ai_id = events_to_process[0]["ai_id"] or "claude"
        # Collect batch_ids from interrupted (status=1) rows for idempotency check
        interrupted_batches = list({
            e["extract_batch"] for e in events_to_process
            if e["extract_status"] == 1 and e["extract_batch"]
        })
        chunks.append({
            "key": key,
            "chat_id": events_to_process[0]["chat_id"],
            "thread_id": events_to_process[0]["thread_id"],
            "chat_type": events_to_process[0]["chat_type"],
            "ai_id": ai_id,
            "platform": events_to_process[0]["platform"],
            "row_ids": [e["id"] for e in events_to_process],
            "events": events_to_process,
            "remaining_count": len(remaining),
            "interrupted_batches": interrupted_batches,
        })
    return chunks


def mark_rows(
    row_ids: list[int], status: int, batch_id: str = "",
    expected_status: list[int] | None = None,
    expected_batch: str | None = None,
    stale_before: str | None = None,
) -> list[int]:
    """Set extract_status on specific rows. Returns list of row IDs actually updated.

    expected_status: if provided, only update rows currently in one of these states.
    expected_batch: if provided, only update rows whose extract_batch matches.
    stale_before: if provided, PROCESSING rows are only claimed when
        last_attempt_at < this ISO timestamp (i.e. they're stale/abandoned).
        PENDING and FAILED rows are claimed unconditionally.
    Together these make claims and done-marking atomic and owner-verified.
    Increments extract_retries when marking as failed (status=3).
    Updates last_attempt_at when marking as processing (1) or failed (3).
    """
    if not row_ids:
        return []
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn = _connect()
    try:
        placeholders = ",".join("?" for _ in row_ids)
        status_filter = ""
        filter_params: list = []
        if expected_status is not None:
            if stale_before is not None and EXTRACT_PROCESSING in expected_status:
                non_proc = [s for s in expected_status if s != EXTRACT_PROCESSING]
                if non_proc:
                    np_ph = ",".join("?" for _ in non_proc)
                    status_filter = (
                        f" AND (extract_status IN ({np_ph})"
                        f" OR (extract_status = {EXTRACT_PROCESSING} AND last_attempt_at < ?))"
                    )
                    filter_params = list(non_proc) + [stale_before]
                else:
                    status_filter = f" AND extract_status = {EXTRACT_PROCESSING} AND last_attempt_at < ?"
                    filter_params = [stale_before]
            else:
                s_placeholders = ",".join("?" for _ in expected_status)
                status_filter = f" AND extract_status IN ({s_placeholders})"
                filter_params = list(expected_status)
        if expected_batch is not None:
            status_filter += " AND extract_batch = ?"
            filter_params.append(expected_batch)

        where = f"id IN ({placeholders}){status_filter}"

        if status == EXTRACT_FAILED:
            conn.execute(
                f"UPDATE raw_events SET extract_status = ?, extract_batch = ?, "
                f"extract_retries = extract_retries + 1, last_attempt_at = ? "
                f"WHERE {where}",
                [status, batch_id or "", now] + row_ids + filter_params,
            )
        elif status == EXTRACT_PROCESSING:
            conn.execute(
                f"UPDATE raw_events SET extract_status = ?, extract_batch = ?, "
                f"last_attempt_at = ? "
                f"WHERE {where}",
                [status, batch_id or "", now] + row_ids + filter_params,
            )
        elif batch_id:
            conn.execute(
                f"UPDATE raw_events SET extract_status = ?, extract_batch = ? "
                f"WHERE {where}",
                [status, batch_id] + row_ids + filter_params,
            )
        else:
            conn.execute(
                f"UPDATE raw_events SET extract_status = ? "
                f"WHERE {where}",
                [status] + row_ids + filter_params,
            )
        if batch_id:
            updated = conn.execute(
                f"SELECT id FROM raw_events WHERE id IN ({placeholders}) "
                f"AND extract_status = ? AND extract_batch = ?",
                row_ids + [status, batch_id],
            ).fetchall()
        else:
            updated = conn.execute(
                f"SELECT id FROM raw_events WHERE id IN ({placeholders}) "
                f"AND extract_status = ?",
                row_ids + [status],
            ).fetchall()
        conn.commit()
        return [r[0] for r in updated]
    finally:
        conn.close()


def save_batch(batch_id: str, row_ids: list[int], llm_result: str | None = None):
    """Create or update an extract_batch record to persist LLM result.
    Does NOT overwrite existing llm_result with None (preserves saved state on re-claim).
    """
    import json as _json
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn = _connect()
    try:
        if llm_result is not None:
            conn.execute(
                "INSERT INTO extract_batches (batch_id, row_ids, llm_result, created_at, status) "
                "VALUES (?, ?, ?, ?, 'processing') "
                "ON CONFLICT(batch_id) DO UPDATE SET llm_result = excluded.llm_result, "
                "row_ids = excluded.row_ids",
                (batch_id, _json.dumps(row_ids), llm_result, now),
            )
        else:
            conn.execute(
                "INSERT OR IGNORE INTO extract_batches (batch_id, row_ids, created_at, status) "
                "VALUES (?, ?, ?, 'processing')",
                (batch_id, _json.dumps(row_ids), now),
            )
        conn.commit()
    finally:
        conn.close()


def get_batch(batch_id: str) -> dict | None:
    """Load a saved batch record. Returns None if not found."""
    import json as _json
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT batch_id, row_ids, llm_result, items_written, status "
            "FROM extract_batches WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {
        "batch_id": row[0],
        "row_ids": _json.loads(row[1]),
        "llm_result": row[2],
        "items_written": row[3],
        "status": row[4],
    }


def update_batch_progress(batch_id: str, items_written: int, status: str = "processing"):
    """Update items_written and status on a batch record."""
    conn = _connect()
    try:
        conn.execute(
            "UPDATE extract_batches SET items_written = ?, status = ? WHERE batch_id = ?",
            (items_written, status, batch_id),
        )
        conn.commit()
    finally:
        conn.close()


def proposal_exists_by_source(source_platform: str) -> bool:
    """Check if a proposal with this exact source_platform already exists."""
    try:
        import database
        conn = database._get_read_conn()
        row = conn.execute(
            "SELECT 1 FROM proposals WHERE source_platform = ? LIMIT 1",
            (source_platform,),
        ).fetchone()
        return row is not None
    except Exception:
        return False


def count_by_status() -> dict[str, int]:
    """Return counts of rows per extract_status for diagnostics."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT extract_status, COUNT(*) FROM raw_events GROUP BY extract_status"
        ).fetchall()
    finally:
        conn.close()
    labels = {-1: "legacy", 0: "pending", 1: "processing", 2: "done", 3: "failed"}
    return {labels.get(s, f"unknown_{s}"): c for s, c in rows}
