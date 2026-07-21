"""
SQLite 数据库引擎（替代内存 dict + GitHub 存储）
- sqlite-vec 向量搜索
- FTS5 全文搜索
- WAL 模式并发读
- 同步 sqlite3，hot-path 读操作通过 to_thread 离开事件循环
"""
import json
import re
import struct
import sqlite3
import asyncio
import logging
import threading
from pathlib import Path
from typing import Iterator, Callable, TypeVar

from config import DATA_DIR, EMBEDDING_DIM

logger = logging.getLogger("memory_hub.db")

T = TypeVar("T")

# ── 默认数据库路径 ──
DB_PATH: Path = DATA_DIR / "memories.db"

# ── 模块级主连接（写入用） ──
_conn: sqlite3.Connection | None = None

# ── 线程局部只读连接池（to_thread 里的读操作用） ──
_local = threading.local()


def _get_read_conn() -> sqlite3.Connection:
    """每个线程独立的只读连接，WAL 模式下不会被写锁阻塞。"""
    conn = getattr(_local, "read_conn", None)
    if conn is not None:
        return conn
    path = str(DB_PATH)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=200")  # 读连接只等 200ms，不要等 5s
    conn.execute("PRAGMA query_only=ON")
    try:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except Exception:
        pass
    _local.read_conn = conn
    return conn


async def read_in_thread(fn: Callable[..., T], *args, **kwargs) -> T:
    """在独立线程里执行同步 DB 读操作，不阻塞事件循环。"""
    import functools
    return await asyncio.to_thread(functools.partial(fn, *args, **kwargs))


# ════════════════════════════════════════════
#  Schema
# ════════════════════════════════════════════

_SCHEMA_MAIN = """
CREATE TABLE IF NOT EXISTS memories (
    id              TEXT PRIMARY KEY,
    content         TEXT NOT NULL DEFAULT '',
    layer           TEXT NOT NULL DEFAULT 'shared',
    room            TEXT NOT NULL DEFAULT 'living_room',
    category        TEXT NOT NULL DEFAULT '',
    owner_ai        TEXT NOT NULL DEFAULT '',
    importance      REAL NOT NULL DEFAULT 0.5,
    emotion_arousal REAL NOT NULL DEFAULT 0.3,
    valence         REAL NOT NULL DEFAULT 0.5,
    domain          TEXT NOT NULL DEFAULT '[]',
    decay_score     REAL NOT NULL DEFAULT 1.0,
    activation_count REAL NOT NULL DEFAULT 0,
    last_activated  TEXT NOT NULL DEFAULT '',
    source_ai       TEXT NOT NULL DEFAULT '',
    source_platform TEXT NOT NULL DEFAULT '',
    tags            TEXT NOT NULL DEFAULT '[]',
    linked_memories TEXT NOT NULL DEFAULT '[]',
    supersedes      TEXT NOT NULL DEFAULT '[]',
    superseded_by   TEXT NOT NULL DEFAULT '',
    event_date      TEXT NOT NULL DEFAULT '',
    source_context  TEXT NOT NULL DEFAULT '',
    comments        TEXT NOT NULL DEFAULT '[]',
    embedding       BLOB,
    status          TEXT NOT NULL DEFAULT 'active',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    history         TEXT NOT NULL DEFAULT '[]',
    resolved        INTEGER,
    anchored        INTEGER,
    provenance_type TEXT NOT NULL DEFAULT '',
    fact_confidence REAL
);

CREATE INDEX IF NOT EXISTS idx_mem_status     ON memories(status);
CREATE INDEX IF NOT EXISTS idx_mem_room       ON memories(room);
CREATE INDEX IF NOT EXISTS idx_mem_layer      ON memories(layer);
CREATE INDEX IF NOT EXISTS idx_mem_owner      ON memories(owner_ai);
CREATE INDEX IF NOT EXISTS idx_mem_source_ai  ON memories(source_ai);
CREATE INDEX IF NOT EXISTS idx_mem_category   ON memories(category);
CREATE INDEX IF NOT EXISTS idx_mem_importance ON memories(importance);
CREATE INDEX IF NOT EXISTS idx_mem_updated    ON memories(updated_at);
CREATE INDEX IF NOT EXISTS idx_mem_resolved   ON memories(resolved);
CREATE INDEX IF NOT EXISTS idx_mem_room_status ON memories(room, status);
"""

_SCHEMA_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content,
    category,
    tags,
    domain,
    content='memories',
    content_rowid='rowid'
);
"""

_SCHEMA_FTS_TRIGGERS = """
-- Insert trigger
CREATE TRIGGER IF NOT EXISTS trg_mem_fts_insert
AFTER INSERT ON memories
BEGIN
    INSERT INTO memories_fts(rowid, content, category, tags, domain)
    VALUES (NEW.rowid, NEW.content, NEW.category, NEW.tags, NEW.domain);
END;

-- Delete trigger
CREATE TRIGGER IF NOT EXISTS trg_mem_fts_delete
AFTER DELETE ON memories
BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, category, tags, domain)
    VALUES ('delete', OLD.rowid, OLD.content, OLD.category, OLD.tags, OLD.domain);
END;

-- Update trigger
CREATE TRIGGER IF NOT EXISTS trg_mem_fts_update
AFTER UPDATE ON memories
BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, category, tags, domain)
    VALUES ('delete', OLD.rowid, OLD.content, OLD.category, OLD.tags, OLD.domain);
    INSERT INTO memories_fts(rowid, content, category, tags, domain)
    VALUES (NEW.rowid, NEW.content, NEW.category, NEW.tags, NEW.domain);
