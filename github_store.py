"""
GitHub 仓库存储引擎 (v2 — SQLite primary, GitHub backup)
- SQLite (via database.py) is the primary storage backend
- GitHub serves as a periodic backup (pushed every 12h by daemon)
- First load: if SQLite is empty, imports from GitHub (one-time migration)
"""
import os
import json
import base64
import asyncio
import logging
from datetime import datetime, timezone

import httpx

import database

log = logging.getLogger("github_store")

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "")  # 格式: "username/repo-name"
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")

# ── Social / group chat still in-memory (minor, not migrated) ──
_social_posts: list[dict] = []
_group_chat: list[dict] = []

# ── Backward-compat shim: memory_ops.py writes to _dirty_files directly ──
# With SQLite as primary storage this is no longer needed for tracking,
# but we expose it as a dummy set so existing code doesn't crash.
_dirty_files: set[str] = set()


def _github_headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }


def _api_base():
    return f"https://api.github.com/repos/{GITHUB_REPO}/contents"


# ══════════════════════════════════════════
#  GitHub I/O (kept for corridor.py + backup)
# ══════════════════════════════════════════

async def _read_github_file(path: str) -> dict | list | None:
    """读取 GitHub 仓库中的 JSON 文件"""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return None
    url = f"{_api_base()}/{path}?ref={GITHUB_BRANCH}"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=_github_headers())
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            data = resp.json()
            content = base64.b64decode(data["content"]).decode("utf-8")
            return json.loads(content)
    except Exception as e:
        log.error(f"Failed to read {path}: {e}")
        return None


async def _write_github_file(path: str, content: dict | list, message: str = ""):
    """写入 JSON 文件到 GitHub 仓库"""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return
    url = f"{_api_base()}/{path}"
    json_str = json.dumps(content, ensure_ascii=False, indent=2)
    encoded = base64.b64encode(json_str.encode("utf-8")).decode("utf-8")

    # 先获取文件的 SHA（如果存在的话，更新需要 SHA）
    sha = None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{url}?ref={GITHUB_BRANCH}", headers=_github_headers())
            if resp.status_code == 200:
                sha = resp.json().get("sha")
    except Exception:
        pass

    body = {
        "message": message or f"Update {path}",
        "content": encoded,
        "branch": GITHUB_BRANCH,
    }
    if sha:
        body["sha"] = sha

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.put(url, json=body, headers=_github_headers())
            resp.raise_for_status()
            log.info(f"Pushed {path} to GitHub")
    except Exception as e:
        log.error(f"Failed to push {path}: {e}")


# ══════════════════════════════════════════
#  File path mapping (kept for GitHub backup export)
# ══════════════════════════════════════════

def _file_path_for_memory(mem: dict) -> str:
    """根据记忆属性决定存在哪个文件"""
    from config import get_room

    room_id = mem.get("room", "living_room")
    owner = mem.get("owner_ai", "")
    room_cfg = get_room(room_id) or {}
    scope = room_cfg.get("scope", "shared")

    if scope == "per_ai" and owner:
        return f"private/{owner}/{room_id}.json"
    elif room_id == "living_room":
        return "shared/living_room.json"
    elif room_id.startswith("infra"):
        return f"shared/{room_id}.json"
    elif room_cfg.get("type") == "isolated":
        return f"isolated/{room_id}.json"
    else:
        return f"shared/{room_id}.json"


# ══════════════════════════════════════════
#  GitHub directory listing (used during migration)
# ══════════════════════════════════════════

async def _list_github_dir(path: str) -> list[str]:
    """列出 GitHub 仓库中某个目录下的文件"""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return []
    url = f"{_api_base()}/{path}?ref={GITHUB_BRANCH}"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=_github_headers())
            if resp.status_code == 404:
                return []
            resp.raise_for_status()
            items = resp.json()
            if isinstance(items, list):
                return [item["path"] for item in items if item.get("name", "").endswith(".json")]
            return []
    except Exception as e:
        log.error(f"Failed to list {path}: {e}")
        return []


# ══════════════════════════════════════════
#  Load / Init
# ══════════════════════════════════════════

