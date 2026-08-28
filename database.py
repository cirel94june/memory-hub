"""
SQLite 数据库引擎（替代内存 dict + GitHub 存储）
- sqlite-vec 向量搜索
- FTS5 全文搜索
- WAL 模式并发读
- 同步 sqlite3，hot-path 读操作通过 to_thread 离开事件循环
"""
import contextlib
import json
import re
import struct
import sqlite3
import asyncio
import logging
import threading
from datetime import datetime, timezone, timedelta
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


# ── 写锁 + 事务 context manager（Phase 2.0 Step 0-A #2a）──
# 所有共享 _conn 上的写操作必须包在 _write_transaction() 内。threading.Lock
# 非重入：同线程嵌套调用立即 RuntimeError（防死锁）。v2.9 H1 修正版：try/finally
# 保证 _in_write_tx.active 与锁在中间抛异常时也恢复；began flag 控制 ROLLBACK
# 只对已开事务生效。
_WRITE_LOCK = threading.Lock()
_in_write_tx = threading.local()


@contextlib.contextmanager
def _write_transaction():
    """共享 _conn 上任何写操作必须包在本 ctx 内。

    非重入：同线程嵌套调用立即 RuntimeError（不 deadlock）。正常退出 commit；
    异常 ROLLBACK 并 re-raise。空 tx（仅读或 no-op）commit 是安全的。
    """
    if getattr(_in_write_tx, 'active', False):
        raise RuntimeError(
            "nested _write_transaction() forbidden — caller inside a write tx "
            "must not call another write helper. Refactor to do all work "
            "inside one _write_transaction() block."
        )
    lock_acquired = False
    began = False
    conn = None
    try:
        _WRITE_LOCK.acquire()
        lock_acquired = True
        _in_write_tx.active = True
        conn = _get_conn()
        conn.execute("BEGIN IMMEDIATE")
        began = True
        yield conn
        conn.commit()
        began = False
    except BaseException:
        if began and conn is not None:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
        raise
    finally:
        _in_write_tx.active = False
        if lock_acquired:
            _WRITE_LOCK.release()


def _get_read_conn() -> sqlite3.Connection:
    """每个线程独立的只读连接，WAL 模式下不会被写锁阻塞。

    Phase 2.0 Step 0-A #4 (v2.9 M2): 缓存 read_conn + read_db_path；DB_PATH 变化
    时关旧建新（--db-path backfill / test 切库 / init_db(path_B) 后同一线程仍
    能读到正确的 DB）。
    """
    cur_path = str(DB_PATH)
    conn = getattr(_local, "read_conn", None)
    cached_path = getattr(_local, "read_db_path", None)
    if conn is not None and cached_path == cur_path:
        return conn
    # 路径变了或首次调用 → 关旧（若有）建新
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    conn = sqlite3.connect(f"file:{cur_path}?mode=ro", uri=True, check_same_thread=False)
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
    _local.read_db_path = cur_path
    return conn