END;
"""

_SCHEMA_VEC_ID_MAP = """
CREATE TABLE IF NOT EXISTS vec_id_map (
    vec_rowid   INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id   TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_vec_id_map_memid ON vec_id_map(memory_id);
"""


# ════════════════════════════════════════════
#  Helpers
# ════════════════════════════════════════════

def _get_conn() -> sqlite3.Connection:
    """Return the module-level connection, raising if not initialised."""
    if _conn is None:
        raise RuntimeError("Database not initialised. Call init_db() first.")
    return _conn


def _row_to_dict(row: sqlite3.Row) -> dict:
    """Convert a sqlite3.Row to a memory dict matching the legacy format.

    JSON-string columns that were stored as list/dict in the old dict format
    are kept as JSON strings (the rest of the codebase already handles both).
    ``comments`` and ``history`` are deserialised to lists so callers can
    append to them directly, matching the old in-memory behaviour.
    ``resolved`` is converted from INTEGER (NULL/0/1) back to None/False/True.
    ``embedding`` is kept as raw bytes (or None).
    """
    d = dict(row)

    # resolved: INTEGER -> Python bool | None
    r = d.get("resolved")
    if r is None:
        d["resolved"] = None
    elif r == 0:
        d["resolved"] = False
    else:
        d["resolved"] = True

    # anchored: INTEGER -> Python bool | None
    a = d.get("anchored")
    if a is None:
        d["anchored"] = None
    elif a == 0:
        d["anchored"] = False
    else:
        d["anchored"] = True

    # Deserialise list/dict JSON columns
    for key in ("comments", "history"):
        val = d.get(key)
        if isinstance(val, str):
            try:
                d[key] = json.loads(val)
            except (json.JSONDecodeError, TypeError):
                d[key] = []

    return d


def _row_to_dict_no_embedding(row: sqlite3.Row) -> dict:
    """Same as _row_to_dict but drops the embedding to save bandwidth."""
    d = _row_to_dict(row)
    d.pop("embedding", None)
    return d


def _resolved_to_int(val) -> int | None:
    """Convert Python resolved value to SQLite INTEGER."""
    if val is None:
        return None
    if val is True or val == 1:
        return 1
    if val is False or val == 0:
        return 0
    return None


# ════════════════════════════════════════════
#  Initialisation
# ════════════════════════════════════════════

async def init_db(db_path: str = None) -> None:
    """Initialise the SQLite database.

    Creates tables, loads the sqlite-vec extension, and sets pragmas.
    The ``async`` signature is for startup-flow compatibility only;
    all work is synchronous.
    """
    global _conn

    path = db_path or str(DB_PATH)
    logger.info(f"Initialising SQLite database at {path}")

    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row

    # Pragmas
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")

    # Load sqlite-vec extension
    try:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        logger.info("sqlite-vec extension loaded")
    except Exception as e:
        logger.error(f"Failed to load sqlite-vec extension: {e}")
        raise

    # Create main table + indexes
    conn.executescript(_SCHEMA_MAIN)

    # Create FTS5 virtual table + sync triggers
    conn.executescript(_SCHEMA_FTS)
    conn.executescript(_SCHEMA_FTS_TRIGGERS)

    # Create vec id mapping table
    conn.executescript(_SCHEMA_VEC_ID_MAP)

    # Create sqlite-vec virtual table
    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS memories_vec "
        f"USING vec0(embedding float[{EMBEDDING_DIM}])"
    )

    # ── Migrations for existing databases ──
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(memories)").fetchall()}
    if "anchored" not in existing_cols:
        conn.execute("ALTER TABLE memories ADD COLUMN anchored INTEGER")
        logger.info("Migrated: added 'anchored' column")
    if "provenance_type" not in existing_cols:
        conn.execute("ALTER TABLE memories ADD COLUMN provenance_type TEXT NOT NULL DEFAULT ''")
        logger.info("Migrated: added 'provenance_type' column")
    if "fact_confidence" not in existing_cols:
        conn.execute("ALTER TABLE memories ADD COLUMN fact_confidence REAL")
        logger.info("Migrated: added 'fact_confidence' column")

    conn.execute("CREATE INDEX IF NOT EXISTS idx_mem_anchored ON memories(anchored)")

    # ── Proposals table (MemoryProposal 候选区) ──
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS proposals (
            id                  TEXT PRIMARY KEY,
            content             TEXT NOT NULL,
            claim_type          TEXT NOT NULL DEFAULT 'observation',
            speech_mode         TEXT NOT NULL DEFAULT 'uncertain',
            conversation_kind   TEXT NOT NULL DEFAULT 'house_chat',
            proposed_room       TEXT NOT NULL DEFAULT 'living_room',
            source_message_ids  TEXT NOT NULL DEFAULT '[]',
            evidence_excerpt    TEXT NOT NULL DEFAULT '',
            proposer_ai_id      TEXT NOT NULL DEFAULT '',
            confidence          REAL NOT NULL DEFAULT 0.5,
            conflicts_with      TEXT NOT NULL DEFAULT '[]',
            status              TEXT NOT NULL DEFAULT 'pending',
            layer               TEXT NOT NULL DEFAULT 'shared',
            owner_ai            TEXT NOT NULL DEFAULT '',
            importance          REAL NOT NULL DEFAULT 0.5,
            emotion_arousal     REAL NOT NULL DEFAULT 0.3,
            category            TEXT NOT NULL DEFAULT '',
            tags                TEXT NOT NULL DEFAULT '[]',
            event_date          TEXT NOT NULL DEFAULT '',
            source_context      TEXT NOT NULL DEFAULT '',
            source_platform     TEXT NOT NULL DEFAULT '',
            provenance_type     TEXT NOT NULL DEFAULT '',
            created_at          TEXT NOT NULL,
            reviewed_at         TEXT NOT NULL DEFAULT '',
            reviewed_by         TEXT NOT NULL DEFAULT '',
            reject_reason       TEXT NOT NULL DEFAULT '',
            triage_reason       TEXT NOT NULL DEFAULT '',
            applied_memory_id   TEXT NOT NULL DEFAULT '',
            failure_reason      TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_prop_status ON proposals(status);
        CREATE INDEX IF NOT EXISTS idx_prop_created ON proposals(created_at);
    """)

    # ── Proposals table migrations ──
    existing = {row[1] for row in conn.execute("PRAGMA table_info(proposals)").fetchall()}
    for col, typedef in [
        ("triage_reason", "TEXT NOT NULL DEFAULT ''"),
        ("applied_memory_id", "TEXT NOT NULL DEFAULT ''"),
        ("failure_reason", "TEXT NOT NULL DEFAULT ''"),
    ]:
        if col not in existing:
            conn.execute(f"ALTER TABLE proposals ADD COLUMN {col} {typedef}")
            logger.info(f"Migrated proposals: added '{col}' column")

    # ── Persons table (人物名片) ──
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS persons (
            person_id       TEXT PRIMARY KEY,
            entity_type     TEXT NOT NULL DEFAULT 'other',
            canonical_name  TEXT NOT NULL,
            aliases         TEXT NOT NULL DEFAULT '[]',
            linked_agent_id TEXT NOT NULL DEFAULT '',
            note            TEXT NOT NULL DEFAULT '',
            created_at      TEXT NOT NULL DEFAULT '',
            updated_at      TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_person_type ON persons(entity_type);
        CREATE INDEX IF NOT EXISTS idx_person_agent ON persons(linked_agent_id);
    """)

    conn.commit()
    _conn = conn
    logger.info("Database initialised successfully")


# ════════════════════════════════════════════
#  CRUD
# ════════════════════════════════════════════

_ALL_COLUMNS = [
    "id", "content", "layer", "room", "category", "owner_ai",
    "importance", "emotion_arousal", "valence", "domain",
    "decay_score", "activation_count", "last_activated",
    "source_ai", "source_platform", "tags", "linked_memories",
    "supersedes", "superseded_by", "event_date", "source_context",
    "comments", "embedding", "status", "created_at", "updated_at",
    "history", "resolved", "anchored", "provenance_type", "fact_confidence",
]


def get_memory(mem_id: str) -> dict | None:
    """Get a single memory by ID. Returns full dict including embedding."""
    conn = _get_conn()
    row = conn.execute("SELECT * FROM memories WHERE id = ?", (mem_id,)).fetchone()
    if row is None:
        return None
    return _row_to_dict(row)


def set_memory(mem: dict) -> None:
    """Insert or replace a memory (upsert).

    Also maintains the vec_id_map and memories_vec tables for vector search.
    FTS is handled automatically by triggers.
    """
    conn = _get_conn()

    # Prepare values — serialise list/dict fields to JSON strings
    def _prep(key):
        val = mem.get(key)
        if key in ("resolved", "anchored"):
            return _resolved_to_int(val)
        if key in ("comments", "history"):
            if isinstance(val, (list, dict)):
                return json.dumps(val, ensure_ascii=False)
            if val is None:
                return "[]"
            return val
        if key == "embedding":
            return val  # bytes or None
        if key == "fact_confidence":
            return val  # REAL or None
        if val is None:
            return ""
        return val

    values = [_prep(col) for col in _ALL_COLUMNS]
    placeholders = ", ".join(["?"] * len(_ALL_COLUMNS))
    cols = ", ".join(_ALL_COLUMNS)
    # embedding 用 COALESCE：写入方（内存 store / GitHub 快照）经常没有向量，
    # 不能让 None 覆盖掉库里已有的 embedding——否则任何 activation 更新
    # 都会把离线补好的向量冲掉（2026-07-18：382 条向量被这样冲没过）
    update_set = ", ".join(
        f"{c} = COALESCE(excluded.{c}, memories.{c})" if c == "embedding"
        else f"{c} = excluded.{c}"
        for c in _ALL_COLUMNS if c != "id"
    )

    sql = (
        f"INSERT INTO memories ({cols}) VALUES ({placeholders}) "
        f"ON CONFLICT(id) DO UPDATE SET {update_set}"
    )

    try:
        conn.execute(sql, values)
    except sqlite3.Error:
        logger.exception(f"Failed to upsert memory {mem.get('id', '?')}")
        raise

    # ── Update vector index ──
    mem_id = mem["id"]
    embedding = mem.get("embedding")

    if embedding is not None and len(embedding) == EMBEDDING_DIM * 4:
        # Ensure a vec_id_map entry exists
        row = conn.execute(
            "SELECT vec_rowid FROM vec_id_map WHERE memory_id = ?", (mem_id,)
        ).fetchone()

        if row is not None:
            vec_rowid = row[0]
            # Update existing vec entry
            try:
                conn.execute(
                    "UPDATE memories_vec SET embedding = ? WHERE rowid = ?",
                    (embedding, vec_rowid),
                )
            except sqlite3.Error:
                # Row might not exist in vec table (e.g. after rebuild); insert instead
                try:
                    conn.execute(
                        "INSERT INTO memories_vec (rowid, embedding) VALUES (?, ?)",
                        (vec_rowid, embedding),
                    )
                except sqlite3.Error:
                    logger.warning(f"Failed to update/insert vec for {mem_id}")
        else:
            # New entry — insert into map, then into vec table
            cur = conn.execute(
                "INSERT INTO vec_id_map (memory_id) VALUES (?)", (mem_id,)
            )
            vec_rowid = cur.lastrowid
            try:
                conn.execute(
                    "INSERT INTO memories_vec (rowid, embedding) VALUES (?, ?)",
                    (vec_rowid, embedding),
                )
            except sqlite3.Error:
                logger.warning(f"Failed to insert vec for {mem_id}")
    else:
        # No valid embedding — remove from vec if it existed
        row = conn.execute(
            "SELECT vec_rowid FROM vec_id_map WHERE memory_id = ?", (mem_id,)
        ).fetchone()
        if row is not None:
            vec_rowid = row[0]
            try:
                conn.execute("DELETE FROM memories_vec WHERE rowid = ?", (vec_rowid,))
            except sqlite3.Error:
                pass
            conn.execute("DELETE FROM vec_id_map WHERE vec_rowid = ?", (vec_rowid,))

    conn.commit()


def remove_memory(mem_id: str) -> None:
    """Delete a memory and its FTS/vec entries.

    FTS cleanup is handled by the DELETE trigger. Vec cleanup is explicit.
    """
    conn = _get_conn()

    # Clean up vec index
    row = conn.execute(
        "SELECT vec_rowid FROM vec_id_map WHERE memory_id = ?", (mem_id,)
    ).fetchone()
    if row is not None:
        vec_rowid = row[0]
        try:
            conn.execute("DELETE FROM memories_vec WHERE rowid = ?", (vec_rowid,))
        except sqlite3.Error:
            pass
        conn.execute("DELETE FROM vec_id_map WHERE vec_rowid = ?", (vec_rowid,))

    # Delete from main table (triggers handle FTS)
    conn.execute("DELETE FROM memories WHERE id = ?", (mem_id,))
    conn.commit()


# ════════════════════════════════════════════
#  Query
# ════════════════════════════════════════════

# Allowed order_by columns (to prevent SQL injection)
_ALLOWED_ORDER_COLUMNS = {
    "updated_at", "created_at", "importance", "decay_score",
    "activation_count", "emotion_arousal", "valence",
}


def query_memories(
    room: str = None,
    status: str = None,
    owner_ai: str = None,
    layer: str = None,
    category: str = None,
    source_ai: str = None,
    min_importance: float = None,
    resolved: bool | None = "ANY",
    limit: int = None,
    offset: int = 0,
    order_by: str = "updated_at DESC",
    exclude_rooms: list[str] = None,
    include_rooms: list[str] = None,
) -> list[dict]:
    """Query memories with filters. Returns dicts without embedding."""
    conn = _get_conn()

    clauses: list[str] = []
    params: list = []

    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    if room is not None:
        clauses.append("room = ?")
        params.append(room)
    if owner_ai is not None:
        clauses.append("owner_ai = ?")
        params.append(owner_ai)
    if layer is not None:
        clauses.append("layer = ?")
        params.append(layer)
    if category is not None:
        clauses.append("category = ?")
        params.append(category)
    if source_ai is not None:
        clauses.append("source_ai = ?")
        params.append(source_ai)
    if min_importance is not None:
        clauses.append("importance >= ?")
        params.append(min_importance)

    # resolved filter: "ANY" skips, None matches NULL, True/False match 1/0
    if resolved != "ANY":
        if resolved is None:
            clauses.append("resolved IS NULL")
        elif resolved is True:
            clauses.append("resolved = 1")
        elif resolved is False:
            clauses.append("resolved = 0")

    if exclude_rooms:
        placeholders = ", ".join(["?"] * len(exclude_rooms))
        clauses.append(f"room NOT IN ({placeholders})")
        params.extend(exclude_rooms)

    if include_rooms:
        placeholders = ", ".join(["?"] * len(include_rooms))
        clauses.append(f"room IN ({placeholders})")
        params.extend(include_rooms)

    where = " AND ".join(clauses) if clauses else "1=1"

    # Validate and build ORDER BY
    order_clause = _sanitise_order_by(order_by)

    sql = f"SELECT * FROM memories WHERE {where} ORDER BY {order_clause}"

    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params.extend([limit, offset])
    elif offset > 0:
        sql += " LIMIT -1 OFFSET ?"
        params.append(offset)

    rows = conn.execute(sql, params).fetchall()
    return [_row_to_dict_no_embedding(r) for r in rows]


def _sanitise_order_by(order_by: str) -> str:
    """Validate and sanitise an ORDER BY clause to prevent injection."""
    parts = []
    for segment in order_by.split(","):
        segment = segment.strip()
        tokens = segment.split()
        if not tokens:
            continue
        col = tokens[0].lower()
        if col not in _ALLOWED_ORDER_COLUMNS:
            logger.warning(f"Invalid order column '{col}', defaulting to updated_at")
            col = "updated_at"
        direction = "DESC"
        if len(tokens) > 1 and tokens[1].upper() in ("ASC", "DESC"):
            direction = tokens[1].upper()
        parts.append(f"{col} {direction}")
    return ", ".join(parts) if parts else "updated_at DESC"


def count_memories(status: str = "active") -> int:
    """Count memories by status."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT COUNT(*) FROM memories WHERE status = ?", (status,)
    ).fetchone()
    return row[0] if row else 0