async def load_all():
    """Initialize database. If SQLite is empty, import from GitHub (one-time migration)."""
    global _social_posts, _group_chat

    # 1. Init SQLite
    await database.init_db()

    # 2. Load custom rooms config from GitHub (if available)
    if GITHUB_TOKEN and GITHUB_REPO:
        custom_rooms = await _read_github_file("_config/custom_rooms.json")
        if custom_rooms and isinstance(custom_rooms, list):
            from config import register_room
            for r in custom_rooms:
                if "id" in r:
                    register_room(r["id"], r)
                    log.info(f"  Registered custom room: {r['id']} ({r.get('name', '')})")

    # 3. Check if SQLite already has data
    count = database.count_memories()
    if count > 0:
        log.info(f"SQLite has {count} memories, skipping GitHub import")
        return

    # 4. SQLite is empty — import from GitHub (one-time migration)
    if not GITHUB_TOKEN or not GITHUB_REPO:
        log.warning("GitHub not configured and SQLite is empty, running with no data")
        return

    log.info(f"SQLite empty, importing from GitHub ({GITHUB_REPO})...")

    dirs_to_scan = ["shared", "private/claude", "private/gemini", "private/gpt", "isolated", "social"]
    all_files = []
    for d in dirs_to_scan:
        files = await _list_github_dir(d)
        all_files.extend(files)

    imported = 0
    for path in all_files:
        data = await _read_github_file(path)
        if data is None:
            continue

        if path.startswith("social/"):
            if isinstance(data, list):
                _social_posts.extend(data)
        else:
            if isinstance(data, list):
                for mem in data:
                    if "id" in mem:
                        # Ensure required timestamp fields exist
                        now = datetime.now(timezone.utc).isoformat()
                        mem.setdefault("created_at", now)
                        mem.setdefault("updated_at", now)
                        mem.setdefault("status", "active")
                        database.set_memory(mem)
                        imported += 1

    log.info(f"Imported {imported} memories from GitHub into SQLite, {len(_social_posts)} social posts")


# ══════════════════════════════════════════
#  Push (full export to GitHub backup)
# ══════════════════════════════════════════

async def push_dirty():
    """Export all active memories from SQLite to GitHub JSON files.

    This replaces the old dirty-file tracking. Now it does a full export
    of all active memories, grouped by file path, and pushes to GitHub.
    Called periodically (every 12h) by the daemon.
    """
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return

    # Clear the compat shim set (memory_ops.py may have added paths)
    _dirty_files.clear()

    # Collect all active memories from SQLite, grouped by file path
    files: dict[str, list[dict]] = {}
    for mem in database.iter_memories(status="active"):
        path = _file_path_for_memory(mem)
        if path not in files:
            files[path] = []
        # Export without embedding (too large for GitHub JSON)
        export = {k: v for k, v in mem.items() if k != "embedding"}
        files[path].append(export)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")

    for path, data in files.items():
        await _write_github_file(path, data, f"Memory backup {now}")

    log.info(f"Pushed {sum(len(v) for v in files.values())} memories to GitHub in {len(files)} files")


async def push_social():
    """推送社交数据"""
    forum = [p for p in _social_posts if p.get("type") == "forum"]
    moments = [p for p in _social_posts if p.get("type") == "moment"]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    if forum:
        await _write_github_file("social/forum.json", forum, f"Forum update {now}")
    if moments:
        await _write_github_file("social/moments.json", moments, f"Moments update {now}")


# ══════════════════════════════════════════
#  Memory CRUD — delegates to database.py
# ══════════════════════════════════════════

def get_all_memories() -> dict[str, dict]:
    """Compatibility shim: return all active memories as {id: dict}.

    Used by corridor.py, daemon.py, memory_ops.py.
    Queries SQLite and builds the dict on the fly.
    """
    result: dict[str, dict] = {}
    for mem in database.iter_memories(status="active"):
        result[mem["id"]] = mem
    return result


def get_memory(mem_id: str) -> dict | None:
    """Get a single memory by ID. Delegates to database.py."""
    return database.get_memory(mem_id)


def set_memory(mem: dict):
    """Insert or update a memory. Delegates to database.py.

    No more _dirty_files tracking — GitHub push is periodic.
    """
    database.set_memory(mem)


def remove_memory(mem_id: str):
    """Delete a memory. Delegates to database.py."""
    database.remove_memory(mem_id)


# ══════════════════════════════════════════
#  Social (still in-memory)
# ══════════════════════════════════════════

def get_social_posts() -> list[dict]:
    return _social_posts


def add_social_post(post: dict):
    _social_posts.append(post)
