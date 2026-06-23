"""
SQLite 数据库引擎（替代内存 dict + GitHub 存储）
- sqlite-vec 向量搜索
- FTS5 全文搜索
- WAL 模式并发读
- 同步 sqlite3（够快，避免 async 复杂度）
"""
import json
import struct
import os
import sqlite3
import logging
from pathlib import Path
from typing import Iterator

from config import DATA_DIR

logger = logging.getLogger("memory_hub.db")

# ── 默认数据库路径 ──
DB_PATH: Path = DATA_DIR / "memories.db"

# ── 模块级连接 ──
_conn: sqlite3.Connection | None = None

# embedding 维度：从环境变量读取，默认 1024（bge-large-zh-v1.5）
EMBEDDING_DIM = int(os.environ.get("EMBEDDING_DIM", "1024"))


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
    anchored        INTEGER
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
CREATE INDEX IF NOT EXISTS idx_mem_anchored   ON memories(anchored);
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
    "history", "resolved", "anchored",
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
        if val is None:
            return ""
        return val

    values = [_prep(col) for col in _ALL_COLUMNS]
    placeholders = ", ".join(["?"] * len(_ALL_COLUMNS))
    cols = ", ".join(_ALL_COLUMNS)
    update_set = ", ".join(f"{c} = excluded.{c}" for c in _ALL_COLUMNS if c != "id")

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