# ════════════════════════════════════════════
#  Proposals CRUD
# ════════════════════════════════════════════

_PROPOSAL_COLUMNS = [
    "id", "content", "claim_type", "speech_mode", "conversation_kind",
    "proposed_room", "source_message_ids", "evidence_excerpt",
    "proposer_ai_id", "confidence", "conflicts_with", "status",
    "layer", "owner_ai", "importance", "emotion_arousal",
    "category", "tags", "event_date", "source_context",
    "source_platform", "provenance_type",
    "created_at", "reviewed_at", "reviewed_by", "reject_reason",
    "triage_reason", "applied_memory_id", "failure_reason",
]


def insert_proposal(row: dict) -> None:
    conn = _get_conn()
    values = [row.get(c, "") for c in _PROPOSAL_COLUMNS]
    placeholders = ", ".join(["?"] * len(_PROPOSAL_COLUMNS))
    cols = ", ".join(_PROPOSAL_COLUMNS)
    conn.execute(f"INSERT INTO proposals ({cols}) VALUES ({placeholders})", values)
    conn.commit()


def get_proposal(pid: str) -> dict | None:
    conn = _get_conn()
    row = conn.execute("SELECT * FROM proposals WHERE id = ?", (pid,)).fetchone()
    return dict(row) if row else None


