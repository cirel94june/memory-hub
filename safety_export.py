"""Memory Safety Kit: readable GitHub/Obsidian exports.

This layer is intentionally separate from the JSON room backup. SQLite remains the
runtime source of truth; GitHub gets a readable, deduplicated Markdown mirror for
long-term memories plus a lightweight safety report.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any

import httpx

import database
from config import get_room
from time_utils import local_now

EXPORT_ROOT = "exports/obsidian"
MANIFEST_PATH = f"{EXPORT_ROOT}/manifest.json"
README_PATH = f"{EXPORT_ROOT}/README.md"
LONG_TERM_MIN_IMPORTANCE = 0.5
DISPOSABLE_ROOMS = {"game_room", "work_tasks"}
SHORT_RETENTION_ROOMS = {"social"}


def _github_config() -> tuple[str, str, str]:
    token = os.getenv("GITHUB_TOKEN", "")
    repo = os.getenv("GITHUB_REPO", "")
    branch = os.getenv("GITHUB_BRANCH", "main")
    return token, repo, branch


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_segment(value: Any, fallback: str = "unknown") -> str:
    text = str(value or fallback).strip().lower()
    text = re.sub(r"[^a-z0-9._-]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-._")
    return text[:80] or fallback


def _parse_jsonish(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return default


def _frontmatter_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value).replace('"', '\\"')
    return f'"{text}"'


def should_export_memory(mem: dict) -> tuple[bool, str]:
    """Return whether this memory belongs in the long-term readable export."""
    status = mem.get("status") or "active"
    if status != "active":
        return False, "not_active"

    room = mem.get("room") or "living_room"
    if room in DISPOSABLE_ROOMS:
        return False, "disposable_room"

    anchored = bool(mem.get("anchored"))
    importance = float(mem.get("importance") or 0)
    category = mem.get("category") or ""
    source_platform = mem.get("source_platform") or ""

    if anchored:
        return True, "anchored"
    if category in {"life_chapter", "weekly_digest", "dream"}:
        return True, "curated_category"
    if room in {"living_room", "relationship", "personality", "psychology", "career", "health", "relationships", "preferences", "learning", "diary", "dreams"} and importance >= LONG_TERM_MIN_IMPORTANCE:
        return True, "long_term_room"
    if room in SHORT_RETENTION_ROOMS:
        return False, "short_retention_room"
    if "auto_capture" in source_platform and importance < 0.65:
        return False, "low_signal_auto_capture"
    if importance < LONG_TERM_MIN_IMPORTANCE:
        return False, "low_importance"
    return True, "importance"


def memory_export_path(mem: dict) -> str:
    room = _safe_segment(mem.get("room"), "room")
    layer = _safe_segment(mem.get("layer"), "shared")
    owner = _safe_segment(mem.get("owner_ai"), "shared") if layer == "private" else "shared"
    mem_id = _safe_segment(mem.get("id"), "memory")
    return f"{EXPORT_ROOT}/memories/{layer}/{owner}/{room}/{mem_id}.md"


def render_memory_markdown(mem: dict, reason: str) -> str:
    tags = _parse_jsonish(mem.get("tags"), [])
    comments = _parse_jsonish(mem.get("comments"), [])
    history = _parse_jsonish(mem.get("history"), [])
    room_cfg = get_room(mem.get("room") or "") or {}

    frontmatter = {
        "id": mem.get("id"),
        "layer": mem.get("layer"),
        "room": mem.get("room"),
        "room_name": room_cfg.get("name", ""),
        "category": mem.get("category"),
        "owner_ai": mem.get("owner_ai"),
        "source_ai": mem.get("source_ai"),
        "source_platform": mem.get("source_platform"),
        "importance": mem.get("importance"),
        "decay_score": mem.get("decay_score"),
        "anchored": bool(mem.get("anchored")),
        "export_reason": reason,
        "created_at": mem.get("created_at"),
        "updated_at": mem.get("updated_at"),
        "event_date": mem.get("event_date"),
    }
    lines = ["---"]
    for key, value in frontmatter.items():
        lines.append(f"{key}: {_frontmatter_value(value)}")
    if tags:
        lines.append("tags:")
        for tag in tags:
            lines.append(f"  - {_frontmatter_value(tag)}")
    lines.append("---")
    lines.append("")
    lines.append(f"# {mem.get('id', 'memory')}")
    lines.append("")
    lines.append(mem.get("content") or "")
    lines.append("")
    lines.append("## Metadata")
    lines.append("")
    lines.append(f"- Room: {mem.get('room') or ''}")
    lines.append(f"- Layer: {mem.get('layer') or ''}")
    lines.append(f"- Owner AI: {mem.get('owner_ai') or 'shared'}")
    lines.append(f"- Source AI: {mem.get('source_ai') or ''}")
    lines.append(f"- Importance: {mem.get('importance') or ''}")
    lines.append(f"- Export reason: {reason}")

    if comments:
        lines.append("")
        lines.append("## Comments")
        for item in comments[-10:]:
            if isinstance(item, dict):
                body = item.get("content") or item.get("text") or ""
                by = item.get("author") or item.get("by") or "comment"
                date = item.get("date") or item.get("created_at") or ""
                lines.append(f"- {date} {by}: {body}")

    if history:
        lines.append("")
        lines.append("## Recent History")
        for item in history[-5:]:
            if isinstance(item, dict):
                body = item.get("content") or ""
                by = item.get("by") or "history"
                date = item.get("date") or ""
                lines.append(f"- {date} {by}: {body[:240]}")

    return "\n".join(lines).rstrip() + "\n"


def render_readme() -> str:
    return """# Memory Hub Obsidian Export

