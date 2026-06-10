"""
一次性迁移脚本：从 GitHub 仓库导入记忆到 SQLite
Usage: python migrate_to_sqlite.py
"""
import os
import sys
import json
import base64
import asyncio
import logging
import struct
from collections import defaultdict

import httpx

# ── 确保项目根目录在 sys.path ──
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import database
from embedding import get_embedding, pack_embedding

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("migrate")

# ── GitHub 配置 ──
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "")
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")


def _github_headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }


def _api_base():
    return f"https://api.github.com/repos/{GITHUB_REPO}/contents"


# ════════════════════════════════════════════
#  GitHub 读取
# ════════════════════════════════════════════

async def list_github_dir(client: httpx.AsyncClient, path: str) -> list[str]:
    """列出 GitHub 目录下的 JSON 文件路径"""
    url = f"{_api_base()}/{path}?ref={GITHUB_BRANCH}"
    try:
        resp = await client.get(url, headers=_github_headers())
        if resp.status_code == 404:
            log.info(f"  目录不存在，跳过: {path}")
            return []
        resp.raise_for_status()
        items = resp.json()
        if isinstance(items, list):
            json_files = [
                item["path"]
                for item in items
                if item.get("name", "").endswith(".json")
                and item.get("type") == "file"
            ]
            return json_files
        return []
    except Exception as e:
        log.error(f"  列出目录失败 {path}: {e}")
        return []


async def read_github_file(client: httpx.AsyncClient, path: str) -> list | dict | None:
    """读取 GitHub 仓库中的 JSON 文件，返回解析后的数据"""
    url = f"{_api_base()}/{path}?ref={GITHUB_BRANCH}"
    try:
        resp = await client.get(url, headers=_github_headers())
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        content = base64.b64decode(data["content"]).decode("utf-8")
        return json.loads(content)
    except Exception as e:
        log.error(f"  读取文件失败 {path}: {e}")
        return None


# ════════════════════════════════════════════
#  迁移逻辑
# ════════════════════════════════════════════