def close_thread_read_conn() -> None:
    """关闭当前线程的只读连接（进程 shutdown / --db-path 切库前调用）。

    Phase 2.0 Step 0-A #4: 避免 fd 泄漏；主进程 shutdown hook 调用；backfill
    脚本切库前调用；测试之间隔离亦可复用。
    """
    conn = getattr(_local, "read_conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
        _local.read_conn = None
        _local.read_db_path = None


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

    Phase 2.0 Step 0-A #4 fixup (Codex High): if `db_path` is provided,
    update the module-level DB_PATH so `_get_read_conn()` will see the new
    path on its next call and rebuild its cached connection. Otherwise the
    read helpers would keep pointing at the old DB (write B, read A — real
    bug scripts/supersede_old_profiles.py would hit).

    Phase 2.0 Step 0-A #4 fixup round-2 (Codex Medium): two-phase swap —
    all setup (connect / pragmas / vec load / migrations) runs on a local
    `new_conn`. Only after success do we swap in the new DB_PATH + close
    old read connections + close old _conn. If any setup step raises, the
    old state (DB_PATH / _conn / cached read connections) is untouched and
    the failed `new_conn` is closed to prevent fd leak. Prevents "init B
    failed → DB_PATH already changed to B → write goes to leftover old
    _conn (A) → public read helpers hit missing B" split state.
    """
    global _conn, DB_PATH

    new_db_path = Path(db_path) if db_path is not None else DB_PATH
    path = str(new_db_path)
    logger.info(f"Initialising SQLite database at {path}")

    conn = sqlite3.connect(path, check_same_thread=False)
    try:
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

        if "subject_id" not in existing_cols:
            conn.execute("ALTER TABLE memories ADD COLUMN subject_id TEXT NOT NULL DEFAULT ''")
            logger.info("Migrated: added 'subject_id' column")
        if "source_speaker_id" not in existing_cols and "source_actor_id" not in existing_cols:
            conn.execute("ALTER TABLE memories ADD COLUMN source_actor_id TEXT NOT NULL DEFAULT ''")
            logger.info("Migrated: added 'source_actor_id' column")
        if "source_speaker_id" in existing_cols and "source_actor_id" not in existing_cols:
            conn.execute("ALTER TABLE memories RENAME COLUMN source_speaker_id TO source_actor_id")
            logger.info("Migrated: renamed 'source_speaker_id' → 'source_actor_id'")
        if "info_type" not in existing_cols:
            conn.execute("ALTER TABLE memories ADD COLUMN info_type TEXT NOT NULL DEFAULT 'fact'")
            logger.info("Migrated: added 'info_type' column")

        # PR C (块 8): async remember 支持——幂等 key + supersede 后骨架追踪
        if "client_request_id" not in existing_cols:
            conn.execute("ALTER TABLE memories ADD COLUMN client_request_id TEXT NOT NULL DEFAULT ''")
            logger.info("Migrated: added 'client_request_id' column")
        if "link_to_real_id" not in existing_cols:
            conn.execute("ALTER TABLE memories ADD COLUMN link_to_real_id TEXT NOT NULL DEFAULT ''")
            logger.info("Migrated: added 'link_to_real_id' column")
        # PR C round-4 H1: real atomic claim for sweep retries. Without this,
        # two concurrent sweeps can both spawn a finalize for the same skeleton.
        if "finalize_claim_id" not in existing_cols:
            conn.execute("ALTER TABLE memories ADD COLUMN finalize_claim_id TEXT NOT NULL DEFAULT ''")
            logger.info("Migrated: added 'finalize_claim_id' column")
        if "finalize_claim_at" not in existing_cols:
            conn.execute("ALTER TABLE memories ADD COLUMN finalize_claim_at TEXT NOT NULL DEFAULT ''")
            logger.info("Migrated: added 'finalize_claim_at' column")

        conn.execute("CREATE INDEX IF NOT EXISTS idx_mem_anchored ON memories(anchored)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mem_subject ON memories(subject_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mem_info_type ON memories(info_type)")
        # Partial unique index: 空字符串 client_request_id 不受约束（老记忆全部 ''）
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_mem_client_req "
            "ON memories(client_request_id) WHERE client_request_id != ''"
        )

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
            ("subject_id", "TEXT NOT NULL DEFAULT ''"),
            ("source_actor_id", "TEXT NOT NULL DEFAULT ''"),
            ("info_type", "TEXT NOT NULL DEFAULT 'fact'"),
            ("maintenance_action", "TEXT NOT NULL DEFAULT ''"),
            ("maintenance_target_id", "TEXT NOT NULL DEFAULT ''"),
        ]:
            if col not in existing:
                conn.execute(f"ALTER TABLE proposals ADD COLUMN {col} {typedef}")
                logger.info(f"Migrated proposals: added '{col}' column")
        if "source_speaker_id" in existing and "source_actor_id" not in existing:
            conn.execute("ALTER TABLE proposals RENAME COLUMN source_speaker_id TO source_actor_id")
            logger.info("Migrated proposals: renamed 'source_speaker_id' → 'source_actor_id'")

        # ── Maintenance Audit table ──
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS maintenance_audit (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                action              TEXT NOT NULL,
                target_id           TEXT NOT NULL DEFAULT '',
                new_content         TEXT NOT NULL DEFAULT '',
                source_message_ids  TEXT NOT NULL DEFAULT '[]',
                decision_reason     TEXT NOT NULL DEFAULT '',
                state_before        TEXT NOT NULL DEFAULT '{}',
                state_after         TEXT NOT NULL DEFAULT '{}',
                model_id            TEXT NOT NULL DEFAULT '',
                source_ai           TEXT NOT NULL DEFAULT '',
                auto_executed       INTEGER NOT NULL DEFAULT 1,
                prompt_version      TEXT NOT NULL DEFAULT '',
                created_at          TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_audit_created ON maintenance_audit(created_at);
            CREATE INDEX IF NOT EXISTS idx_audit_action ON maintenance_audit(action);
            CREATE INDEX IF NOT EXISTS idx_audit_target ON maintenance_audit(target_id);
        """)

        audit_cols = {row[1] for row in conn.execute("PRAGMA table_info(maintenance_audit)").fetchall()}
        if "prompt_version" not in audit_cols:
            conn.execute("ALTER TABLE maintenance_audit ADD COLUMN prompt_version TEXT NOT NULL DEFAULT ''")
            logger.info("Migrated maintenance_audit: added 'prompt_version' column")

        # ── PR C H2: async_remember_ledger ──
        # Records the terminal outcome of each async remember pipeline. Written
        # in the SAME transaction as the memory changes (via _commit_ledger),
        # so a mid-flight crash cannot leave the ledger and memory tables out
        # of sync. Sweep consults the ledger BEFORE retrying — if a terminal
        # state exists, sweep applies it without re-running the pipeline.
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS async_remember_ledger (
                skeleton_id       TEXT PRIMARY KEY,
                client_request_id TEXT NOT NULL DEFAULT '',
                terminal_state    TEXT NOT NULL,   -- 'in_flight' | 'active' | 'replaced' | 'failed'
                result_memory_id  TEXT NOT NULL,   -- real memory id after pipeline
                committed_at      TEXT NOT NULL,
                owner_token       TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_ledger_crq
                ON async_remember_ledger(client_request_id);
        """)

        # H1 round-6: owner_token migration for pre-existing ledger tables.
        ledger_cols = {row[1] for row in conn.execute(
            "PRAGMA table_info(async_remember_ledger)").fetchall()}
        if "owner_token" not in ledger_cols:
            conn.execute(
                "ALTER TABLE async_remember_ledger "
                "ADD COLUMN owner_token TEXT NOT NULL DEFAULT ''")
            logger.info("Migrated async_remember_ledger: added 'owner_token'")

        # ── Profiles table migration ──
        try:
            profile_cols = {row[1] for row in conn.execute("PRAGMA table_info(profiles)").fetchall()}
            if profile_cols and "status" not in profile_cols:
                conn.execute("ALTER TABLE profiles ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
                logger.info("Migrated profiles: added 'status' column")
        except sqlite3.OperationalError:
            pass

        # ── Dream dedup table (one dream per AI per local day) ──
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS dream_log (
                ai_id       TEXT NOT NULL,
                local_day   TEXT NOT NULL,
                memory_id   TEXT NOT NULL DEFAULT '',
                created_at  TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (ai_id, local_day)
            );
        """)

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

        # ── Profiles table (User/Agent/Relationship Profile) ──
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS profiles (
                id              TEXT PRIMARY KEY,
                profile_type    TEXT NOT NULL,
                owner_ai        TEXT NOT NULL DEFAULT '',
                content         TEXT NOT NULL DEFAULT '{}',
                generated_at    TEXT NOT NULL DEFAULT '',
                source_memory_ids TEXT NOT NULL DEFAULT '[]',
                version         INTEGER NOT NULL DEFAULT 1,
                status          TEXT NOT NULL DEFAULT 'pending_review'
            );
            CREATE INDEX IF NOT EXISTS idx_profile_type ON profiles(profile_type);
            CREATE INDEX IF NOT EXISTS idx_profile_owner ON profiles(owner_ai);
            CREATE INDEX IF NOT EXISTS idx_profile_status ON profiles(status);
        """)

        conn.commit()
    except BaseException:
        # Setup failed — close the half-initialised conn and re-raise
        # WITHOUT touching module globals. DB_PATH / _conn / read_conn
        # remain pointing at whatever was in place before init_db was called.
        try:
            conn.close()
        except Exception:
            pass
        raise

    # Two-phase swap: only after all setup succeeded do we mutate globals.
    # Close the previously-installed write conn (if any) to avoid fd leak on
    # re-init; close the current thread's cached read conn so the next
    # _get_read_conn() rebuilds against the new DB_PATH.
    old_conn = _conn
    DB_PATH = new_db_path
    _conn = conn
    close_thread_read_conn()
    if old_conn is not None and old_conn is not conn:
        try:
            old_conn.close()
        except Exception:
            pass
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
    "subject_id", "source_actor_id", "info_type",
    "client_request_id", "link_to_real_id",
    "finalize_claim_id", "finalize_claim_at",
]


def get_memory(mem_id: str) -> dict | None:
    """Get a single memory by ID. Returns full dict including embedding."""
    conn = _get_read_conn()
    row = conn.execute("SELECT * FROM memories WHERE id = ?", (mem_id,)).fetchone()
    if row is None:
        return None
    return _row_to_dict(row)


# ── PR C 块 8: async remember 支持 ──

def get_memory_by_client_request_id(crq: str) -> dict | None:
    """Idempotent lookup: find any memory with matching client_request_id
    regardless of status (pending/active/replaced/failed)."""
    if not crq:
        return None
    conn = _get_read_conn()
    row = conn.execute(
        "SELECT * FROM memories WHERE client_request_id = ? LIMIT 1", (crq,)
    ).fetchone()
    return _row_to_dict(row) if row else None


def insert_pending_memory(mem: dict) -> None:
    """Insert a pending-status skeleton (no embedding, no analyzer fields).

    Raises sqlite3.IntegrityError if the client_request_id collides with an
    existing row — MCP layer catches this to return an idempotent response.

    tags / domain: honored from mem if provided (as JSON string OR list).
    Historically this method hard-coded '[]' which silently dropped any
    caller-provided values.
    """
    now = mem.get("created_at") or _now_iso()

    def _as_json_list(val):
        if val is None or val == "":
            return "[]"
        if isinstance(val, (list, tuple)):
            return json.dumps(list(val), ensure_ascii=False)
        # Already-serialized string
        return val

    with _write_transaction() as conn:
        conn.execute(
            "INSERT INTO memories ("
            "  id, content, layer, room, category, owner_ai, importance,"
            "  source_ai, source_platform, event_date, source_context,"
            "  status, client_request_id, created_at, updated_at, tags, domain"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                mem["id"], mem.get("content", ""), mem.get("layer", "shared"),
                mem.get("room", "living_room"), mem.get("category", ""),
                mem.get("owner_ai", ""), float(mem.get("importance") or 0.5),
                mem.get("source_ai", ""), mem.get("source_platform", ""),
                mem.get("event_date", ""), mem.get("source_context", ""),
                mem.get("status", "pending"), mem.get("client_request_id", ""),
                now, now,
                _as_json_list(mem.get("tags")),
                _as_json_list(mem.get("domain")),
            ),
        )


def update_memory_status(mem_id: str, status: str,
                         source_platform_suffix: str = "",
                         require_status: str | None = None) -> int:
    """Atomic status update. Returns the number of rows affected (0 or 1).

    require_status: if set, add `AND status = ?` to the WHERE clause. Use
    this to atomically claim/transition a row only when it is still in the
    expected state — critical for the pending sweep, which must not clobber
    a row that a concurrent finalize just marked 'active'.

    source_platform_suffix: optionally append a suffix to source_platform
    so downstream can tell WHY the row is in this state
    (e.g. ':pipeline_error' vs ':sweep_timeout'). Idempotent.
    """
    now = _now_iso()
    if source_platform_suffix:
        suffix = source_platform_suffix if source_platform_suffix.startswith(":") \
                 else ":" + source_platform_suffix
        sql = (
            "UPDATE memories SET status = ?, updated_at = ?, "
            "source_platform = CASE "
            "  WHEN source_platform LIKE '%' || ? THEN source_platform "
            "  ELSE source_platform || ? END "
            "WHERE id = ?"
        )
        params: list = [status, now, suffix, suffix, mem_id]
    else:
        sql = "UPDATE memories SET status = ?, updated_at = ? WHERE id = ?"
        params = [status, now, mem_id]

    if require_status is not None:
        sql += " AND status = ?"
        params.append(require_status)

    with _write_transaction() as conn:
        cur = conn.execute(sql, params)
        rowcount = cur.rowcount
    return rowcount


def mark_replaced(skeleton_id: str, link_to_real_id: str) -> None:
    """Mark a pending skeleton as replaced when memory_ops.remember() returned
    a different real_id (merge/supersede path). Skeleton is NOT deleted so
    idempotency lookups by client_request_id still find it and can redirect
    to the real memory via link_to_real_id.
    """
    if not skeleton_id or not link_to_real_id:
        raise ValueError("mark_replaced requires both skeleton_id and link_to_real_id")
    now = _now_iso()
    with _write_transaction() as conn:
        conn.execute(
            "UPDATE memories SET status = 'replaced', link_to_real_id = ?, "
            "updated_at = ? WHERE id = ?",
            (link_to_real_id, now, skeleton_id),
        )


def write_intent_ledger(skeleton_id: str, client_request_id: str,
                        owner_token: str) -> str:
    """H2 round-4 + H1 round-6: two-phase ledger with owner_token so
    concurrent attempts don't misclaim ownership via matching timestamps.

    Returns:
      - 'created'    → this caller (identified by owner_token) wrote a fresh
                       in_flight row; safe to proceed.
      - 'in_flight'  → another finalize is already running; caller MUST back off.
      - 'active' | 'replaced' | 'failed' → terminal state exists; caller MUST
                       apply it via _apply_ledger_to_skeleton and NOT re-run
                       pipeline.

    Ownership via cursor.rowcount from INSERT OR IGNORE (SQLite semantics:
    rowcount == 1 on real insert, 0 on conflict). Owner_token also stored
    so commit_finalize_atomic can guard the terminal UPDATE.

    fail-closed (H2 round-6): if the DB write raises, raise the exception so
    the caller does NOT proceed with pipeline side effects.
    """
    if not skeleton_id:
        return "created"  # no idempotency requested for this call
    if not owner_token:
        raise ValueError("write_intent_ledger requires a non-empty owner_token")
    now = _now_iso()
    with _write_transaction() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO async_remember_ledger "
            "(skeleton_id, client_request_id, terminal_state, "
            " result_memory_id, committed_at, owner_token) "
            "VALUES (?, ?, 'in_flight', '', ?, ?)",
            (skeleton_id, client_request_id, now, owner_token),
        )
        if cur.rowcount == 1:
            # We inserted the row — we own the intent.
            return "created"

        # INSERT ignored — a row already exists. Read the winner's state.
        row = conn.execute(
            "SELECT terminal_state FROM async_remember_ledger "
            "WHERE skeleton_id = ?", (skeleton_id,),
        ).fetchone()
        if not row:
            # Extremely unlikely: no row despite INSERT OR IGNORE not inserting.
            # Fail-closed: treat as unknown; caller should NOT proceed.
            return "in_flight"
        return row[0] if row[0] else "in_flight"


def list_stale_intent_ledgers(older_than_minutes: int = 30) -> list[dict]:
    """H2 round-6: find in_flight ledger entries whose owner has been silent
    too long. These skeletons must be marked failed (NOT retried) because
    we can't tell whether the crashed pipeline already mutated the target
    memory — replaying would risk double-merge.

    Sweep should mark each returned skeleton failed + close out the ledger.
    """
    conn = _get_read_conn()
    cutoff = (datetime.now(timezone.utc)
              - timedelta(minutes=older_than_minutes)).isoformat()
    rows = conn.execute(
        "SELECT skeleton_id, client_request_id, committed_at, owner_token "
        "FROM async_remember_ledger "
        "WHERE terminal_state = 'in_flight' AND committed_at <= ?",
        (cutoff,),
    ).fetchall()
    return [
        {"skeleton_id": r[0], "client_request_id": r[1],
         "committed_at": r[2], "owner_token": r[3]}
        for r in rows
    ]


def close_stale_intent(skeleton_id: str, owner_token: str) -> bool:
    """DEPRECATED: kept for backwards compat. Prefer
    close_stale_intent_atomic which reconciles ledger + skeleton + audit
    in one transaction."""
    now = _now_iso()
    with _write_transaction() as conn:
        cur = conn.execute(
            "UPDATE async_remember_ledger "
            "SET terminal_state = 'failed', committed_at = ? "
            "WHERE skeleton_id = ? AND terminal_state = 'in_flight' "
            "  AND owner_token = ?",
            (now, skeleton_id, owner_token),
        )
        rowcount = cur.rowcount
    return rowcount == 1


def close_stale_intent_atomic(
    skeleton_id: str, owner_token: str, reason: str,
) -> dict:
    """H round-7: atomic reconciliation of a stale intent ledger with the
    skeleton row.

    A crashed pipeline may have completed *some* side effects before dying.
    The skeleton row is the source of truth for what actually happened:

      - skeleton status='pending'   → no side effects committed →
        ledger + skeleton → 'failed'; write intent_timeout audit.
      - skeleton status='active'    → create pipeline reused the skeleton
        and committed set_memory() but crashed before terminal ledger →
        ledger → 'active' (catches up); result_memory_id = skeleton_id.
      - skeleton status='replaced'  → merge pipeline committed and set
        link_to_real_id but crashed before terminal ledger →
        ledger → 'replaced' (catches up); result_memory_id from link.
      - any other status            → leave ledger in_flight for human
        review (return dict with disposition='needs_review').

    Every branch runs inside ONE BEGIN IMMEDIATE. If anything fails, all
    rows roll back — no partial ledger/skeleton/audit state.

    Returns: {"disposition": ..., "transitioned": bool, "skeleton_status": ...}
    """
    now = _now_iso()
    with _write_transaction() as conn:

        # 1. Re-verify ledger is still our stale in_flight (defense against
        #    race where the ledger raced between our list_stale_* snapshot
        #    and this close attempt).
        ledger_row = conn.execute(
            "SELECT terminal_state, owner_token FROM async_remember_ledger "
            "WHERE skeleton_id = ?", (skeleton_id,),
        ).fetchone()
        if not ledger_row or ledger_row[0] != "in_flight" \
                or ledger_row[1] != owner_token:
            # Read-only bail-out — ctx commits an empty tx (no-op).
            return {"disposition": "already_terminaled",
                    "transitioned": False, "skeleton_status": None}

        # 2. Read skeleton current status.
        skel_row = conn.execute(
            "SELECT status, link_to_real_id, source_platform "
            "FROM memories WHERE id = ?", (skeleton_id,),
        ).fetchone()
        if not skel_row:
            # Skeleton was hard-deleted somehow. Close ledger to failed
            # and audit the anomaly — target row no longer exists but the
            # audit trail should still record what happened.
            # (Low round-8: skeleton_missing writes audit.)
            conn.execute(
                "UPDATE async_remember_ledger SET terminal_state='failed', "
                "committed_at=? WHERE skeleton_id=?", (now, skeleton_id))
            _sm_audit = {c: "" for c in _AUDIT_COLUMNS}
            _sm_audit.update({
                "action": "sweep_fail", "target_id": skeleton_id,
                "decision_reason": reason + " (skeleton row missing)",
                "state_before": json.dumps({"ledger": "in_flight",
                                            "skeleton": "MISSING"},
                                           ensure_ascii=False),
                "state_after": json.dumps({"ledger": "failed",
                                           "skeleton": "MISSING"},
                                          ensure_ascii=False),
                "auto_executed": 1, "created_at": now,
            })
            _sm_vals = [_sm_audit.get(c, "") for c in _AUDIT_COLUMNS]
            _sm_ph = ", ".join(["?"] * len(_AUDIT_COLUMNS))
            conn.execute(
                f"INSERT INTO maintenance_audit "
                f"({', '.join(_AUDIT_COLUMNS)}) VALUES ({_sm_ph})",
                _sm_vals)
            # ctx auto-commits on normal exit
            return {"disposition": "skeleton_missing",
                    "transitioned": True, "skeleton_status": None}
        skel_status = skel_row[0]
        skel_link = skel_row[1] or ""

        # 3. Reconcile based on skeleton state.
        if skel_status == "pending":
            # Nothing committed. Both → failed with audit.
            _audit_defaults = {c: "" for c in _AUDIT_COLUMNS}
            _audit_defaults.update({
                "action": "sweep_fail", "target_id": skeleton_id,
                "decision_reason": reason,
                "state_before": json.dumps({"status": "pending",
                                            "ledger": "in_flight"},
                                           ensure_ascii=False),
                "state_after": json.dumps({"status": "failed",
                                           "ledger": "failed"},
                                          ensure_ascii=False),
                "auto_executed": 1, "created_at": now,
            })
            audit_vals = [_audit_defaults.get(c, "") for c in _AUDIT_COLUMNS]
            audit_ph = ", ".join(["?"] * len(_AUDIT_COLUMNS))

            conn.execute(
                "UPDATE async_remember_ledger SET terminal_state='failed', "
                "committed_at=? WHERE skeleton_id=?", (now, skeleton_id))
            conn.execute(
                "UPDATE memories SET status='failed', updated_at=?, "
                "source_platform = CASE "
                "  WHEN source_platform LIKE '%:intent_timeout' "
                "    THEN source_platform "
                "  ELSE source_platform || ':intent_timeout' END, "
                "finalize_claim_id='', finalize_claim_at='' "
                "WHERE id=? AND status='pending'",
                (now, skeleton_id))
            conn.execute(
                f"INSERT INTO maintenance_audit "
                f"({', '.join(_AUDIT_COLUMNS)}) "
                f"VALUES ({audit_ph})",
                audit_vals)
            # ctx auto-commits
            return {"disposition": "failed", "transitioned": True,
                    "skeleton_status": "pending"}

        if skel_status == "active":
            # Create pipeline reused skeleton and committed set_memory
            # but crashed before terminal ledger. Ledger catches up.
            # Low round-8: also clear stale finalize_claim in same tx.
            conn.execute(
                "UPDATE async_remember_ledger SET terminal_state='active', "
                "result_memory_id=?, committed_at=? WHERE skeleton_id=?",
                (skeleton_id, now, skeleton_id))
            conn.execute(
                "UPDATE memories SET finalize_claim_id='', "
                "finalize_claim_at='' WHERE id=?", (skeleton_id,))
            # ctx auto-commits
            return {"disposition": "already_active", "transitioned": True,
                    "skeleton_status": "active"}

        if skel_status == "replaced":
            # Merge pipeline set link_to_real_id but crashed before terminal.
            # Ledger catches up using the link.
            conn.execute(
                "UPDATE async_remember_ledger SET terminal_state='replaced', "
                "result_memory_id=?, committed_at=? WHERE skeleton_id=?",
                (skel_link, now, skeleton_id))
            conn.execute(
                "UPDATE memories SET finalize_claim_id='', "
                "finalize_claim_at='' WHERE id=?", (skeleton_id,))
            # ctx auto-commits
            return {"disposition": "already_replaced", "transitioned": True,
                    "skeleton_status": "replaced"}

        # Any other status (failed, superseded, ...) — inconsistent.
        # Medium round-8: write ONE audit row per skeleton so ops has a
        # durable signal (log alone gets rotated). Use INSERT OR IGNORE
        # keyed on (action, target_id) to prevent spam every sweep tick.
        # We identify the row by putting the skeleton_id in the audit
        # target_id and using a fixed action string.
        existing_audit = conn.execute(
            "SELECT COUNT(*) FROM maintenance_audit "
            "WHERE action = 'intent_needs_review' AND target_id = ?",
            (skeleton_id,),
        ).fetchone()
        if existing_audit and existing_audit[0] == 0:
            _nr_audit = {c: "" for c in _AUDIT_COLUMNS}
            _nr_audit.update({
                "action": "intent_needs_review", "target_id": skeleton_id,
                "decision_reason": (
                    f"{reason} — skeleton status={skel_status!r} + ledger "
                    f"in_flight; cannot auto-reconcile"),
                "state_before": json.dumps({
                    "ledger": "in_flight",
                    "skeleton": skel_status,
                }, ensure_ascii=False),
                "state_after": json.dumps({
                    "ledger": "in_flight",  # unchanged
                    "skeleton": skel_status,  # unchanged
                    "needs_manual_review": True,
                }, ensure_ascii=False),
                "auto_executed": 0, "created_at": now,
            })
            _nr_vals = [_nr_audit.get(c, "") for c in _AUDIT_COLUMNS]
            _nr_ph = ", ".join(["?"] * len(_AUDIT_COLUMNS))
            conn.execute(
                f"INSERT INTO maintenance_audit "
                f"({', '.join(_AUDIT_COLUMNS)}) VALUES ({_nr_ph})",
                _nr_vals)
        # else: already audited this skeleton — nothing new to insert; ctx
        # commits an empty (SELECT-only) tx which is a no-op.
        return {"disposition": "needs_review", "transitioned": False,
                "skeleton_status": skel_status}


def get_ledger(skeleton_id: str) -> dict | None:
    """H2: look up the ledger entry for a skeleton. Returns None if the
    pipeline hasn't committed a terminal state yet."""
    if not skeleton_id:
        return None
    conn = _get_read_conn()
    row = conn.execute(
        "SELECT skeleton_id, client_request_id, terminal_state, "
        "       result_memory_id, committed_at "
        "FROM async_remember_ledger WHERE skeleton_id = ?",
        (skeleton_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "skeleton_id": row[0], "client_request_id": row[1],
        "terminal_state": row[2], "result_memory_id": row[3],
        "committed_at": row[4],
    }


def try_claim_finalize(skeleton_id: str, claim_token: str,
                       stale_after_minutes: int = 30) -> bool:
    """H1: atomically claim a pending skeleton for finalize. Returns True
    if this caller won the claim, False if another sweep/finalize already
    holds it. Atomic single-row UPDATE with rowcount check.

    A claim is takeable if:
      - finalize_claim_id is empty (never claimed), OR
      - the previous claim is older than stale_after_minutes (crashed
        holder — take over).
    """
    if not skeleton_id or not claim_token:
        return False
    now = _now_iso()
    cutoff = (datetime.now(timezone.utc)
              - timedelta(minutes=stale_after_minutes)).isoformat()
    with _write_transaction() as conn:
        cur = conn.execute(
            "UPDATE memories SET finalize_claim_id = ?, finalize_claim_at = ? "
            "WHERE id = ? AND status = 'pending' "
            "  AND (finalize_claim_id = '' OR finalize_claim_at < ?)",
            (claim_token, now, skeleton_id, cutoff),
        )
        rowcount = cur.rowcount
    return rowcount == 1


def release_finalize_claim(skeleton_id: str) -> None:
    """H1: release a claim after finalize completes (successful or terminal
    failure). Called from commit_finalize_atomic in the same transaction.
    Standalone helper for the rare non-terminal cleanup paths."""
    with _write_transaction() as conn:
        conn.execute(
            "UPDATE memories SET finalize_claim_id = '', finalize_claim_at = '' "
            "WHERE id = ?", (skeleton_id,))


def commit_finalize_atomic(
    skeleton_id: str,
    client_request_id: str,
    terminal_state: str,
    result_memory_id: str,
    skeleton_update: dict | None = None,
    owner_token: str = "",
) -> bool:
    """H2 + H4 + H1 round-6: atomically write the ledger entry AND
    transition the skeleton row. Both go in one BEGIN IMMEDIATE — if either
    fails, both roll back so sweep never sees a half-completed pipeline.

    owner_token: MUST match the token this caller passed to
    write_intent_ledger. If it doesn't match the ledger's current owner
    (another worker took over via stale-intent takeover, or shouldn't
    happen), the ledger UPDATE affects 0 rows and this function returns
    False WITHOUT touching the skeleton row. Caller should read the
    winner's terminal via get_ledger() and apply that instead.

    Returns True if this caller successfully committed the terminal state
    (and the skeleton was updated), False if another owner won.
    """
    if terminal_state not in ("active", "replaced", "failed"):
        raise ValueError(f"invalid terminal_state: {terminal_state}")

    now = _now_iso()
    with _write_transaction() as conn:
        # H2 + H1 round-6: race-losing terminal writer cannot clobber
        # winner. Two constraints on the UPDATE branch:
        #   - terminal_state must still be 'in_flight' (nobody terminaled yet)
        #   - owner_token must match ours (we're the intent holder)
        # If either fails, ledger stays as-is and skeleton is NOT touched.
        cur = conn.execute(
            "INSERT INTO async_remember_ledger "
            "(skeleton_id, client_request_id, terminal_state, "
            " result_memory_id, committed_at, owner_token) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(skeleton_id) DO UPDATE SET "
            "terminal_state=excluded.terminal_state, "
            "result_memory_id=excluded.result_memory_id, "
            "committed_at=excluded.committed_at "
            "WHERE async_remember_ledger.terminal_state = 'in_flight' "
            "  AND (async_remember_ledger.owner_token = excluded.owner_token "
            "       OR async_remember_ledger.owner_token = '')",
            (skeleton_id, client_request_id, terminal_state,
             result_memory_id, now, owner_token),
        )
        # H1 round-6: if the ledger UPDATE did not affect our row (someone
        # else already terminaled or owns a different token), do NOT touch
        # the skeleton. Return False; ctx commits (no-op — no rows changed).
        if cur.rowcount != 1:
            return False

        if skeleton_update:
            # Build UPDATE dynamically for the requested fields.
            # H1: also release finalize_claim so a future sweep can retry
            # if this row somehow ends up back in pending.
            set_clauses = [
                "updated_at = ?",
                "finalize_claim_id = ''",
                "finalize_claim_at = ''",
            ]
            params: list = [now]
            for key, val in skeleton_update.items():
                if key == "source_platform_suffix":
                    # Special: append-if-not-present pattern
                    suffix = val if val.startswith(":") else ":" + val
                    set_clauses.append(
                        "source_platform = CASE "
                        "  WHEN source_platform LIKE '%' || ? THEN source_platform "
                        "  ELSE source_platform || ? END"
                    )
                    params.extend([suffix, suffix])
                else:
                    set_clauses.append(f"{key} = ?")
                    params.append(val)
            params.append(skeleton_id)
            conn.execute(
                f"UPDATE memories SET {', '.join(set_clauses)} WHERE id = ?",
                params,
            )
        # ctx auto-commits
        return True


class MaintenanceDrift(RuntimeError):
    """Raised when commit_maintenance_atomic finds the target row has drifted
    (status or updated_at changed since the caller captured the snapshot).
    Transaction is rolled back; no audit is written; caller should skip."""


def commit_maintenance_atomic(
    memory_id: str,
    memory_updates: dict,
    audit_row: dict,
    expected_status: str | None = None,
    expected_updated_at: str | None = None,
    extra_expected_rows: list[dict] | None = None,
) -> None:
    """H4: apply a maintenance action and write the audit row in ONE
    BEGIN IMMEDIATE transaction. If either fails, both roll back so the
    target memory can't be left in a modified state without an audit trail.

    H3 round-4: expected_status / expected_updated_at gate the UPDATE inside
    the transaction. If either doesn't match at UPDATE time (concurrent
    writer changed the row between plan and execute), rowcount==0 and we
    raise MaintenanceDrift so the whole tx rolls back with NO audit written.

    memory_updates: fields to UPDATE on the memory row. `comments` and
    `history` are serialized to JSON if list/dict.
    audit_row: dict with keys matching _AUDIT_COLUMNS.
    """
    now = _now_iso()

    # Build memory UPDATE
    set_clauses = ["updated_at = ?"]
    params: list = [now]
    for key, val in memory_updates.items():
        if key in ("comments", "history") and isinstance(val, (list, dict)):
            val = json.dumps(val, ensure_ascii=False)
        set_clauses.append(f"{key} = ?")
        params.append(val)

    where_clauses = ["id = ?"]
    where_params: list = [memory_id]
    if expected_status is not None:
        where_clauses.append("status = ?")
        where_params.append(expected_status)
    if expected_updated_at is not None:
        where_clauses.append("updated_at = ?")
        where_params.append(expected_updated_at)

    # Ensure required audit columns are present
    audit_defaults = {c: "" for c in _AUDIT_COLUMNS}
    audit_defaults["auto_executed"] = 1
    audit_defaults["created_at"] = now
    audit_defaults.update(audit_row)
    audit_values = [audit_defaults.get(c, "") for c in _AUDIT_COLUMNS]
    audit_placeholders = ", ".join(["?"] * len(_AUDIT_COLUMNS))
    audit_cols_str = ", ".join(_AUDIT_COLUMNS)

    with _write_transaction() as conn:
        # H3 round-6: validate ALL rows the decision depends on — not just
        # the target A being mutated. dedup plans use A+B: if B drifted
        # between plan generation and execute, the mutation of A based on
        # B's old content is stale and must not commit.
        if extra_expected_rows:
            for expect in extra_expected_rows:
                exp_id = expect.get("id")
                if not exp_id:
                    raise ValueError(
                        "extra_expected_rows entry missing 'id'")
                sel = conn.execute(
                    "SELECT status, updated_at FROM memories WHERE id = ?",
                    (exp_id,),
                ).fetchone()
                if not sel:
                    raise MaintenanceDrift(
                        f"companion row {exp_id} not found")
                cur_status, cur_updated = sel[0], sel[1]
                if ("status" in expect
                        and expect["status"] != cur_status):
                    raise MaintenanceDrift(
                        f"companion {exp_id} drifted: expected "
                        f"status={expect['status']!r}, got {cur_status!r}")
                if ("updated_at" in expect
                        and expect["updated_at"] != cur_updated):
                    raise MaintenanceDrift(
                        f"companion {exp_id} drifted: expected "
                        f"updated_at={expect['updated_at']!r}, "
                        f"got {cur_updated!r}")

        cur = conn.execute(
            f"UPDATE memories SET {', '.join(set_clauses)} "
            f"WHERE {' AND '.join(where_clauses)}",
            params + where_params,
        )
        if cur.rowcount != 1:
            # Drift detected — someone modified the row between snapshot
            # and now. Raise → ctx ROLLBACKs the UPDATE (no-op if nothing
            # matched) and skips the audit INSERT.
            raise MaintenanceDrift(
                f"target {memory_id} drifted "
                f"(expected status={expected_status!r}, "
                f"updated_at={expected_updated_at!r}); rowcount={cur.rowcount}"
            )
        conn.execute(
            f"INSERT INTO maintenance_audit ({audit_cols_str}) "
            f"VALUES ({audit_placeholders})",
            audit_values,
        )
        # ctx auto-commits


def list_memories_by_status(status: str, older_than_minutes: int = 0,
                             limit: int = 500) -> list[dict]:
    """Return memories in a specific status, optionally older than N minutes.
    Used by the pending sweep to find stuck skeletons."""
    conn = _get_read_conn()
    if older_than_minutes > 0:
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(minutes=older_than_minutes)).isoformat()
        rows = conn.execute(
            "SELECT * FROM memories WHERE status = ? AND created_at <= ? "
            "ORDER BY created_at LIMIT ?",
            (status, cutoff, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM memories WHERE status = ? ORDER BY created_at LIMIT ?",
            (status, limit),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _prepare_memory_value(key: str, value):
    """Memory-field value preparation for INSERT/UPSERT.

    Phase 2.0 Step 0-A: 从 set_memory._prep 原样抽出，供
    _set_memory_in_tx / _payload_fingerprint (Step 0-B) 共用同一份规范化
    规则，避免"写入时补 ''、fingerprint 时保 None"造成的漂移。

    5 条既有语义严格保留（v2.9 H1 收敛）：
      - resolved/anchored → _resolved_to_int
      - comments/history → JSON dumps；None → "[]"
      - embedding 原样（bytes/None）
      - fact_confidence 原样（保留 None 表"未知置信度"，不改成 0.0）
      - 其他 None → ""
    state_ttl_days 的默认 7 分支等 Step 0-B（列还没进 _ALL_COLUMNS）。
    """
    if key in ("resolved", "anchored"):
        return _resolved_to_int(value)
    if key in ("comments", "history"):
        if isinstance(value, (list, dict)):
            return json.dumps(value, ensure_ascii=False)
        if value is None:
            return "[]"
        return value
    if key == "embedding":
        return value  # bytes or None
    if key == "fact_confidence":
        return value  # REAL or None
    if value is None:
        return ""
    return value


def _set_memory_in_tx(conn: sqlite3.Connection, mem: dict) -> None:
    """UPSERT `mem` into memories + maintain vec_id_map + memories_vec, on an
    already-open tx (does NOT acquire _WRITE_LOCK, does NOT commit).

    Phase 2.0 Step 0-A #3: extracted from set_memory() with **zero behavioural
    changes** so future callers already inside `_write_transaction()` (e.g.
    the state supersede helper coming in Step 0-B) can reuse the full upsert
    logic without nesting `_write_transaction()` (which would RuntimeError
    on non-reentrant lock).

    Preserved semantics from set_memory:
      - `_prepare_memory_value` for column value coercion
      - `_preserve_on_empty` = {client_request_id, link_to_real_id,
        finalize_claim_id, finalize_claim_at} — CASE-guarded UPSERT so empty
        excluded values never clobber a valid stored value
      - `_preserve_always` = {created_at} — kept unless memories.created_at is
        currently empty (first insert only)
      - embedding via COALESCE — never overwrite a non-null stored embedding
        with NULL from a caller that didn't compute vectors
      - vec_id_map + memories_vec kept in sync (insert/update/delete based on
        whether a valid EMBEDDING_DIM*4-sized bytes is present)

    Failure handling: on sqlite3.Error from the main UPSERT, logs + re-raises
    (caller's tx will roll back). Vec-index writes swallow errors after
    logging.warning to preserve the original best-effort semantics.
    """
    values = [_prepare_memory_value(col, mem.get(col)) for col in _ALL_COLUMNS]
    placeholders = ", ".join(["?"] * len(_ALL_COLUMNS))
    cols = ", ".join(_ALL_COLUMNS)
    # embedding 用 COALESCE：写入方（内存 store / GitHub 快照）经常没有向量，
    # 不能让 None 覆盖掉库里已有的 embedding——否则任何 activation 更新
    # 都会把离线补好的向量冲掉（2026-07-18：382 条向量被这样冲没过）。
    # PR C 块 8: client_request_id / link_to_real_id 同理——非 MCP 路径的
    # set_memory 调用（activation touch、comment 追加等）不能把 '' 覆盖到
    # 已存在的 crq/link，否则骨架幂等追踪失效。
    # created_at 永远保留 DB 中已有值——pipeline 完成时 UPSERT 会传入新
    # created_at，会破坏骨架的 recency（一条 10 分钟前入 pending 的记忆变成
    # "刚刚创建"，破 recency boost、破 sweep 判 age）。
    # M1 round-6: finalize_claim_id/at are runtime coordination columns —
    # generic UPSERT (activation touch, comment append) must NEVER clobber
    # them. Callers write these ONLY via try_claim_finalize /
    # release_finalize_claim / commit_finalize_atomic. If the caller-supplied
    # dict lacks the field, keep the DB's current value.
    _preserve_on_empty = {"client_request_id", "link_to_real_id",
                          "finalize_claim_id", "finalize_claim_at"}
    _preserve_always = {"created_at"}
    update_set_parts = []
    for c in _ALL_COLUMNS:
        if c == "id":
            continue
        if c == "embedding":
            update_set_parts.append(f"{c} = COALESCE(excluded.{c}, memories.{c})")
        elif c in _preserve_always:
            # 只在 memories.{c} 为空时才用 excluded.{c}（首次插入）
            update_set_parts.append(
                f"{c} = CASE WHEN memories.{c} != '' THEN memories.{c} "
                f"ELSE excluded.{c} END"
            )
        elif c in _preserve_on_empty:
            # 只在 excluded 值非空时覆盖，为空则保留已有值
            update_set_parts.append(
                f"{c} = CASE WHEN excluded.{c} != '' THEN excluded.{c} "
                f"ELSE memories.{c} END"
            )
        else:
            update_set_parts.append(f"{c} = excluded.{c}")
    update_set = ", ".join(update_set_parts)

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


def set_memory(mem: dict) -> None:
    """Insert or replace a memory (upsert).

    Also maintains the vec_id_map and memories_vec tables for vector search.
    FTS is handled automatically by triggers.

    Public wrapper: opens a `_write_transaction()` and delegates to the
    in-tx helper `_set_memory_in_tx`. Callers already inside a write tx
    (e.g. state supersede in Step 0-B) MUST call `_set_memory_in_tx`
    directly — invoking `set_memory` would hit the non-reentrant lock's
    RuntimeError guard.
    """
    with _write_transaction() as conn:
        _set_memory_in_tx(conn, mem)


def remove_memory(mem_id: str) -> None:
    """Delete a memory and its FTS/vec entries.

    FTS cleanup is handled by the DELETE trigger. Vec cleanup is explicit.
    """
    with _write_transaction() as conn:
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
    conn = _get_read_conn()
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
    "subject_id", "source_actor_id",
    "info_type", "maintenance_action", "maintenance_target_id",
]


def insert_proposal(row: dict) -> None:
    values = [row.get(c, "") for c in _PROPOSAL_COLUMNS]
    placeholders = ", ".join(["?"] * len(_PROPOSAL_COLUMNS))
    cols = ", ".join(_PROPOSAL_COLUMNS)
    with _write_transaction() as conn:
        conn.execute(f"INSERT INTO proposals ({cols}) VALUES ({placeholders})", values)


def get_proposal(pid: str) -> dict | None:
    conn = _get_read_conn()
    row = conn.execute("SELECT * FROM proposals WHERE id = ?", (pid,)).fetchone()
    return dict(row) if row else None


def list_proposals(
    status: str = "pending", limit: int = 50, offset: int = 0,
) -> list[dict]:
    conn = _get_read_conn()
    rows = conn.execute(
        "SELECT * FROM proposals WHERE status = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (status, limit, offset),
    ).fetchall()
    return [dict(r) for r in rows]


def update_proposal_status(
    pid: str, status: str, reviewed_by: str = "", reject_reason: str = "",
    applied_memory_id: str = "", failure_reason: str = "",
) -> None:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with _write_transaction() as conn:
        conn.execute(
            "UPDATE proposals SET status = ?, reviewed_at = ?, reviewed_by = ?, "
            "reject_reason = ?, applied_memory_id = ?, failure_reason = ? WHERE id = ?",
            (status, now, reviewed_by, reject_reason, applied_memory_id, failure_reason, pid),
        )


def count_proposals(status: str = "pending") -> int:
    conn = _get_read_conn()
    row = conn.execute(
        "SELECT COUNT(*) FROM proposals WHERE status = ?", (status,)
    ).fetchone()
    return row[0] if row else 0


# ════════════════════════════════════════════
#  Maintenance Audit CRUD
# ════════════════════════════════════════════

_AUDIT_COLUMNS = [
    "action", "target_id", "new_content", "source_message_ids",
    "decision_reason", "state_before", "state_after",
    "model_id", "source_ai", "auto_executed", "prompt_version", "created_at",
]


def insert_audit(row: dict) -> int:
    values = [row.get(c, "") for c in _AUDIT_COLUMNS]
    placeholders = ", ".join(["?"] * len(_AUDIT_COLUMNS))
    cols = ", ".join(_AUDIT_COLUMNS)
    with _write_transaction() as conn:
        cur = conn.execute(f"INSERT INTO maintenance_audit ({cols}) VALUES ({placeholders})", values)
        lastrowid = cur.lastrowid
    return lastrowid


def list_audits(action: str = None, limit: int = 50, offset: int = 0) -> list[dict]:
    conn = _get_read_conn()
    if action:
        rows = conn.execute(
            "SELECT * FROM maintenance_audit WHERE action = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (action, limit, offset),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM maintenance_audit ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    return [dict(r) for r in rows]


def count_audits(action: str = None) -> int:
    conn = _get_read_conn()
    if action:
        row = conn.execute("SELECT COUNT(*) FROM maintenance_audit WHERE action = ?", (action,)).fetchone()
    else:
        row = conn.execute("SELECT COUNT(*) FROM maintenance_audit").fetchone()
    return row[0] if row else 0


# ════════════════════════════════════════════
#  Profiles CRUD
# ════════════════════════════════════════════

def upsert_profile(profile: dict) -> None:
    pid = profile["id"]
    status = profile.get("status", "pending_review")
    with _write_transaction() as conn:
        existing = conn.execute("SELECT version FROM profiles WHERE id = ?", (pid,)).fetchone()
        if existing:
            new_version = existing[0] + 1
            conn.execute("""
                UPDATE profiles SET content = ?, generated_at = ?, source_memory_ids = ?,
                version = ?, status = ?
                WHERE id = ?
            """, (profile["content"], profile["generated_at"], profile.get("source_memory_ids", "[]"),
                  new_version, status, pid))
        else:
            conn.execute("""
                INSERT INTO profiles (id, profile_type, owner_ai, content, generated_at,
                source_memory_ids, version, status)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?)
            """, (pid, profile["profile_type"], profile.get("owner_ai", ""),
                  profile["content"], profile["generated_at"],
                  profile.get("source_memory_ids", "[]"), status))


def approve_profile(profile_id: str) -> bool:
    with _write_transaction() as conn:
        cur = conn.execute(
            "UPDATE profiles SET status = 'active' WHERE id = ? AND status = 'pending_review'",
            (profile_id,))
        rowcount = cur.rowcount
    return rowcount > 0


def supersede_profile(profile_id: str) -> bool:
    with _write_transaction() as conn:
        cur = conn.execute(
            "UPDATE profiles SET status = 'superseded' WHERE id = ? AND status != 'superseded'",
            (profile_id,))
        rowcount = cur.rowcount
    return rowcount > 0


def get_profile(profile_id: str, status: str = None) -> dict | None:
    conn = _get_read_conn()
    if status:
        row = conn.execute("SELECT * FROM profiles WHERE id = ? AND status = ?",
                           (profile_id, status)).fetchone()
    else:
        row = conn.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)).fetchone()
    return dict(row) if row else None