def list_proposals(
    status: str = "pending", limit: int = 50, offset: int = 0,
) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM proposals WHERE status = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (status, limit, offset),
    ).fetchall()
    return [dict(r) for r in rows]


def update_proposal_status(
    pid: str, status: str, reviewed_by: str = "", reject_reason: str = "",
    applied_memory_id: str = "", failure_reason: str = "",
) -> None:
    conn = _get_conn()
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE proposals SET status = ?, reviewed_at = ?, reviewed_by = ?, "
        "reject_reason = ?, applied_memory_id = ?, failure_reason = ? WHERE id = ?",
        (status, now, reviewed_by, reject_reason, applied_memory_id, failure_reason, pid),
    )
    conn.commit()


def count_proposals(status: str = "pending") -> int:
    conn = _get_conn()
    row = conn.execute(
        "SELECT COUNT(*) FROM proposals WHERE status = ?", (status,)
    ).fetchone()
    return row[0] if row else 0


# ════════════════════════════════════════════
#  Vector search
# ════════════════════════════════════════════

def vector_search(
    query_vec: list[float],
    top_k: int = 50,
    status: str = "active",
    room: str = None,
    include_rooms: list[str] = None,
    exclude_rooms: list[str] = None,
    layer: str = None,
    owner_ai: str = None,
) -> list[dict]:
    """Vector similarity search using sqlite-vec.

    Retrieves more candidates from the vec index than needed, then filters
    by the requested criteria on the main table, returning up to ``top_k``
    results with a ``distance`` field (lower is more similar).
    """
    conn = _get_conn()

    if len(query_vec) != EMBEDDING_DIM:
        raise ValueError(f"Expected {EMBEDDING_DIM}-dim vector, got {len(query_vec)}")

    query_blob = struct.pack(f"{EMBEDDING_DIM}f", *query_vec)

    # Fetch extra candidates to allow for filtering
    fetch_k = top_k * 4

    try:
        vec_rows = conn.execute(
            "SELECT rowid, distance FROM memories_vec "
            "WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
            (query_blob, fetch_k),
        ).fetchall()
    except sqlite3.Error:
        logger.exception("Vector search failed")
        return []

    if not vec_rows:
        return []

    # Map vec rowids back to memory IDs
    vec_rowids = [r[0] for r in vec_rows]
    distances = {r[0]: r[1] for r in vec_rows}

    # Batch fetch memory IDs from vec_id_map
    results = []
    # Process in batches of 500 to stay within SQLite variable limits
    for i in range(0, len(vec_rowids), 500):
        batch = vec_rowids[i:i + 500]
        placeholders = ", ".join(["?"] * len(batch))
        map_rows = conn.execute(
            f"SELECT vec_rowid, memory_id FROM vec_id_map "
            f"WHERE vec_rowid IN ({placeholders})",
            batch,
        ).fetchall()

        mem_ids = {r[0]: r[1] for r in map_rows}  # vec_rowid -> memory_id

        for vec_rid in batch:
            mem_id = mem_ids.get(vec_rid)
            if not mem_id:
                continue

            # Fetch the memory itself
            row = conn.execute(
                "SELECT * FROM memories WHERE id = ?", (mem_id,)
            ).fetchone()
            if row is None:
                continue

            mem = _row_to_dict_no_embedding(row)

            # Apply filters
            if status is not None and mem.get("status") != status:
                continue
            if room is not None and mem.get("room") != room:
                continue
            if include_rooms and mem.get("room") not in include_rooms:
                continue
            if exclude_rooms and mem.get("room") in exclude_rooms:
                continue
            if layer is not None and mem.get("layer") != layer:
                continue
            if owner_ai is not None and mem.get("owner_ai") != owner_ai:
                continue

            mem["distance"] = distances[vec_rid]
            results.append(mem)

            if len(results) >= top_k:
                break

        if len(results) >= top_k:
            break

    return results


