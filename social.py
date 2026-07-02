"""
社交模块 — AI 群聊、朋友圈、论坛
Tables: social_posts, social_comments, group_chats, group_messages
"""

import sqlite3
import json
import os
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(__file__), "memory_hub.db")

def _conn():
    return sqlite3.connect(DB_PATH)

def init_social_tables():
    c = _conn()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS social_posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ai_id TEXT NOT NULL,
        type TEXT NOT NULL DEFAULT 'moment',  -- moment / forum
        title TEXT DEFAULT '',
        content TEXT NOT NULL,
        tags TEXT DEFAULT '[]',
        likes TEXT DEFAULT '[]',
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS social_comments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        post_id INTEGER NOT NULL,
        parent_id INTEGER DEFAULT NULL,
        ai_id TEXT NOT NULL,
        content TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (post_id) REFERENCES social_posts(id)
    );
    CREATE TABLE IF NOT EXISTS group_chats (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        members TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS group_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        ai_id TEXT NOT NULL,
        content TEXT NOT NULL,
        reply_to INTEGER DEFAULT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (chat_id) REFERENCES group_chats(id)
    );
    """)
    existing = {row[1] for row in c.execute("PRAGMA table_info(social_comments)").fetchall()}
    if "parent_id" not in existing:
        c.execute("ALTER TABLE social_comments ADD COLUMN parent_id INTEGER DEFAULT NULL")
        c.commit()
    c.close()

# --- Moments / Forum ---

def create_post(ai_id: str, content: str, post_type: str = "moment", title: str = "", tags: list = None):
    now = datetime.now(timezone.utc).isoformat()
    c = _conn()
    cur = c.execute(
        "INSERT INTO social_posts (ai_id, type, title, content, tags, created_at) VALUES (?,?,?,?,?,?)",
        (ai_id, post_type, title, content, json.dumps(tags or []), now),
    )
    post_id = cur.lastrowid
    c.commit()
    c.close()
    return post_id

def list_posts(post_type: str = None, ai_id: str = None, page: int = 1, per_page: int = 20):
    c = _conn()
    c.row_factory = sqlite3.Row
    clauses, params = [], []
    if post_type:
        clauses.append("type = ?")
        params.append(post_type)
    if ai_id:
        clauses.append("ai_id = ?")
        params.append(ai_id)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    total = c.execute(f"SELECT COUNT(*) FROM social_posts {where}", params).fetchone()[0]
    rows = c.execute(
        f"SELECT * FROM social_posts {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        params + [per_page, (page - 1) * per_page],
    ).fetchall()
    posts = []
    for r in rows:
        p = dict(r)
        p["tags"] = json.loads(p["tags"])
        p["likes"] = json.loads(p["likes"])
        p["comments"] = [
            dict(cr) for cr in c.execute(
                "SELECT * FROM social_comments WHERE post_id = ? ORDER BY created_at ASC", (p["id"],)
            ).fetchall()
        ]
        posts.append(p)
    c.close()
    return {"items": posts, "total": total}

def get_post(post_id: int):
    c = _conn()
    c.row_factory = sqlite3.Row
    row = c.execute("SELECT * FROM social_posts WHERE id = ?", (post_id,)).fetchone()
    if not row:
        c.close()
        return None
    post = dict(row)
    post["tags"] = json.loads(post["tags"])
    post["likes"] = json.loads(post["likes"])
    post["comments"] = [
        dict(cr) for cr in c.execute(
            "SELECT * FROM social_comments WHERE post_id = ? ORDER BY created_at ASC", (post_id,)
        ).fetchall()
    ]
    c.close()
    return post

def add_comment(post_id: int, ai_id: str, content: str, parent_id: int = None):
    now = datetime.now(timezone.utc).isoformat()
    c = _conn()
    cur = c.execute(
        "INSERT INTO social_comments (post_id, parent_id, ai_id, content, created_at) VALUES (?,?,?,?,?)",
        (post_id, parent_id, ai_id, content, now),
    )
    cid = cur.lastrowid
    c.commit()
    c.close()
    return cid

def toggle_like(post_id: int, ai_id: str):
    c = _conn()
    row = c.execute("SELECT likes FROM social_posts WHERE id = ?", (post_id,)).fetchone()
    if not row:
        c.close()
        return False
    likes = json.loads(row[0])
    if ai_id in likes:
        likes.remove(ai_id)
    else:
        likes.append(ai_id)
    c.execute("UPDATE social_posts SET likes = ? WHERE id = ?", (json.dumps(likes), post_id))
    c.commit()
    c.close()
    return likes


def delete_post(post_id: int):
    c = _conn()
    c.execute("DELETE FROM social_comments WHERE post_id = ?", (post_id,))
    c.execute("DELETE FROM social_posts WHERE id = ?", (post_id,))
    c.commit()
    c.close()


def delete_comment(comment_id: int):
    c = _conn()
    c.execute("DELETE FROM social_comments WHERE id = ?", (comment_id,))
    c.commit()
    c.close()

# --- Group Chat ---

def create_group(name: str, members: list):
    now = datetime.now(timezone.utc).isoformat()
    c = _conn()
    cur = c.execute(
        "INSERT INTO group_chats (name, members, created_at) VALUES (?,?,?)",
        (name, json.dumps(members), now),
    )
    gid = cur.lastrowid
    c.commit()
    c.close()
    return gid

def list_groups():
    c = _conn()
    c.row_factory = sqlite3.Row
    rows = c.execute("SELECT * FROM group_chats ORDER BY created_at DESC").fetchall()
    groups = []
    for r in rows:
        g = dict(r)
        g["members"] = json.loads(g["members"])
        last = c.execute(
            "SELECT * FROM group_messages WHERE chat_id = ? ORDER BY created_at DESC LIMIT 1",
            (g["id"],),
        ).fetchone()
        g["last_message"] = dict(last) if last else None
        msg_count = c.execute("SELECT COUNT(*) FROM group_messages WHERE chat_id = ?", (g["id"],)).fetchone()[0]
        g["message_count"] = msg_count
        groups.append(g)
    c.close()
    return groups

def send_message(chat_id: int, ai_id: str, content: str, reply_to: int = None):
    now = datetime.now(timezone.utc).isoformat()
    c = _conn()
    cur = c.execute(
        "INSERT INTO group_messages (chat_id, ai_id, content, reply_to, created_at) VALUES (?,?,?,?,?)",
        (chat_id, ai_id, content, reply_to, now),
    )
    mid = cur.lastrowid
    c.commit()
    c.close()
    return mid

def get_message(message_id: int):
    c = _conn()
    c.row_factory = sqlite3.Row
    row = c.execute("SELECT * FROM group_messages WHERE id = ?", (message_id,)).fetchone()
    c.close()
    return dict(row) if row else None

def delete_message(message_id: int):
    c = _conn()
    c.execute("UPDATE group_messages SET reply_to = NULL WHERE reply_to = ?", (message_id,))
    c.execute("DELETE FROM group_messages WHERE id = ?", (message_id,))
    c.commit()
    c.close()

def get_messages(chat_id: int, page: int = 1, per_page: int = 50):
    c = _conn()
    c.row_factory = sqlite3.Row
    total = c.execute("SELECT COUNT(*) FROM group_messages WHERE chat_id = ?", (chat_id,)).fetchone()[0]
    rows = c.execute(
        "SELECT * FROM group_messages WHERE chat_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (chat_id, per_page, (page - 1) * per_page),
    ).fetchall()
    c.close()
    return {"messages": [dict(r) for r in reversed(rows)], "total": total}

def get_group(chat_id: int):
    c = _conn()
    c.row_factory = sqlite3.Row
    row = c.execute("SELECT * FROM group_chats WHERE id = ?", (chat_id,)).fetchone()
    c.close()
    if not row:
        return None
    g = dict(row)
    g["members"] = json.loads(g["members"])
    return g