def list_profiles(profile_type: str = None, status: str = None) -> list[dict]:
    conn = _get_read_conn()
    conditions = []
    params = []
    if profile_type:
        conditions.append("profile_type = ?")
        params.append(profile_type)
    if status:
        conditions.append("status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    rows = conn.execute(
        f"SELECT * FROM profiles {where} ORDER BY profile_type, id", params
    ).fetchall()
    return [dict(r) for r in rows]


def delete_profile(profile_id: str) -> bool:
    with _write_transaction() as conn:
        cur = conn.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
        rowcount = cur.rowcount
    return rowcount > 0


# ════════════════════════════════════════════
#  Vector search
# ════════════════════════════════════════════

def _build_vec_mem_sql(
    status, room, layer, owner_ai, exclude_provenance,
    exclude_resolved, exclude_superseded,
):
    """Build parameterized WHERE clause for vector search memory lookup."""
    parts = []
    params: list = []
    if status is not None:
        parts.append("status = ?")
        params.append(status)
    if room is not None:
        parts.append("room = ?")
        params.append(room)
    if layer is not None:
        parts.append("layer = ?")
        params.append(layer)
    if owner_ai is not None:
        parts.append("owner_ai = ?")
        params.append(owner_ai)
    if exclude_provenance:
        ph = ",".join("?" * len(exclude_provenance))
        parts.append(f"(provenance_type IS NULL OR provenance_type NOT IN ({ph}))")
        params.extend(exclude_provenance)
    if exclude_resolved:
        parts.append("(resolved IS NULL OR resolved != 1)")
    if exclude_superseded:
        parts.append("(superseded_by IS NULL OR superseded_by = '')")

    where = ""
    if parts:
        where = "AND " + " AND ".join(parts)
    return where, params