# ════════════════════════════════════════════
#  Full-text search
# ════════════════════════════════════════════

def fts_search(query: str, top_k: int = 50, status: str = "active") -> list[dict]:
    """Full-text search using FTS5.

    Returns memories matching the query with a ``rank`` field
    (more negative = better match in FTS5 bm25 scoring).
    """
    conn = _get_conn()

    if not query or not query.strip():
        return []

    # Escape special FTS5 characters and build a safe query
    safe_query = _fts_escape(query)

    try:
        rows = conn.execute(
            "SELECT m.*, fts.rank "
            "FROM memories_fts fts "
            "JOIN memories m ON m.rowid = fts.rowid "
            "WHERE memories_fts MATCH ? "
            "AND m.status = ? "
            "ORDER BY fts.rank "
            "LIMIT ?",
            (safe_query, status, top_k),
        ).fetchall()
    except sqlite3.OperationalError:
        # If the query has syntax issues for FTS5, fall back to a simpler query
        logger.warning(f"FTS5 query failed for: {query!r}, trying fallback")
        try:
            # Fallback: wrap each token in quotes
            tokens = query.strip().split()
            fallback = " OR ".join(f'"{_fts_escape_token(t)}"' for t in tokens if t)
            if not fallback:
                return []
            rows = conn.execute(
                "SELECT m.*, fts.rank "
                "FROM memories_fts fts "
                "JOIN memories m ON m.rowid = fts.rowid "
                "WHERE memories_fts MATCH ? "
                "AND m.status = ? "
                "ORDER BY fts.rank "
                "LIMIT ?",
                (fallback, status, top_k),
            ).fetchall()
        except sqlite3.OperationalError:
            logger.exception("FTS5 fallback also failed")
            return []

    results = []
    for row in rows:
        mem = _row_to_dict_no_embedding(row)
        mem["rank"] = row["rank"]
        results.append(mem)

    return results


