"""
GitHub 仓库存储引擎
- 启动时从 GitHub 仓库加载所有记忆到内存
- 写入时同步推送到 GitHub
- JSON 文件按房间/AI角色分组
"""
import os
import json
import base64
import asyncio
import logging
from datetime import datetime, timezone

import httpx

log = logging.getLogger("github_store")

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "")  # 格式: "username/repo-name"
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")

# ── 内存缓存 ──
_memories: dict[str, dict] = {}  # id -> memory dict
_social_posts: list[dict] = []
_group_chat: list[dict] = []
_dirty_files: set[str] = set()  # 需要推送到 GitHub 的文件路径
_push_lock = asyncio.Lock()


def _github_headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }


def _api_base():
    return f"https://api.github.com/repos/{GITHUB_REPO}/contents"


# ── 从 GitHub 读取文件 ──

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


# ── 写入文件到 GitHub ──

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


# ── 记忆文件路径映射 ──

def _file_path_for_memory(mem: dict) -> str:
    """根据记忆属性决定存在哪个文件"""
    from config import get_room

    room_id = mem.get("room", "living_room")
    owner = mem.get("owner_ai", "")
    room_cfg = get_room(room_id) or {}
    scope = room_cfg.get("scope", "shared")

    if scope == "per_ai" and owner:
        # 每个 AI 各一份：private/claude/diary.json
        return f"private/{owner}/{room_id}.json"
    elif room_id == "living_room":
        return "shared/living_room.json"
    elif room_id.startswith("infra"):
        return f"shared/{room_id}.json"
    elif room_cfg.get("type") == "isolated":
        return f"isolated/{room_id}.json"
    else:
        return f"shared/{room_id}.json"


def _collect_memories_by_file() -> dict[str, list[dict]]:
    """将内存中的记忆按文件路径分组"""
    files: dict[str, list[dict]] = {}
    for mem in _memories.values():
        path = _file_path_for_memory(mem)
        if path not in files:
            files[path] = []
        # 导出时去掉 embedding（太大了不存 GitHub）
        export = {k: v for k, v in mem.items() if k != "embedding"}
        files[path].append(export)
    return files


# ── 加载所有记忆 ──

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


async def load_all():
    """启动时从 GitHub 加载全部记忆（自动扫描所有目录）"""
    global _memories, _social_posts, _group_chat

    if not GITHUB_TOKEN or not GITHUB_REPO:
        log.warning("GitHub not configured, running with empty memory")
        return

    log.info(f"Loading memories from {GITHUB_REPO}...")

    # 自动加载自定义房间配置（如果有的话）
    custom_rooms = await _read_github_file("_config/custom_rooms.json")
    if custom_rooms and isinstance(custom_rooms, list):
        from config import register_room
        for r in custom_rooms:
            if "id" in r:
                register_room(r["id"], r)
                log.info(f"  Registered custom room: {r['id']} ({r.get('name', '')})")

    # 扫描所有目录
    dirs_to_scan = ["shared", "private/claude", "private/gemini", "private/gpt", "isolated", "social"]
    all_files = []
    for d in dirs_to_scan:
        files = await _list_github_dir(d)
        all_files.extend(files)

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
                        _memories[mem["id"]] = mem

    log.info(f"Loaded {len(_memories)} memories, {len(_social_posts)} social posts from {len(all_files)} files")


# ── 保存到 GitHub（批量推送） ──

async def push_dirty():
    """将修改过的文件推送到 GitHub"""
    async with _push_lock:
        if not _dirty_files:
            return

        files = _collect_memories_by_file()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")

        for path in list(_dirty_files):
            data = files.get(path, [])
            await _write_github_file(path, data, f"Memory update {now}")

        _dirty_files.clear()


async def push_social():
    """推送社交数据"""
    forum = [p for p in _social_posts if p.get("type") == "forum"]
    moments = [p for p in _social_posts if p.get("type") == "moment"]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    if forum:
        await _write_github_file("social/forum.json", forum, f"Forum update {now}")
    if moments:
        await _write_github_file("social/moments.json", moments, f"Moments update {now}")


# ── 内存操作 ──

def get_all_memories() -> dict[str, dict]:
    return _memories


def get_memory(mem_id: str) -> dict | None:
    return _memories.get(mem_id)


def set_memory(mem: dict):
    """写入/更新内存中的记忆，并标记文件为脏"""
    _memories[mem["id"]] = mem
    _dirty_files.add(_file_path_for_memory(mem))


def remove_memory(mem_id: str):
    mem = _memories.pop(mem_id, None)
    if mem:
        _dirty_files.add(_file_path_for_memory(mem))


def get_social_posts() -> list[dict]:
    return _social_posts


def add_social_post(post: dict):
    _social_posts.append(post)