这个目录是 Memory Hub 的可读安全导出，不是运行时数据库。

- SQLite 仍然是 Memory Hub 在线服务使用的主数据库。
- GitHub 里的 JSON 房间备份用于机器恢复。
- 这里的 Markdown 用于 Obsidian 阅读、人工检查和安心留档。

## 怎么用 Obsidian 打开

1. 在自己的电脑上把这个 GitHub 仓库 clone 到一个文件夹。
2. 打开 Obsidian。
3. 选择 `Open folder as vault`。
4. 选择仓库里的 `exports/obsidian` 文件夹。

不需要 Obsidian 账号。Obsidian 只是本地阅读器；云端副本在 GitHub。

## 导出规则

- 同一条记忆按 memory id 固定写入同一个 Markdown 文件。
- 只有新增或 `updated_at` 改变的记忆会重新导出，避免每天重复生成新文件。
- 已归档、低重要度、临时社交闲聊和工作临时事项默认不进入长期 Markdown 导出。
- 锚点、人生章节、梦境、周记和重要长期房间会优先保留。
- `reports/` 里是安全报告，用来快速确认数据库是否可读、各房间数量是否正常、最近一次导出是否成功。
"""


def build_safety_report(memories: list[dict], selected: list[tuple[dict, str]], skipped: Counter) -> str:
    today = local_now().strftime("%Y-%m-%d")
    by_room = Counter((m.get("room") or "unknown") for m in memories if m.get("status") == "active")
    by_ai = Counter((m.get("owner_ai") or m.get("source_ai") or "shared") for m in memories if m.get("status") == "active")
    active_count = sum(1 for m in memories if m.get("status") == "active")
    archived_count = sum(1 for m in memories if m.get("status") == "archived")
    anchored_count = sum(1 for m in memories if m.get("anchored"))
    latest = sorted((m.get("updated_at") or "" for m in memories), reverse=True)[:1]

    lines = [
        f"# Memory Safety Report {today}",
        "",
        "## Summary",
        "",
        f"- Active memories: {active_count}",
        f"- Archived memories: {archived_count}",
        f"- Anchored memories: {anchored_count}",
        f"- Long-term Markdown exports selected: {len(selected)}",
        f"- Latest memory update: {latest[0] if latest else 'none'}",
        f"- Generated at: {_now_utc()}",
        "",
        "## Active Memories By Room",
        "",
    ]
    for room, count in by_room.most_common():
        lines.append(f"- {room}: {count}")

    lines.extend(["", "## Active Memories By AI", ""])
    for ai_id, count in by_ai.most_common():
        lines.append(f"- {ai_id}: {count}")

    lines.extend(["", "## Skipped From Long-Term Markdown", ""])
    for reason, count in skipped.most_common():
        lines.append(f"- {reason}: {count}")

    lines.extend(["", "## Exported Reasons", ""])
    reason_counts = Counter(reason for _, reason in selected)
    for reason, count in reason_counts.most_common():
        lines.append(f"- {reason}: {count}")

    return "\n".join(lines).rstrip() + "\n"


async def _read_github_json(path: str) -> dict:
    token, repo, branch = _github_config()
    if not token or not repo:
        return {}
    url = f"https://api.github.com/repos/{repo}/contents/{path}?ref={branch}"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(url, headers=_headers(token))
            if resp.status_code == 404:
                return {}
            resp.raise_for_status()
            data = resp.json()
            content = base64.b64decode(data["content"]).decode("utf-8")
            parsed = json.loads(content)
            return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


async def _commit_files(files: dict[str, str], message: str, delete_paths: set[str] | None = None) -> str | None:
    token, repo, branch = _github_config()
    delete_paths = delete_paths or set()
    if not token or not repo or (not files and not delete_paths):
        return None

    headers = _headers(token)
    base = f"https://api.github.com/repos/{repo}"
    async with httpx.AsyncClient(timeout=60) as client:
        ref = await client.get(f"{base}/git/ref/heads/{branch}", headers=headers)
        ref.raise_for_status()
        parent_sha = ref.json()["object"]["sha"]

        parent = await client.get(f"{base}/git/commits/{parent_sha}", headers=headers)
        parent.raise_for_status()
        base_tree = parent.json()["tree"]["sha"]

        tree = []
        for path, content in files.items():
            blob = await client.post(
                f"{base}/git/blobs",
                headers=headers,
                json={"content": base64.b64encode(content.encode("utf-8")).decode("ascii"), "encoding": "base64"},
            )
            blob.raise_for_status()
            tree.append({"path": path, "mode": "100644", "type": "blob", "sha": blob.json()["sha"]})

        for path in sorted(delete_paths):
            if path not in files:
                tree.append({"path": path, "mode": "100644", "type": "blob", "sha": None})

        new_tree = await client.post(f"{base}/git/trees", headers=headers, json={"base_tree": base_tree, "tree": tree})
        new_tree.raise_for_status()
        commit = await client.post(
            f"{base}/git/commits",
            headers=headers,
            json={"message": message, "tree": new_tree.json()["sha"], "parents": [parent_sha]},
        )
        commit.raise_for_status()
        commit_sha = commit.json()["sha"]
        update = await client.patch(
            f"{base}/git/refs/heads/{branch}",
            headers=headers,
            json={"sha": commit_sha, "force": False},
        )
        update.raise_for_status()
        return commit_sha


async def export_obsidian(dry_run: bool = False, force: bool = False) -> dict:
    memories = list(database.iter_memories(status=None, batch_size=500))
    manifest = await _read_github_json(MANIFEST_PATH)
    manifest_memories = manifest.get("memories", {}) if isinstance(manifest.get("memories"), dict) else {}

    selected: list[tuple[dict, str]] = []
    skipped: Counter = Counter()
    for mem in memories:
        ok, reason = should_export_memory(mem)
        if ok:
            selected.append((mem, reason))
        else:
            skipped[reason] += 1

    files: dict[str, str] = {README_PATH: render_readme()}
    new_manifest: dict[str, Any] = {
        "version": 1,
        "updated_at": _now_utc(),
        "export_root": EXPORT_ROOT,
        "memories": {},
    }

    changed = 0
    unchanged = 0
    stale_paths: set[str] = set()
    by_reason: Counter = Counter()
    for mem, reason in selected:
        path = memory_export_path(mem)
        markdown = render_memory_markdown(mem, reason)
        checksum = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
        previous = manifest_memories.get(mem.get("id"), {})
        if previous.get("path") and previous.get("path") != path:
            stale_paths.add(previous["path"])
        if force or previous.get("checksum") != checksum or previous.get("path") != path:
            files[path] = markdown
            changed += 1
        else:
            unchanged += 1
        by_reason[reason] += 1
        new_manifest["memories"][mem.get("id")] = {
            "path": path,
            "updated_at": mem.get("updated_at"),
            "checksum": checksum,
            "room": mem.get("room"),
            "layer": mem.get("layer"),
            "owner_ai": mem.get("owner_ai"),
            "reason": reason,
        }

    for old_id, previous in manifest_memories.items():
        if old_id not in new_manifest["memories"] and previous.get("path"):
            stale_paths.add(previous["path"])

    today = local_now().strftime("%Y-%m-%d")
    report_path = f"{EXPORT_ROOT}/reports/{today}.md"
    report = build_safety_report(memories, selected, skipped)
    files[report_path] = report
    new_manifest["last_report"] = report_path
    new_manifest["selected_count"] = len(selected)
    new_manifest["skipped"] = dict(skipped)
    new_manifest["reasons"] = dict(by_reason)
    new_manifest["deleted_paths"] = sorted(stale_paths)
    files[MANIFEST_PATH] = json.dumps(new_manifest, ensure_ascii=False, indent=2) + "\n"

    commit_sha = None
    if not dry_run:
        commit_sha = await _commit_files(files, f"Memory safety export {today}", delete_paths=stale_paths)

    return {
        "status": "dry_run" if dry_run else "exported",
        "commit": commit_sha,
        "selected": len(selected),
        "changed_memories": changed,
        "unchanged_memories": unchanged,
        "files_written": len(files) if not dry_run else 0,
        "files_deleted": len(stale_paths) if not dry_run else 0,
        "report_path": report_path,
        "manifest_path": MANIFEST_PATH,
        "skipped": dict(skipped),
        "reasons": dict(by_reason),
        "github_configured": bool(_github_config()[0] and _github_config()[1]),
    }