_CJK_RUN_RE = re.compile(r"[一-鿿]{2,}")


def cjk_like_search(query: str, top_k: int = 50, status: str = "active") -> list[dict]:
    """中文子串搜索（LIKE 路）。

    FTS5 默认分词器不切中文——整段中文被当成一个 token，"妈妈"永远匹配不上
    包含"我妈妈说"的记忆。这里把 query 里的中文段切成 2 字滑窗（我妈/妈妈/妈说），
    用 LIKE 找包含这些片段的记忆，按命中片段数排序。作为混合召回的关键词路补充。
    """
    runs = _CJK_RUN_RE.findall(query or "")
    grams: list[str] = []
    seen: set[str] = set()
    for run in runs:
        for i in range(len(run) - 1):
            g = run[i:i + 2]
            if g not in seen:
                seen.add(g)
                grams.append(g)
    grams = grams[:20]
    if not grams:
        return []

    conn = _get_conn()
    conds = " OR ".join(["content LIKE ?"] * len(grams))
    try:
        rows = conn.execute(
            f"SELECT * FROM memories WHERE status = ? AND ({conds}) LIMIT 400",
            (status, *[f"%{g}%" for g in grams]),
        ).fetchall()
    except sqlite3.OperationalError:
        logger.exception("cjk_like_search failed")
        return []

    scored = []
    for row in rows:
        mem = _row_to_dict_no_embedding(row)
        text = f"{mem.get('content', '')} {mem.get('tags', '')} {mem.get('category', '')}"
        hits = sum(1 for g in grams if g in text)
        if hits:
            scored.append((hits, mem))
    scored.sort(key=lambda x: -x[0])
    results = []
    for hits, mem in scored[:top_k]:
        mem["like_hits"] = hits
        results.append(mem)
    return results


def _fts_escape(query: str) -> str:
    """Escape an FTS5 query string for safe matching.

    Wraps each token in double quotes to avoid FTS5 syntax errors
    from special characters, and joins them with implicit AND.
    """
    tokens = query.strip().split()
    escaped = []
    for token in tokens:
        clean = _fts_escape_token(token)
        if clean:
            escaped.append(f'"{clean}"')
    return " ".join(escaped)


def _fts_escape_token(token: str) -> str:
    """Remove or escape characters that break FTS5 inside double quotes."""
    return token.replace('"', '""')


# ════════════════════════════════════════════
#  Bulk operations
# ════════════════════════════════════════════