async def migrate():
    # ── 检查 GitHub 配置 ──
    if not GITHUB_TOKEN or not GITHUB_REPO:
        print("GitHub 未配置。请设置环境变量 GITHUB_TOKEN 和 GITHUB_REPO 后重试。")
        print("  GITHUB_TOKEN: GitHub personal access token")
        print("  GITHUB_REPO:  格式 'username/repo-name'")
        sys.exit(1)

    print(f"=== Memory Hub 迁移工具 ===")
    print(f"GitHub 仓库: {GITHUB_REPO} (branch: {GITHUB_BRANCH})")
    print()

    # ── 1. 初始化 SQLite ──
    print("[1/5] 初始化 SQLite 数据库...")
    await database.init_db()
    print(f"  数据库位置: {database.DB_PATH}")

    # ── 2. 检查已有数据 ──
    existing_count = database.count_memories(status="active")
    archived_count = database.count_memories(status="archived")
    total_existing = existing_count + archived_count

    if total_existing > 0:
        print(f"\n  数据库中已有 {total_existing} 条记忆 (active={existing_count}, archived={archived_count})")
        answer = input("  是否继续导入？已有的记忆会被覆盖 (y/N): ").strip().lower()
        if answer not in ("y", "yes"):
            print("  已取消。")
            sys.exit(0)
        print()

    # ── 3. 加载自定义房间配置 ──
    print("[2/5] 检查自定义房间配置...")
    async with httpx.AsyncClient(timeout=15) as client:
        custom_rooms = await read_github_file(client, "_config/custom_rooms.json")

    if custom_rooms and isinstance(custom_rooms, list):
        from config import register_room
        for r in custom_rooms:
            if "id" in r:
                register_room(r["id"], r)
                print(f"  注册自定义房间: {r['id']} ({r.get('name', '')})")
    else:
        print("  无自定义房间配置")

    # ── 4. 扫描 GitHub 目录并下载 JSON ──
    print("[3/5] 扫描 GitHub 仓库目录...")

    dirs_to_scan = [
        "shared",
        "private/claude",
        "private/gemini",
        "private/gpt",
        "private/cloudy",
        "private/lucien",
        "private/jasper",
        "isolated",
    ]

    all_files: list[str] = []
    async with httpx.AsyncClient(timeout=15) as client:
        for d in dirs_to_scan:
            files = await list_github_dir(client, d)
            if files:
                print(f"  {d}/ -> {len(files)} 个 JSON 文件")
                all_files.extend(files)

    if not all_files:
        print("\n  GitHub 仓库中未找到任何 JSON 文件。无需迁移。")
        sys.exit(0)

    print(f"\n  共找到 {len(all_files)} 个文件，开始下载...")

    # ── 5. 下载并导入 ──
    print("[4/5] 下载并导入记忆...")

    total_imported = 0
    room_counts: dict[str, int] = defaultdict(int)
    skipped = 0
    errors = 0

    async with httpx.AsyncClient(timeout=30) as client:
        for i, path in enumerate(all_files, 1):
            print(f"  [{i}/{len(all_files)}] {path} ...", end=" ", flush=True)

            data = await read_github_file(client, path)
            if data is None:
                print("跳过 (读取失败)")
                errors += 1
                continue

            if not isinstance(data, list):
                print("跳过 (非列表格式)")
                skipped += 1
                continue

            file_count = 0
            for mem in data:
                if not isinstance(mem, dict) or "id" not in mem:
                    skipped += 1
                    continue

                # GitHub JSON 不含 embedding，设为 None
                mem.pop("embedding", None)
                mem["embedding"] = None

                # 确保必要字段存在
                if "created_at" not in mem or not mem["created_at"]:
                    mem["created_at"] = mem.get("updated_at", "")
                if "updated_at" not in mem or not mem["updated_at"]:
                    mem["updated_at"] = mem.get("created_at", "")

                try:
                    database.set_memory(mem)
                    file_count += 1
                    room = mem.get("room", "unknown")
                    room_counts[room] += 1
                except Exception as e:
                    log.error(f"    导入失败 {mem['id']}: {e}")
                    errors += 1

            total_imported += file_count
            print(f"{file_count} 条记忆")

    print(f"\n  导入完成: {total_imported} 条记忆, {skipped} 跳过, {errors} 错误")

    # ── 6. 生成 embeddings ──
    print("[5/5] 生成 embeddings...")

    # 查询所有没有 embedding 的记忆
    all_ids = database.get_all_memory_ids()
    no_embedding_ids = []

    conn = database._get_conn()
    for mem_id in all_ids:
        row = conn.execute(
            "SELECT embedding FROM memories WHERE id = ?", (mem_id,)
        ).fetchone()
        if row is None or row[0] is None:
            no_embedding_ids.append(mem_id)

    print(f"  需要生成 embedding 的记忆: {len(no_embedding_ids)}/{len(all_ids)}")

    embedded_count = 0
    embed_errors = 0

    for i, mem_id in enumerate(no_embedding_ids, 1):
        mem = database.get_memory(mem_id)
        if not mem:
            continue

        content = mem.get("content", "")
        if not content or not content.strip():
            continue

        if i % 50 == 0 or i == 1:
            print(f"  进度: {i}/{len(no_embedding_ids)}...")

        try:
            vec = await get_embedding(content)
            if vec:
                mem["embedding"] = pack_embedding(vec)
                database.set_memory(mem)
                embedded_count += 1
        except Exception as e:
            embed_errors += 1
            if embed_errors <= 5:
                log.error(f"  Embedding 失败 {mem_id}: {e}")

    print(f"  Embedding 生成完成: {embedded_count} 成功, {embed_errors} 失败")

    # ── 统计 ──
    print()
    print("=" * 50)
    print("迁移统计")
    print("=" * 50)
    print(f"  总导入:     {total_imported} 条记忆")
    print(f"  Embedding:  {embedded_count}/{total_imported} 已生成")
    print(f"  跳过:       {skipped}")
    print(f"  错误:       {errors + embed_errors}")
    print()
    print("  各房间记忆数:")
    for room, count in sorted(room_counts.items(), key=lambda x: -x[1]):
        print(f"    {room}: {count}")
    print()
    print(f"  数据库文件: {database.DB_PATH}")
    print("  迁移完成!")


if __name__ == "__main__":
    asyncio.run(migrate())