def vector_search(
    query_vec: list[float],
    top_k: int = 50,
    status: str = "active",
    room: str = None,
    include_rooms: list[str] = None,
    exclude_rooms: list[str] = None,
    layer: str = None,
    owner_ai: str = None,
    exclude_provenance: list[str] = None,
    exclude_resolved: bool = False,
    exclude_superseded: bool = False,
) -> list[dict]:
    """Vector similarity search using sqlite-vec.

    Retrieves candidates from the vec index in expanding batches, filters
    by the requested criteria at the SQL layer, returning up to ``top_k``
    results with a ``distance`` field (lower is more similar).
    """
    conn = _get_read_conn()
    return _vector_search_impl(
        conn, query_vec, top_k, status, room, include_rooms, exclude_rooms,
        layer, owner_ai, exclude_provenance, exclude_resolved, exclude_superseded,
    )


def _vector_search_impl(
    conn, query_vec, top_k, status, room, include_rooms, exclude_rooms,
    layer, owner_ai, exclude_provenance, exclude_resolved, exclude_superseded,
):
    if len(query_vec) != EMBEDDING_DIM:
        raise ValueError(f"Expected {EMBEDDING_DIM}-dim vector, got {len(query_vec)}")

    query_blob = struct.pack(f"{EMBEDDING_DIM}f", *query_vec)

    mem_where, mem_params = _build_vec_mem_sql(
        status, room, layer, owner_ai, exclude_provenance,
        exclude_resolved, exclude_superseded,
    )

    # Adaptive expansion: double fetch limit until we fill top_k or exhaust index.
    # max_fetch is derived from actual index size so we never silently give up
    # while the index still has candidates left to try.
    try:
        index_size = conn.execute("SELECT COUNT(*) FROM memories_vec").fetchone()[0]
    except sqlite3.Error:
        index_size = 0
    max_fetch = max(index_size, top_k * 4)

    fetch_limit = min(top_k * 4, max_fetch) if max_fetch else top_k * 4
    results = []
    seen_rowids: set = set()
    prev_fetch = 0

    while len(results) < top_k and fetch_limit <= max_fetch:
        try:
            vec_rows = conn.execute(
                "SELECT rowid, distance FROM memories_vec "
                "WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
                (query_blob, fetch_limit),
            ).fetchall()
        except sqlite3.Error:
            logger.exception("Vector search failed")
            return results

        if not vec_rows or len(vec_rows) == prev_fetch:
            break
        prev_fetch = len(vec_rows)

        new_rows = [(r[0], r[1]) for r in vec_rows if r[0] not in seen_rowids]
        if not new_rows:
            # Reached natural end of index — no new candidates possible.
            break
        for r in new_rows:
            seen_rowids.add(r[0])

        new_rowids = [r[0] for r in new_rows]
        distances = {r[0]: r[1] for r in new_rows}

        for i in range(0, len(new_rowids), 500):
            batch = new_rowids[i:i + 500]
            placeholders = ", ".join(["?"] * len(batch))
            map_rows = conn.execute(
                f"SELECT vec_rowid, memory_id FROM vec_id_map "
                f"WHERE vec_rowid IN ({placeholders})",
                batch,
            ).fetchall()

            mem_ids = {r[0]: r[1] for r in map_rows}

            for vec_rid in batch:
                mem_id = mem_ids.get(vec_rid)
                if not mem_id:
                    continue

                row = conn.execute(
                    f"SELECT * FROM memories WHERE id = ? {mem_where}",
                    [mem_id] + mem_params,
                ).fetchone()
                if row is None:
                    continue

                mem = _row_to_dict_no_embedding(row)

                if include_rooms and mem.get("room") not in include_rooms:
                    continue
                if exclude_rooms and mem.get("room") in exclude_rooms:
                    continue

                mem["distance"] = distances[vec_rid]
                results.append(mem)

                if len(results) >= top_k:
                    return results

        if fetch_limit >= max_fetch:
            break
        fetch_limit = min(fetch_limit * 2, max_fetch)

    return results