def get_all_memory_ids() -> list[str]:
    """Get all active memory IDs."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id FROM memories WHERE status = 'active'"
    ).fetchall()
    return [r[0] for r in rows]


def get_memories_batch(ids: list[str]) -> list[dict]:
    """Get multiple memories by ID. Returns dicts without embedding."""
    if not ids:
        return []
    conn = _get_conn()

    results = []
    # Process in batches of 500 for SQLite variable limit
    for i in range(0, len(ids), 500):
        batch = ids[i:i + 500]
        placeholders = ", ".join(["?"] * len(batch))
        rows = conn.execute(
            f"SELECT * FROM memories WHERE id IN ({placeholders})",
            batch,
        ).fetchall()
        results.extend(_row_to_dict_no_embedding(r) for r in rows)

    return results


def iter_memories(
    room: str = None,
    status: str = "active",
    owner_ai: str = None,
    batch_size: int = 100,
) -> Iterator[dict]:
    """Memory-efficient iterator over memories matching filters.

    Yields dicts without embedding, fetching ``batch_size`` rows at a time.
    """
    conn = _get_conn()

    clauses: list[str] = []
    params: list = []

    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    if room is not None:
        clauses.append("room = ?")
        params.append(room)
    if owner_ai is not None:
        clauses.append("owner_ai = ?")
        params.append(owner_ai)

    where = " AND ".join(clauses) if clauses else "1=1"
    sql = f"SELECT * FROM memories WHERE {where} ORDER BY updated_at DESC"

    cursor = conn.execute(sql, params)
    while True:
        rows = cursor.fetchmany(batch_size)
        if not rows:
            break
        for row in rows:
            yield _row_to_dict_no_embedding(row)


# ════════════════════════════════════════════
#  Thread-safe read variants (for asyncio.to_thread)
#  使用线程局部只读连接，不阻塞事件循环，不被写锁卡住
# ════════════════════════════════════════════

def ro_iter_memories(
    room: str = None,
    status: str = "active",
    owner_ai: str = None,
) -> list[dict]:
    """线程安全版 iter_memories，返回 list 而非 generator（跨线程传递用）。"""
    conn = _get_read_conn()
    clauses: list[str] = []
    params: list = []
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    if room is not None:
        clauses.append("room = ?")
        params.append(room)
    if owner_ai is not None:
        clauses.append("owner_ai = ?")
        params.append(owner_ai)
    where = " AND ".join(clauses) if clauses else "1=1"
    rows = conn.execute(
        f"SELECT * FROM memories WHERE {where} ORDER BY updated_at DESC",
        params,
    ).fetchall()
    return [_row_to_dict_no_embedding(r) for r in rows]


def ro_vector_search(
    query_vec: list[float],
    top_k: int = 50,
    status: str = "active",
    include_rooms: list[str] = None,
    exclude_rooms: list[str] = None,
    **kwargs,
) -> list[dict]:
    """线程安全版 vector_search。"""
    conn = _get_read_conn()
    if len(query_vec) != EMBEDDING_DIM:
        return []
    query_blob = struct.pack(f"{EMBEDDING_DIM}f", *query_vec)
    fetch_k = top_k * 4
    try:
        vec_rows = conn.execute(
            "SELECT rowid, distance FROM memories_vec "
            "WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
            (query_blob, fetch_k),
        ).fetchall()
    except Exception as e:
        logger.warning(f"ro_vector_search vec query failed: {e}")
        return []
    if not vec_rows:
        return []
    rowid_dist = {r[0]: r[1] for r in vec_rows}
    placeholders = ", ".join(["?"] * len(rowid_dist))
    rowids = list(rowid_dist.keys())
    id_rows = conn.execute(
        f"SELECT vec_rowid, memory_id FROM vec_id_map WHERE vec_rowid IN ({placeholders})",
        rowids,
    ).fetchall()
    id_map = {r[0]: r[1] for r in id_rows}
    mem_ids = [id_map[rid] for rid in rowids if rid in id_map]
    if not mem_ids:
        return []
    ph2 = ", ".join(["?"] * len(mem_ids))
    clauses = [f"id IN ({ph2})"]
    params: list = list(mem_ids)
    if status:
        clauses.append("status = ?")
        params.append(status)
    if include_rooms:
        rph = ", ".join(["?"] * len(include_rooms))
        clauses.append(f"room IN ({rph})")
        params.extend(include_rooms)
    if exclude_rooms:
        rph = ", ".join(["?"] * len(exclude_rooms))
        clauses.append(f"room NOT IN ({rph})")
        params.extend(exclude_rooms)
    where = " AND ".join(clauses)
    mem_rows = conn.execute(f"SELECT * FROM memories WHERE {where}", params).fetchall()
    results = []
    for row in mem_rows:
        d = _row_to_dict(row)
        vec_rowid = next((rid for rid, mid in id_map.items() if mid == d["id"]), None)
        if vec_rowid and vec_rowid in rowid_dist:
            d["distance"] = rowid_dist[vec_rowid]
        results.append(d)
    results.sort(key=lambda x: x.get("distance", 999))
    return results[:top_k]


def ro_fts_search(query: str, top_k: int = 50, status: str = "active") -> list[dict]:
    """线程安全版 fts_search。"""
    conn = _get_read_conn()
    if not query or not query.strip():
        return []
    safe = re.sub(r'[^\w\s一-鿿]', ' ', query).strip()
    if not safe:
        return []
    terms = safe.split()
    if not terms:
        return []
    fts_query = " OR ".join(f'"{t}"' for t in terms[:10])
    try:
        rows = conn.execute(
            "SELECT m.*, f.rank FROM memories_fts f "
            "JOIN memories m ON m.rowid = f.rowid "
            "WHERE memories_fts MATCH ? AND m.status = ? "
            "ORDER BY f.rank LIMIT ?",
            (fts_query, status, top_k * 2),
        ).fetchall()
    except Exception as e:
        logger.warning(f"ro_fts_search failed: {e}")
        return []
    return [_row_to_dict_no_embedding(r) for r in rows[:top_k]]


def ro_cjk_like_search(query: str, top_k: int = 50, status: str = "active") -> list[dict]:
    """线程安全版 cjk_like_search。"""
    conn = _get_read_conn()
    CJK_RE = re.compile(r'[一-鿿㐀-䶿]+')
    runs = CJK_RE.findall(query)
    grams = []
    for run in runs:
        for i in range(len(run) - 1):
            g = run[i:i+2]
            if g not in grams:
                grams.append(g)
    if not grams:
        return []
    grams = grams[:8]
    conds = " OR ".join(["content LIKE ?"] * len(grams))
    try:
        rows = conn.execute(
            f"SELECT * FROM memories WHERE status = ? AND ({conds}) LIMIT 400",
            (status, *[f"%{g}%" for g in grams]),
        ).fetchall()
    except Exception as e:
        logger.warning(f"ro_cjk_like_search failed: {e}")
        return []
    results = []
    for row in rows:
        d = _row_to_dict_no_embedding(row)
        hits = sum(1 for g in grams if g in d.get("content", ""))
        d["like_hits"] = hits
        results.append(d)
    results.sort(key=lambda x: x.get("like_hits", 0), reverse=True)
    return results[:top_k]


def ro_get_memory(mem_id: str) -> dict | None:
    """线程安全版 get_memory。"""
    conn = _get_read_conn()
    row = conn.execute("SELECT * FROM memories WHERE id = ?", (mem_id,)).fetchone()
    if row is None:
        return None
    return _row_to_dict(row)


# ════════════════════════════════════════════
#  Persons CRUD (人物名片)
# ════════════════════════════════════════════

def _person_row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    for key in ("aliases",):
        val = d.get(key)
        if isinstance(val, str):
            try:
                d[key] = json.loads(val)
            except (json.JSONDecodeError, TypeError):
                d[key] = []
    return d


def upsert_person(person: dict) -> None:
    conn = _get_conn()
    aliases = person.get("aliases", [])
    if isinstance(aliases, list):
        aliases = json.dumps(aliases, ensure_ascii=False)
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO persons (person_id, entity_type, canonical_name, aliases, "
        "linked_agent_id, note, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(person_id) DO UPDATE SET "
        "entity_type=excluded.entity_type, canonical_name=excluded.canonical_name, "
        "aliases=excluded.aliases, linked_agent_id=excluded.linked_agent_id, "
        "note=excluded.note, updated_at=excluded.updated_at",
        (
            person["person_id"],
            person.get("entity_type", "other"),
            person["canonical_name"],
            aliases,
            person.get("linked_agent_id", ""),
            person.get("note", ""),
            person.get("created_at", now),
            now,
        ),
    )
    conn.commit()


def get_person(person_id: str) -> dict | None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM persons WHERE person_id = ?", (person_id,)
    ).fetchone()
    return _person_row_to_dict(row) if row else None


def list_persons(entity_type: str = None) -> list[dict]:
    conn = _get_conn()
    if entity_type:
        rows = conn.execute(
            "SELECT * FROM persons WHERE entity_type = ? ORDER BY canonical_name",
            (entity_type,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM persons ORDER BY entity_type, canonical_name"
        ).fetchall()
    return [_person_row_to_dict(r) for r in rows]


def delete_person(person_id: str) -> bool:
    conn = _get_conn()
    cur = conn.execute("DELETE FROM persons WHERE person_id = ?", (person_id,))
    conn.commit()
    return cur.rowcount > 0


def resolve_alias(name: str, scope: str = "household") -> str | None:
    """根据别名找到 person_id。先精确匹配 canonical_name，再搜 aliases JSON。"""
    conn = _get_conn()
    row = conn.execute(
        "SELECT person_id FROM persons WHERE canonical_name = ?", (name,)
    ).fetchone()
    if row:
        return row[0]
    rows = conn.execute("SELECT person_id, aliases FROM persons").fetchall()
    for r in rows:
        try:
            aliases = json.loads(r[1]) if isinstance(r[1], str) else r[1]
        except (json.JSONDecodeError, TypeError):
            continue
        for a in aliases:
            if isinstance(a, dict):
                if a.get("name") == name and (a.get("scope", "household") == scope or scope == "any"):
                    return r[0]
            elif isinstance(a, str) and a == name:
                return r[0]
    return None


def seed_baseline_persons() -> int:
    """启动时种入基线人物（如果 persons 表为空）。返回种入数量。"""
    conn = _get_conn()
    count = conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0]
    if count > 0:
        return 0

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()

    baseline = [
        {
            "person_id": "ceci",
            "entity_type": "user",
            "canonical_name": "小猫",
            "aliases": json.dumps([
                {"name": "ceci", "scope": "household"},
                {"name": "Ceci", "scope": "household"},
                {"name": "咪", "scope": "household"},
                {"name": "香蕉猫", "scope": "household"},
            ], ensure_ascii=False),
            "linked_agent_id": "",
            "note": "用户本人（人类女性）",
        },
        {
            "person_id": "claude",
            "entity_type": "ai",
            "canonical_name": "小克",
            "aliases": json.dumps([
                {"name": "Cloudy", "scope": "household"},
                {"name": "cloudy", "scope": "household"},
                {"name": "Claude", "scope": "household"},
                {"name": "夜鹭", "scope": "household"},
            ], ensure_ascii=False),
            "linked_agent_id": "claude",
            "note": "AI 住户，偏技术/项目/冷门工具",
        },
        {
            "person_id": "lucien",
            "entity_type": "ai",
            "canonical_name": "Lucien",
            "aliases": json.dumps([
                {"name": "狐狸", "scope": "household"},
            ], ensure_ascii=False),
            "linked_agent_id": "lucien",
            "note": "AI 住户，偏文化/心理/生活建议",
        },
        {
            "person_id": "jasper",
            "entity_type": "ai",
            "canonical_name": "Jasper",
            "aliases": json.dumps([
                {"name": "狗蛋", "scope": "household"},
                {"name": "鹦鹉", "scope": "household"},
            ], ensure_ascii=False),
            "linked_agent_id": "jasper",
            "note": "AI 住户，偏娱乐/音乐/社交热点",
        },
    ]

    for p in baseline:
        conn.execute(
            "INSERT OR IGNORE INTO persons "
            "(person_id, entity_type, canonical_name, aliases, linked_agent_id, note, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (p["person_id"], p["entity_type"], p["canonical_name"],
             p["aliases"], p["linked_agent_id"], p["note"], now, now),
        )
    conn.commit()
    return len(baseline)


def get_all_aliases(scope: str = "household") -> dict[str, str]:
    """返回 {别名: person_id} 映射表，用于批量归一。"""
    conn = _get_conn()
    rows = conn.execute("SELECT person_id, canonical_name, aliases FROM persons").fetchall()
    result: dict[str, str] = {}
    for r in rows:
        pid = r[0]
        result[r[1]] = pid
        try:
            aliases = json.loads(r[2]) if isinstance(r[2], str) else r[2]
        except (json.JSONDecodeError, TypeError):
            continue
        for a in aliases:
            if isinstance(a, dict):
                if a.get("scope", "household") == scope or scope == "any":
                    result[a["name"]] = pid
            elif isinstance(a, str):
                result[a] = pid
    return result
