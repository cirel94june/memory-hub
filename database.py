"""
数据库：支持 Supabase PostgreSQL（线上）和 SQLite（本地开发）
通过环境变量 DATABASE_URL 切换：
  - 有 DATABASE_URL → 用 PostgreSQL
  - 没有 → 用本地 SQLite
"""
import os
import json
from contextlib import asynccontextmanager

# ── 判断使用哪种数据库 ──
DATABASE_URL = os.getenv("DATABASE_URL", "")
USE_POSTGRES = DATABASE_URL.startswith("postgres")

if USE_POSTGRES:
    # 线上模式：Supabase PostgreSQL
    # 需要额外安装 asyncpg: pip install asyncpg
    print("[DB] Using PostgreSQL (Supabase)")
else:
    # 本地模式：SQLite
    import aiosqlite
    from config import DB_PATH
    print(f"[DB] Using SQLite at {DB_PATH}")


# ── 建表 SQL（兼容两种数据库） ──

POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    layer TEXT NOT NULL DEFAULT 'shared',
    room TEXT NOT NULL DEFAULT 'living_room',
    category TEXT DEFAULT '',
    owner_ai TEXT DEFAULT '',
    importance REAL DEFAULT 0.5,
    emotion_arousal REAL DEFAULT 0.3,
    decay_score REAL DEFAULT 1.0,
    activation_count INTEGER DEFAULT 0,
    last_activated TEXT DEFAULT '',
    source_ai TEXT DEFAULT '',
    source_platform TEXT DEFAULT '',
    tags TEXT DEFAULT '[]',
    linked_memories TEXT DEFAULT '[]',
    embedding BYTEA,
    status TEXT DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_history (
    id SERIAL PRIMARY KEY,
    memory_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    content TEXT NOT NULL,
    changed_at TEXT NOT NULL,
    changed_by TEXT DEFAULT '',
    FOREIGN KEY (memory_id) REFERENCES memories(id)
);

CREATE TABLE IF NOT EXISTS social_posts (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL DEFAULT 'moment',
    author TEXT NOT NULL,
    content TEXT NOT NULL,
    media TEXT DEFAULT '[]',
    reactions TEXT DEFAULT '{}',
    comments TEXT DEFAULT '[]',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS group_chat (
    id SERIAL PRIMARY KEY,
    room_id TEXT NOT NULL DEFAULT 'main',
    sender TEXT NOT NULL,
    content TEXT NOT NULL,
    reply_to INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daemon_logs (
    id SERIAL PRIMARY KEY,
    action TEXT NOT NULL,
    details TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memories_layer ON memories(layer);
CREATE INDEX IF NOT EXISTS idx_memories_room ON memories(room);
CREATE INDEX IF NOT EXISTS idx_memories_owner ON memories(owner_ai);
CREATE INDEX IF NOT EXISTS idx_memories_status ON memories(status);
CREATE INDEX IF NOT EXISTS idx_social_type ON social_posts(type);
CREATE INDEX IF NOT EXISTS idx_group_chat_room ON group_chat(room_id);
"""

SQLITE_SCHEMA = POSTGRES_SCHEMA.replace("SERIAL", "INTEGER").replace("BYTEA", "BLOB")


# ── 数据库适配层 ──
# 提供统一接口，屏蔽 PostgreSQL 和 SQLite 的差异

class DBWrapper:
    """统一数据库接口，让上层代码不用关心是 PG 还是 SQLite"""

    def __init__(self, conn, is_pg=False):
        self._conn = conn
        self._is_pg = is_pg

    async def execute(self, sql, params=None):
        if self._is_pg:
            # PostgreSQL 用 $1, $2... 做占位符
            if params:
                # 把 ? 转换为 $1, $2...
                counter = [0]
                def replace_placeholder(match=None):
                    counter[0] += 1
                    return f"${counter[0]}"
                sql = sql.replace("?", "__PH__")
                for i in range(len(params)):
                    sql = sql.replace("__PH__", f"${i+1}", 1)
                return await self._conn.fetch(sql, *params)
            return await self._conn.fetch(sql)
        else:
            # SQLite
            if params:
                cur = await self._conn.execute(sql, params)
            else:
                cur = await self._conn.execute(sql)
            return cur

    async def fetchone(self, sql, params=None):
        if self._is_pg:
            if params:
                counter = [0]
                sql_new = sql
                for i in range(len(params)):
                    sql_new = sql_new.replace("?", f"${i+1}", 1)
                row = await self._conn.fetchrow(sql_new, *params)
            else:
                row = await self._conn.fetchrow(sql)
            return dict(row) if row else None
        else:
            self._conn.row_factory = aiosqlite.Row
            if params:
                cur = await self._conn.execute(sql, params)
            else:
                cur = await self._conn.execute(sql)
            row = await cur.fetchone()
            return dict(row) if row else None

    async def fetchall(self, sql, params=None):
        if self._is_pg:
            if params:
                for i in range(len(params)):
                    sql = sql.replace("?", f"${i+1}", 1)
                rows = await self._conn.fetch(sql, *params)
            else:
                rows = await self._conn.fetch(sql)
            return [dict(r) for r in rows]
        else:
            self._conn.row_factory = aiosqlite.Row
            if params:
                cur = await self._conn.execute(sql, params)
            else:
                cur = await self._conn.execute(sql)
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def commit(self):
        if not self._is_pg:
            await self._conn.commit()


# ── 初始化 ──

async def init_db():
    if USE_POSTGRES:
        import asyncpg
        conn = await asyncpg.connect(DATABASE_URL)
        await conn.execute(POSTGRES_SCHEMA)
        await conn.close()
    else:
        async with aiosqlite.connect(str(DB_PATH)) as db:
            await db.executescript(SQLITE_SCHEMA)
            await db.commit()


# ── 连接管理 ──

@asynccontextmanager
async def get_db():
    if USE_POSTGRES:
        import asyncpg
        conn = await asyncpg.connect(DATABASE_URL)
        try:
            yield DBWrapper(conn, is_pg=True)
        finally:
            await conn.close()
    else:
        conn = await aiosqlite.connect(str(DB_PATH))
        try:
            yield DBWrapper(conn, is_pg=False)
        finally:
            await conn.close()