# ════════════════════════════════════════════
#  Full-text search
# ════════════════════════════════════════════

def fts_search(query: str, top_k: int = 50, status: str = "active",
               exclude_provenance: list[str] = None,
               exclude_resolved: bool = False,
               exclude_superseded: bool = False,
               include_rooms: list[str] = None,
               exclude_rooms: list[str] = None) -> list[dict]:
    """Full-text search using FTS5.

    Returns memories matching the query with a ``rank`` field
    (more negative = better match in FTS5 bm25 scoring).
    """
    conn = _get_read_conn()
    return _fts_search_impl(conn, query, top_k, status, exclude_provenance,
                            exclude_resolved, exclude_superseded,
                            include_rooms, exclude_rooms)


def _fts_search_impl(conn, query, top_k, status, exclude_provenance,
                     exclude_resolved, exclude_superseded,
                     include_rooms=None, exclude_rooms=None):
    if not query or not query.strip():
        return []

    safe_query = _fts_escape(query)

    extra_where = ""
    extra_params: list = []
    if exclude_provenance:
        ph = ",".join("?" * len(exclude_provenance))
        extra_where += f" AND (m.provenance_type IS NULL OR m.provenance_type NOT IN ({ph}))"
        extra_params.extend(exclude_provenance)
    if exclude_resolved:
        extra_where += " AND (m.resolved IS NULL OR m.resolved != 1)"
    if exclude_superseded:
        extra_where += " AND (m.superseded_by IS NULL OR m.superseded_by = '')"
    if include_rooms:
        rph = ",".join("?" * len(include_rooms))
        extra_where += f" AND m.room IN ({rph})"
        extra_params.extend(include_rooms)
    if exclude_rooms:
        rph = ",".join("?" * len(exclude_rooms))
        extra_where += f" AND m.room NOT IN ({rph})"
        extra_params.extend(exclude_rooms)

    sql_tpl = (
        "SELECT m.*, fts.rank "
        "FROM memories_fts fts "
        "JOIN memories m ON m.rowid = fts.rowid "
        "WHERE memories_fts MATCH ? "
        "AND m.status = ? "
        f"{extra_where} "
        "ORDER BY fts.rank "
        "LIMIT ?"
    )

    try:
        rows = conn.execute(sql_tpl, (safe_query, status, *extra_params, top_k)).fetchall()
    except sqlite3.OperationalError:
        logger.warning(f"FTS5 query failed for: {query!r}, trying fallback")
        try:
            tokens = query.strip().split()
            fallback = " OR ".join(f'"{_fts_escape_token(t)}"' for t in tokens if t)
            if not fallback:
                return []
            rows = conn.execute(sql_tpl, (fallback, status, *extra_params, top_k)).fetchall()
        except sqlite3.OperationalError:
            logger.exception("FTS5 fallback also failed")
            return []

    results = []
    for row in rows:
        mem = _row_to_dict_no_embedding(row)
        mem["rank"] = row["rank"]
        results.append(mem)

    return results


