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
        conn.execute("CREATE INDEX IF NOT EXISTS idx_raw_time ON raw_events(created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_raw_ai ON raw_events(ai_id, created_at DESC)")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS raw_vec_id_map (
                vec_rowid  INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id   INTEGER NOT NULL UNIQUE
            )
        """)
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


def _store_embedding(event_id: int, embedding: list[float]) -> bool:
    """把 embedding 写入 raw_events + vec 索引。三步在同一事务内完成。"""
    blob = struct.pack(f"{len(embedding)}f", *embedding)
    conn = _connect(load_vec=True)
    try:
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
        conn.commit()
        return True
    except Exception as e:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        log.warning(f"raw_vault store_embedding failed: {e}")
        return False
    finally:
        conn.close()


async def _async_embed_and_store(event_id: int, text: str):
    """后台异步：算 embedding 并存储。"""
    try:
        from embedding import get_embedding
        vec = await get_embedding(text)
        if vec and len(vec) == EMBEDDING_DIM:
            _store_embedding(event_id, vec)
    except Exception as e:
        log.debug(f"raw_vault async embed failed for event {event_id}: {e}")


_bg_tasks: set[asyncio.Task] = set()


def log_turn(user_message: str, ai_response: str, ai_id: str = "",
             platform: str = "", chat_id: str = "", chat_type: str = "",
             turn_id: str = ""):
    """记录一轮原始对话。任何失败都不往外抛——保险箱故障不能影响聊天。"""
    if not (user_message or "").strip() and not (ai_response or "").strip():
        return
    try:
        conn = _connect()
        cur = conn.execute(
            "INSERT INTO raw_events (ai_id, platform, chat_id, chat_type, "
            "user_text, ai_text, created_at, turn_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ai_id, platform, str(chat_id), chat_type,
             (user_message or "")[:4000], (ai_response or "")[:4000],
             datetime.now(timezone.utc).isoformat(timespec="seconds"),
             turn_id or ""),
        )
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
    except Exception as e:
        log.warning(f"raw_vault log failed: {e}")


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


def semantic_search(
    query_vec: list[float],
    ai_id: str = "",
    days: int = 7,
    limit: int = 8,
) -> list[dict]:
    """在最近 N 天的 raw_events 里做向量语义搜索。

    返回按 真实cosine × 0.7 + recency × 0.3 排序的结果。
    隔离策略与 search() 一致。
    逐步扩容 KNN 候选池直到凑够 limit 条合格结果或耗尽。
    """
    query_blob = struct.pack(f"{EMBEDDING_DIM}f", *query_vec)

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    now_ts = datetime.now(timezone.utc)
    decay_hours = days * 24.0

    conn = _connect(load_vec=True)
    conn.row_factory = sqlite3.Row

    ai_ids = _resolve_ai_ids(ai_id) if ai_id else []

    results = []
    fetch_k = limit * 6
    max_fetch = 500

    try:
        while len(results) < limit and fetch_k <= max_fetch:
            try:
                vec_rows = conn.execute(
                    "SELECT rowid, distance FROM raw_events_vec "
                    "WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
                    (query_blob, fetch_k),
                ).fetchall()
            except Exception as e:
                log.warning(f"raw_vault semantic_search vec query failed: {e}")
                return []

            if not vec_rows:
                break

            vec_rowids = [vr["rowid"] for vr in vec_rows]
            distances = {vr["rowid"]: vr["distance"] for vr in vec_rows}

            ph = ",".join("?" * len(vec_rowids))
            map_rows = conn.execute(
                f"SELECT vec_rowid, event_id FROM raw_vec_id_map WHERE vec_rowid IN ({ph})",
                vec_rowids,
            ).fetchall()
            event_map = {mr["vec_rowid"]: mr["event_id"] for mr in map_rows}

            event_ids = list(event_map.values())
            if not event_ids:
                break
            eph = ",".join("?" * len(event_ids))
            events = conn.execute(
                f"SELECT {_SELECT_COLS}, embedding FROM raw_events "
                f"WHERE id IN ({eph}) AND created_at >= ?",
                (*event_ids, cutoff),
            ).fetchall()
            event_dict = {e["id"]: dict(e) for e in events}

            seen_ids = {r["id"] for r in results}
            for vec_rowid in vec_rowids:
                eid = event_map.get(vec_rowid)
                if eid is None or eid in seen_ids:
                    continue
                ev = event_dict.get(eid)
                if ev is None:
                    continue

                ct = (ev.get("chat_type") or "").strip().lower()
                if ai_ids:
                    if ct == "private" and ev.get("ai_id") not in ai_ids:
                        continue
                elif not ai_id:
                    if ct == "private":
                        continue

                raw_emb = ev.pop("embedding", None)
                if raw_emb and len(raw_emb) == EMBEDDING_DIM * 4:
                    cos_sim = _cosine_sim(query_blob, raw_emb)
                else:
                    cos_sim = max(0.0, 1.0 - distances[vec_rowid] / 2.0)

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
                seen_ids.add(eid)

            if len(results) >= limit or len(vec_rows) < fetch_k:
                break
            fetch_k *= 2
    finally:
        conn.close()

    results.sort(key=lambda x: x["_score"], reverse=True)
    return results[:limit]


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
        vec = await get_embedding(text)
        if vec and len(vec) == EMBEDDING_DIM:
            if _store_embedding(row_id, vec):
                done += 1
            else:
                failed += 1

    conn2 = _connect()
    try:
        missing = conn2.execute("SELECT COUNT(*) FROM raw_events WHERE embedding IS NULL").fetchone()[0]
    finally:
        conn2.close()

    if done:
        log.info(f"raw_vault backfilled {done} embeddings ({failed} failed), {missing} still missing")
    return {"backfilled": done, "failed": failed, "total_missing": missing}
