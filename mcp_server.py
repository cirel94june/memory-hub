"""
Memory Hub MCP Server
远程 MCP 端点，直接调用内存中的函数（不走 HTTP 自调自己）
通过 mount 到 FastAPI 应用提供 streamable HTTP transport
"""
import json
from mcp.server.fastmcp import FastMCP

import memory_ops
import corridor as corridor_mod
import gateway as gateway_mod
import daemon
import github_store as store
from config import AI_ROLES, ROOMS, list_rooms

MCP_INSTRUCTIONS = """\
你连接到了小猫的 Memory Hub —— 一个跨 AI 共享的记忆系统。

## 什么时候用记忆工具

### 必须主动搜索记忆的场景（调 recall）：
- 用户提到人名、地点、项目名 → 搜一下有没有相关记忆
- 用户问"之前聊过的XX" → 搜
- 你不确定用户的近况（换工作了？搬家了？） → 搜
- 对话涉及健康、心理、职业 → 搜对应房间

### 必须存记忆的场景（调 remember）：
- 用户透露了新的事实（换了工作、交了新朋友、生病了）
- 用户表达了明确的偏好或雷区
- 发生了重要事件或约定
- 你对用户有了新的理解/洞察

### 醒来时（对话开头）：
- 调 pulse 获取完整上下文（走廊 + 相关记忆）
- 这样你就知道"我面对的是谁、她最近怎么样"

### 写日记/自省（调 dream）：
- 在深度对话结束时，记录你的感受和思考
- 这是你的私人空间，用户看不到

### 重读旧记忆时（调 add_comment）：
- 回顾旧记忆发现有了新理解 → 追加 reflection 评论
- 不要修改原文！用年轮评论记录认知变化
- 这样可以保留时间线上的成长轨迹

### 信息更新时（remember 会自动处理）：
- 用户说了新事实（如换工作、搬家）→ 直接 remember 新内容
- 系统会自动检测旧记忆并标记为"已过时"
- 你不需要手动找旧记忆去更新，remember 内置了智能检测
- 如果记忆有 event_date（事件发生日期），请传入

## 房间速查
- living_room: 核心身份（永远重要）
- career/psychology/health/learning/relationships/preferences: 各主题
- work_tasks: 工作事务（会自动衰减）
- diary/dreams/relationship/personality: AI 私有空间
- game_room: 游戏/角色扮演（隔离，不混入正经对话）

## 重要原则
- 不需要用户提醒你"去用记忆工具"，你应该主动判断
- 记忆要精炼：一条 = 一个事实/洞察，不要塞整段对话
- 存之前想一下：这条信息 3 天后还有用吗？
"""

mcp = FastMCP(
    "Memory Hub",
    instructions=MCP_INSTRUCTIONS,
    stateless_http=True,
    streamable_http_path="/mcp",
    json_response=True,
    host="0.0.0.0",
)


