"""
Memory Hub MCP Server
让 Claude Code 通过 MCP 工具直接操作 Memory Hub
"""
import os
import json
import httpx
from mcp.server.fastmcp import FastMCP

HUB_URL = os.getenv("MEMORY_HUB_URL", "https://memory-hub-vry8.onrender.com")
HUB_SECRET = os.getenv("HUB_SECRET", "")

mcp = FastMCP(
    "Memory Hub",
    description="多AI角色共享记忆系统 - 存储、搜索、管理记忆",
)

def _headers():
    return {"Authorization": f"Bearer {HUB_SECRET}", "Content-Type": "application/json"}

async def _post(path: str, data: dict) -> dict:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{HUB_URL}{path}", json=data, headers=_headers())
        r.raise_for_status()
        return r.json()

async def _get(path: str, params: dict = None) -> dict:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{HUB_URL}{path}", params=params, headers=_headers())
        r.raise_for_status()
        return r.json()

async def _put(path: str, data: dict) -> dict:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.put(f"{HUB_URL}{path}", json=data, headers=_headers())
        r.raise_for_status()
        return r.json()

async def _delete(path: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.delete(f"{HUB_URL}{path}", headers=_headers())
        r.raise_for_status()
        return r.json()


@mcp.tool()
async def remember(
    content: str,
    room: str = "living_room",
    importance: float = 0.5,
    tags: list[str] = [],
    source_ai: str = "claude",
    category: str = "",
) -> str:
    """存储一条新记忆到 Memory Hub。

    房间选择：
    - living_room: 核心身份、当前状态（永远注入）
    - career: 职业生涯
    - psychology: 心理状态
    - health: 身体健康
    - learning: 学习目标
    - relationships: 人际关系
    - preferences: 兴趣偏好
    - work_tasks: 工作事务（快速衰减）
    - infra: 基建总览（项目/架构/部署）
    - infra_changelog: 基建更新日志
    - diary: 日记本（per_ai私有）
    - dreams: 梦境/自省（per_ai私有）
    - relationship: 和用户的关系（per_ai私有）
    - personality: 自我认知（per_ai私有）
    - game_room: 游戏房（隔离）

    Args:
        content: 记忆内容
        room: 房间ID
        importance: 重要度 0-1，越高越不容易被遗忘
        tags: 标签列表
        source_ai: 来源AI（claude/gemini/gpt）
        category: 分类标签
    """
    result = await _post("/api/memory/remember", {
        "content": content,
        "room": room,
        "importance": importance,
        "tags": tags,
        "source_ai": source_ai,
        "category": category,
        "source_platform": "claude_code",
    })
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def recall(query: str, top_k: int = 5) -> str:
    """搜索记忆。用自然语言描述要找的内容，会用向量相似度匹配最相关的记忆。

    Args:
        query: 搜索关键词或自然语言描述
        top_k: 返回数量（默认5）
    """
    result = await _get("/api/memory/recall", {"q": query, "ai_id": "claude", "top_k": top_k})
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def list_memories(
    room: str = None,
    status: str = "active",
    page: int = 1,
    per_page: int = 20,
) -> str:
    """列出记忆。可按房间、状态筛选。

    Args:
        room: 房间ID筛选（可选）
        status: 状态筛选：active/archived/decayed
        page: 页码
        per_page: 每页数量
    """
    params = {"status": status, "page": page, "per_page": per_page}
    if room:
        params["room"] = room
    result = await _get("/api/memory/list", params)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def update_memory(
    memory_id: str,
    content: str = None,
    importance: float = None,
    room: str = None,
    tags: list[str] = None,
) -> str:
    """更新一条已有记忆。

    Args:
        memory_id: 记忆ID
        content: 新内容（可选）
        importance: 新重要度（可选）
        room: 移动到新房间（可选）
        tags: 新标签（可选）
    """
    data = {"changed_by": "claude"}
    if content is not None:
        data["content"] = content
    if importance is not None:
        data["importance"] = importance
    if room is not None:
        data["room"] = room
    if tags is not None:
        data["tags"] = tags
    result = await _put(f"/api/memory/{memory_id}", data)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def archive_memory(memory_id: str) -> str:
    """归档一条记忆（不删除，标记为archived）。

    Args:
        memory_id: 记忆ID
    """
    result = await _post(f"/api/memory/{memory_id}/archive", {})
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def delete_memory(memory_id: str) -> str:
    """永久删除一条记忆。

    Args:
        memory_id: 记忆ID
    """
    result = await _delete(f"/api/memory/{memory_id}")
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def corridor() -> str:
    """获取 Claude 的走廊文档 - AI醒来时读的第一份记忆上下文快照。"""
    result = await _get("/api/corridor/claude")
    return result.get("corridor", "（走廊为空）")


@mcp.tool()
async def living_room() -> str:
    """获取客厅内容 - 核心身份和当前状态。"""
    result = await _get("/api/memory/living-room")
    items = result.get("items", [])
    if not items:
        return "（客厅为空）"
    return json.dumps(items, ensure_ascii=False, indent=2)


@mcp.tool()
async def maintain() -> str:
    """执行记忆整理：合并相似记忆、压缩日记、衰减遗忘、重建走廊。"""
    result = await _post("/api/daemon/maintain", {})
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def hub_info() -> str:
    """查看 Memory Hub 的角色和房间配置信息。"""
    result = await _get("/api/info")
    return json.dumps(result, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    mcp.run()
