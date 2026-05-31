"""
Memory Hub MCP Server
远程 MCP 端点，直接调用内存中的函数（不走 HTTP 自调自己）
通过 mount 到 FastAPI 应用提供 streamable HTTP transport
"""
import json
from mcp.server.fastmcp import FastMCP

import memory_ops
import corridor as corridor_mod
import daemon
from config import AI_ROLES, ROOMS, list_rooms

mcp = FastMCP(
    "Memory Hub",
    description="多AI角色共享记忆系统 - 存储、搜索、管理记忆",
    stateless_http=True,
)


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
    result = await memory_ops.remember(
        content=content, room=room, importance=importance,
        tags=tags, source_ai=source_ai, category=category,
        source_platform="mcp",
    )
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def recall(query: str, top_k: int = 5) -> str:
    """搜索记忆。用自然语言描述要找的内容，会用向量相似度匹配最相关的记忆。

    Args:
        query: 搜索关键词或自然语言描述
        top_k: 返回数量（默认5）
    """
    results = await memory_ops.recall(query=query, ai_id="claude", top_k=top_k)
    return json.dumps({"results": results}, ensure_ascii=False, indent=2)


@mcp.tool()
async def list_memories(
    room: str = "",
    status: str = "active",
    page: int = 1,
    per_page: int = 20,
) -> str:
    """列出记忆。可按房间、状态筛选。

    Args:
        room: 房间ID筛选（留空=全部）
        status: 状态筛选：active/archived/decayed
        page: 页码
        per_page: 每页数量
    """
    result = await memory_ops.list_memories(
        room=room or None, status=status, page=page, per_page=per_page,
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def update_memory(
    memory_id: str,
    content: str = "",
    importance: float = -1,
    room: str = "",
    tags: list[str] = [],
) -> str:
    """更新一条已有记忆。

    Args:
        memory_id: 记忆ID
        content: 新内容（留空=不改）
        importance: 新重要度（-1=不改）
        room: 移动到新房间（留空=不改）
        tags: 新标签（空列表=不改）
    """
    result = await memory_ops.update_memory(
        memory_id=memory_id,
        content=content or None,
        importance=importance if importance >= 0 else None,
        room=room or None,
        tags=tags or None,
        changed_by="claude",
    )
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def archive_memory(memory_id: str) -> str:
    """归档一条记忆（不删除，标记为archived）。

    Args:
        memory_id: 记忆ID
    """
    result = await memory_ops.archive_memory(memory_id)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def delete_memory(memory_id: str) -> str:
    """永久删除一条记忆。

    Args:
        memory_id: 记忆ID
    """
    result = await memory_ops.delete_memory(memory_id)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def get_corridor() -> str:
    """获取 Claude 的走廊文档 - AI醒来时读的第一份记忆上下文快照。"""
    text = await corridor_mod.get_corridor("claude")
    return text or "（走廊为空）"


@mcp.tool()
async def living_room() -> str:
    """获取客厅内容 - 核心身份和当前状态。"""
    items = await memory_ops.get_living_room()
    if not items:
        return "（客厅为空）"
    return json.dumps(items, ensure_ascii=False, indent=2)


@mcp.tool()
async def maintain() -> str:
    """执行记忆整理：合并相似记忆、压缩日记、衰减遗忘、重建走廊。"""
    result = await daemon.run_full_maintenance()
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def hub_info() -> str:
    """查看 Memory Hub 的角色和房间配置信息。"""
    rooms = list_rooms()
    data = {
        "roles": AI_ROLES,
        "rooms": {k: {"name": v["name"], "icon": v.get("icon", ""), "type": v.get("type", "")} for k, v in rooms.items()},
    }
    return json.dumps(data, ensure_ascii=False, indent=2)