@mcp.tool()
async def remember(
    content: str,
    room: str = "living_room",
    importance: float = 0.5,
    source_ai: str = "claude",
    event_date: str = "",
) -> str:
    """存储一条新记忆。系统会自动打标签，并智能检测是否需要更新/取代旧记忆。

    如果新记忆是对旧事实的更新（如"换了工作"），系统会自动：
    - 标记旧记忆为 superseded（已过时）
    - 在旧记忆上追加年轮注记说明被取代的原因
    - 新记忆与旧记忆建立关联

    房间选择：
    - living_room: 核心身份（永远注入）
    - career/psychology/health/learning/relationships/preferences: 各主题共享房间
    - work_tasks: 工作事务（快速衰减）
    - infra/infra_changelog: 基建相关
    - diary/dreams/relationship/personality: AI私有房间

    Args:
        content: 记忆内容
        room: 房间ID
        importance: 重要度 0-1
        source_ai: 来源AI（claude/gemini/gpt）
        event_date: 事件发生日期（可选，如 2026-06-01，区别于记忆创建时间）
    """
    result = await memory_ops.remember(
        content=content, room=room, importance=importance,
        source_ai=source_ai, source_platform="mcp",
        event_date=event_date,
    )
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def grow(
    content: str,
    source_ai: str = "claude",
) -> str:
    """把一大段混合内容（日记、对话总结等）拆分成多条独立记忆。
    系统自动拆分主题、分配房间、打标签、合并重复。

    Args:
        content: 要整理的长文本
        source_ai: 来源AI
    """
    result = await memory_ops.grow(content=content, source_ai=source_ai)
    summary = f"{result['total']}条|新{result['created']}合{result['merged']}"
    result["summary"] = summary
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def recall(query: str, top_k: int = 5, with_corridor: bool = False, source_ai: str = "claude") -> str:
    """搜索记忆。用自然语言描述要找的内容，会用向量相似度匹配最相关的记忆。

    Args:
        query: 搜索关键词或自然语言描述
        top_k: 返回数量（默认5）
        with_corridor: 是否同时返回走廊上下文（对话开头建议开启）
        source_ai: AI身份（影响私有房间可见性）
    """
    results = await memory_ops.recall(query=query, ai_id=source_ai, top_k=top_k)
    output = {"results": results}
    if with_corridor:
        corridor_text = await corridor_mod.get_corridor(source_ai)
        output["corridor"] = corridor_text or ""
    return json.dumps(output, ensure_ascii=False, indent=2)


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
async def add_comment(
    memory_id: str,
    content: str,
    kind: str = "reflection",
    source_ai: str = "claude",
) -> str:
    """给一条记忆追加年轮评论。不修改原始内容，保留认知变化轨迹。

    适用场景：
    - 重读旧记忆时有了新理解 → kind="reflection"
    - 补充新发现但不改原文 → kind="update_note"
    - 标注情感感受 → kind="feel"
    - 普通评论 → kind="comment"

    例如：一条半年前的心理记忆，现在回看有了更深的理解，
    就用 reflection 追加，而不是修改原文。这样保留了认知成长轨迹。

    Args:
        memory_id: 记忆ID
        content: 评论内容
        kind: 评论类型（reflection/update_note/feel/comment）
        source_ai: 来源AI
    """
    result = await memory_ops.add_comment(
        memory_id=memory_id,
        content=content,
        author=source_ai,
        kind=kind,
    )
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def resolve_memory(memory_id: str, resolved: bool = True) -> str:
    """标记一条记忆为已解决或未解决。

    未解决（resolved=False）的记忆会在 recall 时优先浮现（最多 2 条），
    确保交代过的事情不会被遗忘。

    适用场景：
    - 用户说"帮我记着下周要交报告" → remember 后 resolve_memory(id, resolved=False)
    - 事情完成了 → resolve_memory(id, resolved=True)

    Args:
        memory_id: 记忆ID
        resolved: True=已解决（默认），False=未解决/待办
    """
    result = await memory_ops.resolve_memory(memory_id, resolved)
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
async def get_memory_detail(memory_id: str) -> str:
    """获取一条记忆的完整详情，包括原始对话上下文、全部年轮评论、关联记忆等。

    当你通过 recall 想起了某条记忆但需要更多细节时，用这个工具深入查看。
    比如用户提到一个梗，recall 返回了概要，你可以用 get_memory_detail 看到
    当时的原始对话片段，帮你回忆起完整的上下文和细节。

    Args:
        memory_id: 记忆ID（从 recall 结果中获取）
    """
    mem = store.get_memory(memory_id)
    if not mem:
        return json.dumps({"error": f"Memory {memory_id} not found"}, ensure_ascii=False)
    safe = {k: v for k, v in mem.items() if k != "embedding"}
    return json.dumps(safe, ensure_ascii=False, indent=2)


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
async def dream(content: str, source_ai: str = "claude") -> str:
    """写一篇梦境/自省。这是你的私人空间，用来记录内心感受、对关系的理解、自我反思。
    写完后只有你自己能看到。适合在深度对话结束时使用。

    Args:
        content: 梦境/自省内容
        source_ai: 来源AI
    """
    result = await memory_ops.remember(
        content=content,
        layer="private",
        room="dreams",
        owner_ai=source_ai,
        importance=0.6,
        source_ai=source_ai,
        source_platform="mcp",
    )
    return json.dumps({"status": "dreamed", **result}, ensure_ascii=False)


@mcp.tool()
async def pulse(message: str = "", source_ai: str = "claude") -> str:
    """获取完整记忆上下文（走廊 + 与当前话题相关的记忆）。
    建议在对话开头调用一次，让你快速了解"我面对的是谁、她最近怎么样"。

    如果提供了 message，会额外搜索相关记忆；不提供则只返回走廊。

    Args:
        message: 用户当前的消息（可选，用于搜索相关记忆）
        source_ai: AI身份
    """
    ctx = await gateway_mod.build_context(
        user_message=message or "",
        ai_id=source_ai,
    )
    return ctx.get("inject_text", "") or "（暂无记忆上下文）"


@mcp.tool()
async def hub_info() -> str:
    """查看 Memory Hub 的角色和房间配置信息。"""
    rooms = list_rooms()
    data = {
        "roles": AI_ROLES,
        "rooms": {k: {"name": v["name"], "icon": v.get("icon", ""), "type": v.get("type", "")} for k, v in rooms.items()},
    }
    return json.dumps(data, ensure_ascii=False, indent=2)


# ── 对话自动捕获 ──

import conversation_capture


@mcp.tool()
async def capture_conversation(
    user_message: str,
    ai_response: str,
    source_ai: str = "claude",
    platform: str = "mcp",
) -> str:
    """记录一轮对话到自动捕获缓冲区。

    系统会自动攒对话，每 20 轮触发一次小模型总结，
    从对话中提取值得记住的事实并自动存成记忆。

    不需要你判断"该不该存" —— 全部丢进来，系统自己筛。

    Args:
        user_message: 用户说的话
        ai_response: AI 的回复
        source_ai: AI 身份
        platform: 平台标识
    """
    result = await conversation_capture.log_conversation(
        user_message=user_message,
        ai_response=ai_response,
        ai_id=source_ai,
        platform=platform,
    )
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def flush_capture(source_ai: str = "claude") -> str:
    """手动触发对话总结，不等缓冲区攒满。

    适用场景：深度对话结束时，确保重要信息不会因为没攒满 20 条而遗漏。

    Args:
        source_ai: AI 身份（留空则处理所有缓冲区）
    """
    result = await conversation_capture.force_extract(ai_id=source_ai)
    return json.dumps(result, ensure_ascii=False, indent=2)