_CJK_RUN_RE = re.compile(r"[㐀-䶿一-鿿]{2,}")


def _cjk_like_search_impl(conn, query, top_k, status, exclude_provenance,
                          exclude_resolved, exclude_superseded,
                          include_rooms, exclude_rooms):
    """Shared CJK LIKE search — used by both cjk_like_search and ro_cjk_like_search."""
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

    conds = " OR ".join(["content LIKE ?"] * len(grams))
    extra_clause = ""
    extra_params: list = []
    if exclude_provenance:
        ph = ",".join("?" * len(exclude_provenance))
        extra_clause += f" AND (provenance_type IS NULL OR provenance_type NOT IN ({ph}))"
        extra_params.extend(exclude_provenance)
    if exclude_resolved:
        extra_clause += " AND (resolved IS NULL OR resolved != 1)"
    if exclude_superseded:
        extra_clause += " AND (superseded_by IS NULL OR superseded_by = '')"
    if include_rooms:
        rph = ",".join("?" * len(include_rooms))
        extra_clause += f" AND room IN ({rph})"
        extra_params.extend(include_rooms)
    if exclude_rooms:
        rph = ",".join("?" * len(exclude_rooms))
        extra_clause += f" AND room NOT IN ({rph})"
        extra_params.extend(exclude_rooms)
    try:
        rows = conn.execute(
            f"SELECT * FROM memories WHERE status = ? AND ({conds}){extra_clause} LIMIT 400",
            (status, *[f"%{g}%" for g in grams], *extra_params),
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


def cjk_like_search(query: str, top_k: int = 50, status: str = "active",
                    exclude_provenance: list[str] = None,
                    exclude_resolved: bool = False,
                    exclude_superseded: bool = False,
                    include_rooms: list[str] = None,
                    exclude_rooms: list[str] = None) -> list[dict]:
    """中文子串搜索（LIKE 路）。委托到共享 impl。"""
    return _cjk_like_search_impl(
        _get_read_conn(), query, top_k, status, exclude_provenance,
        exclude_resolved, exclude_superseded, include_rooms, exclude_rooms,
    )


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
    conn = _get_read_conn()
    rows = conn.execute(
        "SELECT id FROM memories WHERE status = 'active'"
    ).fetchall()
    return [r[0] for r in rows]


def get_memories_batch(ids: list[str]) -> list[dict]:
    """Get multiple memories by ID. Returns dicts without embedding."""
    if not ids:
        return []
    conn = _get_read_conn()

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
    """线程安全版 vector_search — delegates to shared impl with read-only conn."""
    conn = _get_read_conn()
    try:
        return _vector_search_impl(
            conn, query_vec, top_k, status, None, include_rooms, exclude_rooms,
            None, None, kwargs.get("exclude_provenance"),
            kwargs.get("exclude_resolved", False),
            kwargs.get("exclude_superseded", False),
        )
    except Exception as e:
        logger.warning(f"ro_vector_search failed: {e}")
        return []


def ro_fts_search(query: str, top_k: int = 50, status: str = "active",
                  exclude_provenance: list[str] = None,
                  exclude_resolved: bool = False,
                  exclude_superseded: bool = False,
                  include_rooms: list[str] = None,
                  exclude_rooms: list[str] = None) -> list[dict]:
    """线程安全版 fts_search — delegates to shared impl with read-only conn."""
    conn = _get_read_conn()
    try:
        return _fts_search_impl(conn, query, top_k, status, exclude_provenance,
                                exclude_resolved, exclude_superseded,
                                include_rooms, exclude_rooms)
    except Exception as e:
        logger.warning(f"ro_fts_search failed: {e}")
        return []


def ro_cjk_like_search(query: str, top_k: int = 50, status: str = "active",
                       exclude_provenance: list[str] = None,
                       exclude_resolved: bool = False,
                       exclude_superseded: bool = False,
                       include_rooms: list[str] = None,
                       exclude_rooms: list[str] = None) -> list[dict]:
    """线程安全版 cjk_like_search — delegates to shared impl with read-only conn."""
    try:
        return _cjk_like_search_impl(
            _get_read_conn(), query, top_k, status, exclude_provenance,
            exclude_resolved, exclude_superseded, include_rooms, exclude_rooms,
        )
    except Exception as e:
        logger.warning(f"ro_cjk_like_search failed: {e}")
        return []


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
    aliases = person.get("aliases", [])
    if isinstance(aliases, list):
        aliases = json.dumps(aliases, ensure_ascii=False)
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with _write_transaction() as conn:
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


def get_person(person_id: str) -> dict | None:
    conn = _get_read_conn()
    row = conn.execute(
        "SELECT * FROM persons WHERE person_id = ?", (person_id,)
    ).fetchone()
    return _person_row_to_dict(row) if row else None


def list_persons(entity_type: str = None) -> list[dict]:
    conn = _get_read_conn()
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


def get_memories_by_subject(person_id: str, limit: int = 50, offset: int = 0) -> list[dict]:
    conn = _get_read_conn()
    rows = conn.execute(
        "SELECT * FROM memories WHERE subject_id = ? AND status = 'active' "
        "ORDER BY updated_at DESC LIMIT ? OFFSET ?",
        (person_id, limit, offset),
    ).fetchall()
    return [_row_to_dict_no_embedding(r) for r in rows]


def count_memories_by_subject(person_id: str) -> int:
    conn = _get_read_conn()
    row = conn.execute(
        "SELECT COUNT(*) FROM memories WHERE subject_id = ? AND status = 'active'",
        (person_id,),
    ).fetchone()
    return row[0] if row else 0


def delete_person(person_id: str) -> bool:
    with _write_transaction() as conn:
        cur = conn.execute("DELETE FROM persons WHERE person_id = ?", (person_id,))
        rowcount = cur.rowcount
    return rowcount > 0


def resolve_alias(name: str, scope: str = "household") -> str | None:
    """根据别名找到 person_id。先精确匹配 canonical_name，再搜 aliases JSON。"""
    conn = _get_read_conn()
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
    """启动时种入基线人物（如果 persons 表为空）。返回真实新增行数。

    Codex #2b M1: COUNT 与 INSERT 必须同一事务，否则两线程同时读到 0 会
    双重返 len(baseline)（数据靠 INSERT OR IGNORE 不重复，但返回值不真实，
    且 seed 是写决策不能走独立只读连接——WAL 快照可能是旧的）。
    """
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

    inserted = 0
    with _write_transaction() as conn:
        # v2b M1: COUNT 在事务内查（BEGIN IMMEDIATE 已锁）→ 决策原子
        count = conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0]
        if count > 0:
            return 0
        for p in baseline:
            cur = conn.execute(
                "INSERT OR IGNORE INTO persons "
                "(person_id, entity_type, canonical_name, aliases, linked_agent_id, note, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (p["person_id"], p["entity_type"], p["canonical_name"],
                 p["aliases"], p["linked_agent_id"], p["note"], now, now),
            )
            inserted += cur.rowcount
    return inserted


def get_all_aliases(scope: str = "household") -> dict[str, str]:
    """返回 {别名: person_id} 映射表，用于批量归一。"""
    conn = _get_read_conn()
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
