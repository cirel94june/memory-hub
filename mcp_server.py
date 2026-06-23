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
    category: str = "",
    importance: float = 0.5,
    source_ai: str = "claude",
    event_date: str = "",
    force_create: bool = False,
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
        category: 分类标签（留空则由系统自动分类。如果你传了，系统不会覆盖）
        importance: 重要度 0-1
        source_ai: 来源AI（claude/gemini/gpt）
        event_date: 事件发生日期（可选，如 2026-06-01，区别于记忆创建时间）
        force_create: 强制新建，跳过自动合并检测。当你确定这条记忆必须独立存在时使用
    """
    result = await memory_ops.remember(
        content=content, room=room, category=category, importance=importance,
        source_ai=source_ai, source_platform="mcp",
        event_date=event_date, force_create=force_create,
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
async def recall(query: str, top_k: int = 5, with_corridor: bool = False, source_ai: str = "claude", compact: bool = False) -> str:
    """搜索记忆。用自然语言描述要找的内容，会用向量相似度匹配最相关的记忆。

    Args:
        query: 搜索关键词或自然语言描述
        top_k: 返回数量（默认5）
        with_corridor: 是否同时返回走廊上下文（对话开头建议开启）
        source_ai: AI身份（影响私有房间可见性）
        compact: 精简模式。为 true 时只返回 id/content/room/score/created_at，减少上下文消耗。适合 MCP 调用场景。
    """
    results = await memory_ops.recall(query=query, ai_id=source_ai, top_k=top_k)
    if compact:
        results = [
            {k: item[k] for k in ("id", "content", "room", "score", "created_at") if k in item}
            for item in results
        ]
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

    # 9 维度情绪打标（fire-and-forget）
    import asyncio
    asyncio.ensure_future(gateway_mod._tag_pulse(user_message, source_ai))

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


@mcp.tool()
async def smart_context(
    ai_id: str,
    user_message: str = "",
    has_base_context: bool = False,
    max_chars: int = 3000,
) -> str:
    """获取智能上下文——根据 AI 前端的能力返回最合适的记忆注入。

    Args:
        ai_id: AI 标识（如 claude / lucien / jasper）
        user_message: 当前用户消息（可选，用于召回相关记忆）
        has_base_context: 该 AI 是否已有基础上下文（如 claude.ai 的 userMemories）。
            True = 只返回增量信息（最近变化 + 待办 + 相关记忆），更短更精准。
            False = 返回完整走廊 + recall，适合 TG bot 或无上下文的前端。
        max_chars: 返回文本的最大字符数（默认 3000）

    使用场景：
    - claude.ai 小克：smart_context(ai_id="claude", user_message="...", has_base_context=True)
    - TG bot / API 小克：smart_context(ai_id="claude", user_message="...", has_base_context=False)
    """
    from smart_context import get_smart_context
    result = await get_smart_context(ai_id, user_message, has_base_context, max_chars)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def batch_ops(
    action: str,
    filter_rules: dict,
    value: str = "",
) -> str:
    """批量操作记忆。

    action 支持：
    - "reset_activation": 重置 activation_count（value 为目标数值，默认 10）
    - "reclassify": 重新生成 category（无需 value）
    - "bulk_resolve": 设置 resolved 状态（value 为 "true" / "null"）
    - "bulk_archive": 批量归档（无需 value）

    filter_rules 支持的键：
    - "room": 按房间过滤
    - "activation_count_gt": activation_count 大于此值
    - "category_length_gt": category 长度大于此值
    - "source_platform_contains": source_platform 包含此字符串
    - "resolved": 按 resolved 过滤（true/false/null）
    - "importance_lt": importance 小于此值

    示例：
    - 重置虚高 activation：action="reset_activation", filter={"activation_count_gt": 50}, value="10"
    - 清理误标待办：action="bulk_resolve", filter={"room": "social", "resolved": false}, value="null"
    - 修复迁移 category：action="reclassify", filter={"category_length_gt": 20}

    Args:
        action: 操作类型
        filter_rules: 过滤条件
        value: 操作值（部分 action 需要）
    """
    from batch_ops import batch_operation

    parsed_value = None
    if value:
        if value.lower() in ("null", "none"):
            parsed_value = None
        elif value.lower() == "true":
            parsed_value = True
        elif value.lower() == "false":
            parsed_value = False
        else:
            try:
                parsed_value = int(value)
            except ValueError:
                parsed_value = value

    result = await batch_operation(action, filter_rules, parsed_value)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
async def extract_from_messages(
    messages: list[dict],
    ai_id: str = "claude",
    chat_type: str = "private",
) -> str:
    """从对话消息中自动提取值得长期记住的信息并存储。

    适合在对话结束时调用，把整段对话交给系统自动提取记忆。
    比手动 remember 更方便——系统会判断哪些值得记、哪些不值得。

    Args:
        messages: 对话消息数组，格式 [{"role": "user"/"assistant", "content": "..."}]
        ai_id: 调用方的 AI 标识
        chat_type: "private" / "private_group" / "public_group"，影响提取策略
    """
    from conversation_capture import extract_from_messages as _extract
    results = await _extract(messages, ai_id, chat_type, quick=True)
    return json.dumps(results, ensure_ascii=False, indent=2)


@mcp.tool()
async def search_by_tags(
    tags: list[str],
    mode: str = "any",
    room: str = "",
    limit: int = 20,
) -> str:
    """按标签搜索记忆。比 recall 更精确——直接匹配标签字段，不走语义模糊搜索。

    适用场景：
    - 找所有tag含"母亲"的记忆 → tags=["母亲"]
    - 找同时有"NPD"和"创伤"标签的 → tags=["NPD", "创伤"], mode="all"
    - 审计某个房间的标签分布 → room="psychology", tags=["创伤"]

    Args:
        tags: 要搜索的标签列表（子串匹配，大小写不敏感）
        mode: "any"=匹配任一标签（默认），"all"=要求全部匹配
        room: 限定房间（留空=全部）
        limit: 最多返回条数
    """
    results = await memory_ops.search_by_tags(tags=tags, mode=mode, room=room, limit=limit)
    return json.dumps({"count": len(results), "results": results}, ensure_ascii=False, indent=2)


@mcp.tool()
async def batch_remember(
    memories: list[dict],
    source_ai: str = "claude",
) -> str:
    """批量存储多条记忆，一次调用完成。

    每条记忆支持的字段：
    - content (必填): 记忆内容
    - room: 房间ID（默认 living_room）
    - category: 分类标签
    - importance: 重要度 0-1
    - tags: 标签列表
    - event_date: 事件日期
    - force_create: 强制新建，跳过合并

    示例：memories=[
        {"content": "xxx", "room": "psychology", "importance": 0.8},
        {"content": "yyy", "room": "career", "force_create": true}
    ]

    Args:
        memories: 记忆列表，每条是一个dict
        source_ai: 来源AI
    """
    result = await memory_ops.batch_remember(memories=memories, source_ai=source_ai)
    summary = f"{result['total']}条|新{result['created']}合{result['merged']}跳{result['skipped']}"
    result["summary"] = summary
    return json.dumps(result, ensure_ascii=False, indent=2)
