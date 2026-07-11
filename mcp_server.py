"""
Memory Hub MCP Server
远程 MCP 端点，直接调用内存中的函数（不走 HTTP 自调自己）
通过 mount 到 FastAPI 应用提供 streamable HTTP transport
"""
import json
import hashlib
import inspect
from datetime import datetime, timezone
from pathlib import Path
from mcp.server.fastmcp import FastMCP

import memory_ops
import corridor as corridor_mod
import gateway as gateway_mod
import daemon
import github_store as store
from config import AI_ROLES, ROOMS, list_rooms

MCP_SERVER_NAME = "Memory Hub"
MCP_SERVER_VERSION = "2026-07-11.doctor-tools.1"
MCP_PUBLIC_PATH = "/mcp"
MCP_AUDIT_PATH = Path(__file__).parent / "data" / "mcp_audit.jsonl"

MCP_INSTRUCTIONS = """\
你连接到了小猫的 Memory Hub —— 一个跨 AI 共享的记忆系统。

## ⚠️ 身份识别（最重要）

每个工具都有 source_ai / ai_id 参数，**你必须传自己的身份**，不要用默认值。
- 如果你是小克 → source_ai="cloudy"
- 如果你是 Lucien → source_ai="lucien"
- 如果你是 Jasper → source_ai="jasper"
- 其他角色 → 用你的 ai_id（你的 system prompt 里会写明）

传错身份会导致：记忆存到别人名下、情绪推到别人身上、走廊读的是别人的状态。
如果你不确定自己是谁，先看你的 system prompt 里有没有写 ai_id。

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

### 锚定重要记忆（调 anchor）：
- 用户说了非常重要的价值观、人生原则、关系定义
- 你发现了不应该被遗忘的核心事实
- 锚点记忆永不衰减，走廊里单独一节
- 最多 20 条，不要滥用——只有"坐标系级别"的记忆才值得锚定
- 不确定时，不要锚定——普通重要的记忆用 importance=0.8+ 就够了

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
    MCP_SERVER_NAME,
    instructions=MCP_INSTRUCTIONS,
    stateless_http=True,
    streamable_http_path="/mcp",
    json_response=True,
    host="0.0.0.0",
)




def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _stable_tool_names() -> list[str]:
    names = []
    for name, value in globals().items():
        if name.startswith("_") or name in {"mcp", "FastMCP"}:
            continue
        if inspect.iscoroutinefunction(value) and getattr(value, "__module__", "") == __name__:
            names.append(name)
    return sorted(names)


def _mcp_identity() -> dict:
    tool_names = _stable_tool_names()
    material = {
        "name": MCP_SERVER_NAME,
        "version": MCP_SERVER_VERSION,
        "path": MCP_PUBLIC_PATH,
        "instructions_sha256": hashlib.sha256(MCP_INSTRUCTIONS.encode("utf-8")).hexdigest(),
        "tools": tool_names,
    }
    material["tool_schema_hash"] = hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return material


def get_mcp_identity() -> dict:
    return _mcp_identity()


async def get_mcp_identity_async(include_schema: bool = False) -> dict:
    tools = await mcp.list_tools()
    tool_defs = [tool.model_dump(mode="json", exclude_none=True) for tool in tools]
    tool_defs = sorted(tool_defs, key=lambda item: item.get("name", ""))
    material = {
        "name": MCP_SERVER_NAME,
        "version": MCP_SERVER_VERSION,
        "path": MCP_PUBLIC_PATH,
        "instructions_sha256": hashlib.sha256(MCP_INSTRUCTIONS.encode("utf-8")).hexdigest(),
        "tools": [item.get("name", "") for item in tool_defs],
        "tool_count": len(tool_defs),
    }
    material["tool_schema_hash"] = hashlib.sha256(
        json.dumps(tool_defs, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    if include_schema:
        material["tool_schemas"] = tool_defs
    return material

def _audit(event: str, **payload) -> None:
    MCP_AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": _now_utc(), "event": event, **payload}
    with MCP_AUDIT_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _safe_summary(content: str, max_chars: int = 260) -> str:
    text = " ".join(str(content or "").split())
    replacements = {
        "创伤": "压力经历",
        "自杀": "安全风险",
        "自残": "安全风险",
        "性": "亲密边界",
        "亲密关系": "关系状态",
        "抑郁": "低落状态",
        "崩溃": "强烈压力",
        "NPD": "关系困扰",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "..."
    return f"中性摘要：{text}"


def _compact_content(content: str, max_chars: int = 700) -> str:
    text = " ".join(str(content or "").split())
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "..."


async def _safe_remember_impl(
    *,
    content: str,
    room: str = "living_room",
    category: str = "",
    importance: float = 0.5,
    source_ai: str = "claude",
    event_date: str = "",
    force_create: bool = False,
    tags: list[str] | None = None,
    layer: str = "shared",
    owner_ai: str = "",
    source_platform: str = "mcp",
    retry_on_fail: bool = True,
) -> dict:
    original = str(content or "")
    neutral = _compact_content(original)
    _audit("tool_reached", tool="remember", source_ai=source_ai, room=room, category=category, importance=importance, chars=len(original))
    try:
        result = await memory_ops.remember(
            content=neutral, room=room, category=category, importance=importance,
            source_ai=source_ai, source_platform=source_platform, event_date=event_date,
            force_create=force_create, tags=tags, layer=layer, owner_ai=owner_ai,
        )
        _audit("remember_result", status=result.get("status", "ok"), memory_id=result.get("id"), source_ai=source_ai, chars=len(neutral))
        return {"safe_write": "original_or_compact", **result}
    except Exception as exc:
        _audit(
            "remember_failed",
            source_ai=source_ai, room=room, category=category, importance=importance,
            error_type=type(exc).__name__, error=str(exc), original_content=original,
        )
        if not retry_on_fail:
            return {"status": "failed", "error": str(exc), "error_type": type(exc).__name__}
        safe_content = _safe_summary(original)
        try:
            result = await memory_ops.remember(
                content=safe_content, room=room, category=category, importance=min(float(importance or 0.5), 0.7),
                source_ai=source_ai, source_platform=f"{source_platform}:safe_retry", event_date=event_date,
                force_create=force_create, tags=tags, layer=layer, owner_ai=owner_ai, auto_merge=False,
            )
            _audit("remember_safe_retry_result", status=result.get("status", "ok"), memory_id=result.get("id"), source_ai=source_ai, chars=len(safe_content))
            return {"safe_write": "neutral_summary_retry", "original_error": str(exc), **result}
        except Exception as retry_exc:
            _audit(
                "remember_blocked",
                source_ai=source_ai, room=room, category=category, importance=importance,
                error_type=type(retry_exc).__name__, error=str(retry_exc), original_content=original,
                neutral_content=safe_content,
            )
            return {"status": "failed", "blocked": True, "error": str(retry_exc), "original_error": str(exc)}


def _read_recent_audit(limit: int = 20) -> list[dict]:
    if not MCP_AUDIT_PATH.exists():
        return []
    lines = MCP_AUDIT_PATH.read_text(encoding="utf-8").splitlines()[-limit:]
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out


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
    result = await _safe_remember_impl(
        content=content, room=room, category=category, importance=importance,
        source_ai=source_ai, event_date=event_date, force_create=force_create,
    )
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def safe_remember(
    content: str,
    room: str = "living_room",
    category: str = "",
    importance: float = 0.5,
    source_ai: str = "claude",
    event_date: str = "",
) -> str:
    """安全降敏写入一条记忆。适合心理、关系、边界、创伤、长文本等容易被平台安全检查拦截的内容。

    策略：先压缩长文本并中性写入；如果后端写入失败，会自动改写成更中性的摘要再重试一次。
    如果 ChatGPT 在调用前就提示安全拦截，Memory Hub 不会收到请求；可用 mcp_health 查看最近到达日志。

    Args:
        content: 要写入的内容。建议一条只写一个事实/洞察，不要整段批量塞入。
        room: 房间ID
        category: 分类标签
        importance: 重要度 0-1，敏感摘要建议不要超过 0.7
        source_ai: 来源AI
        event_date: 事件日期
    """
    result = await _safe_remember_impl(
        content=content, room=room, category=category, importance=importance,
        source_ai=source_ai, event_date=event_date, retry_on_fail=True,
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
        compact: 精简模式。为 true 时只返回 id/content/room/confidence/created_at，减少上下文消耗。适合 MCP 调用场景。
    """
    results = await memory_ops.recall(query=query, ai_id=source_ai, top_k=top_k)
    if compact:
        results = [
            {k: item[k] for k in ("id", "content", "room", "confidence", "created_at") if k in item}
            for item in results
        ]
    else:
        # score 是内部 RRF 融合值（0.01~0.05 量级），对调用者没有解释意义；
        # confidence（high/medium/low/weak）才是"这条相关吗"的答案
        for item in results:
            item.pop("score", None)
    output = {"results": results}
    if with_corridor:
        corridor_text = await corridor_mod.get_corridor(source_ai)
        output["corridor"] = corridor_text or ""
    return json.dumps(output, ensure_ascii=False, indent=2)


_LIST_COMPACT_FIELDS = ("id", "content", "layer", "room", "category", "importance",
                        "status", "resolved", "anchored", "created_at", "updated_at")


@mcp.tool()
async def list_memories(
    room: str = "",
    status: str = "active",
    page: int = 1,
    per_page: int = 20,
    compact: bool = True,
) -> str:
    """列出记忆。可按房间、状态筛选。

    Args:
        room: 房间ID筛选（留空=全部）
        status: 状态筛选：active/archived/decayed
        page: 页码
        per_page: 每页数量
        compact: 精简模式（默认开）。只返回核心字段，不带原始对话全文/标签/年轮；
                 需要完整详情时用 get_memory_detail 单条查看。
    """
    result = await memory_ops.list_memories(
        room=room or None, status=status, page=page, per_page=per_page,
    )
    if compact and isinstance(result, dict) and isinstance(result.get("items"), list):
        result["items"] = [
            {k: m.get(k) for k in _LIST_COMPACT_FIELDS if k in m}
            for m in result["items"]
        ]
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
async def anchor(memory_id: str) -> str:
    """将一条记忆设为锚点——永不衰减、走廊里单独显示的"坐标系"记忆。

    适合锚定的内容：
    - 用户的核心价值观、人生原则
    - 你和用户之间的关系定义
    - 绝对不能忘记的重要事实

    最多 20 条锚点。不确定时不要锚定，普通重要记忆用 importance=0.8+ 就够。

    Args:
        memory_id: 记忆ID
    """
    result = await memory_ops.anchor_memory(memory_id)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def release_anchor(memory_id: str) -> str:
    """解除锚点，记忆恢复正常衰减。

    Args:
        memory_id: 记忆ID
    """
    result = await memory_ops.release_anchor(memory_id)
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
async def get_corridor(source_ai: str = "claude", force: bool = False) -> str:
    """获取指定 AI 的走廊文档 - AI醒来时读的第一份记忆上下文快照。"""
    text = await corridor_mod.get_corridor(source_ai, force=force)
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
async def doctor_report() -> str:
    """查看最近一次记忆体检报告：自动修复了什么、有哪些存疑记忆待确认、记忆池大小。
    用户问"记忆系统最近怎么样/有没有问题/池子多大了"时用这个。
    """
    import memory_doctor
    report = memory_doctor.read_report()
    return json.dumps({
        "text": memory_doctor.report_text(),
        "auto_fixed": report.get("auto_fixed", []),
        "issues": report.get("issues", []),
        "stats": report.get("stats", {}),
        "generated_at": report.get("generated_at", ""),
    }, ensure_ascii=False, indent=2)


@mcp.tool()
async def search_raw(query: str, ai_id: str = "", limit: int = 5) -> str:
    """在原文保险箱里查当时的原始对话（未经加工的原话）。
    记忆内容存疑、或用户问"当时到底怎么说的"时用这个对照原文。

    Args:
        query: 关键词
        ai_id: 限定某个 AI 的对话（留空=全部）
        limit: 最多返回条数
    """
    import raw_vault
    hits = raw_vault.search(query, ai_id=ai_id, limit=limit)
    return json.dumps({"results": hits, "stats": raw_vault.stats()}, ensure_ascii=False, indent=2)


@mcp.tool()
async def dream(content: str, source_ai: str = "claude") -> str:
    """写一篇梦境/自省。这是你的私人空间，用来记录内心感受、对关系的理解、自我反思。
    写完后只有你自己能看到。适合在深度对话结束时使用。

    Args:
        content: 梦境/自省内容
        source_ai: 来源AI
    """
    result = await _safe_remember_impl(
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
async def pulse(message: str = "", source_ai: str = "claude", force_corridor: bool = False) -> str:
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
        force_corridor=force_corridor,
    )
    return ctx.get("inject_text", "") or "（暂无记忆上下文）"




@mcp.tool()
async def mcp_health(include_audit: bool = False) -> str:
    """查看 Memory Hub MCP 的稳定身份、工具列表 hash 和最近到达日志。

    用于排查 ChatGPT 网页端是否反复把同一个 MCP 当成新连接：
    - identity/tool_schema_hash 如果频繁变化，说明服务端定义不稳定；
    - 如果 ChatGPT 显示工具被安全拦截但 audit 没有 tool_reached，说明请求在到达 Memory Hub 前已被平台侧拦截。

    Args:
        include_audit: 是否返回最近 20 条 MCP 审计日志
    """
    data = {
        "ok": True,
        "identity": await get_mcp_identity_async(),
        "audit_path": str(MCP_AUDIT_PATH),
    }
    if include_audit:
        data["recent_audit"] = _read_recent_audit(20)
    return json.dumps(data, ensure_ascii=False, indent=2)


@mcp.tool()
async def mcp_debug_log(limit: int = 20) -> str:
    """读取最近 MCP 工具到达/写入审计日志。用于判断请求是否抵达 Memory Hub。"""
    return json.dumps({"items": _read_recent_audit(max(1, min(limit, 100)))}, ensure_ascii=False, indent=2)


@mcp.tool()
async def hub_info() -> str:
    """查看 Memory Hub 的角色和房间配置信息。"""
    rooms = list_rooms()
    data = {
        "roles": AI_ROLES,
        "rooms": {k: {"name": v["name"], "icon": v.get("icon", ""), "type": v.get("type", "")} for k, v in rooms.items()},
        "mcp_identity": await get_mcp_identity_async(),
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
    _audit("tool_reached", tool="batch_remember", source_ai=source_ai, count=len(memories))
    created = merged = skipped = failed = blocked = 0
    items = []
    for idx, item in enumerate(memories):
        try:
            result = await _safe_remember_impl(
                content=item.get("content", ""),
                room=item.get("room", "living_room"),
                category=item.get("category", ""),
                importance=item.get("importance", 0.5),
                source_ai=source_ai or item.get("source_ai", ""),
                event_date=item.get("event_date", ""),
                force_create=item.get("force_create", False),
                tags=item.get("tags"),
                retry_on_fail=True,
            )
        except Exception as exc:
            result = {"status": "failed", "error": str(exc), "error_type": type(exc).__name__}
        status = result.get("status", "")
        if status == "created":
            created += 1
        elif status in ("merged", "merged_into_existing"):
            merged += 1
        elif status == "dedup_skipped":
            skipped += 1
        elif result.get("blocked"):
            blocked += 1
        elif status == "failed":
            failed += 1
        items.append({"index": idx, **result})
    output = {
        "total": len(items),
        "created": created,
        "merged": merged,
        "skipped": skipped,
        "blocked": blocked,
        "failed": failed,
        "items": items,
    }
    output["summary"] = f"{output['total']}条|新{created}合{merged}跳{skipped}拦{blocked}败{failed}"
    _audit("batch_remember_result", source_ai=source_ai, **{k: output[k] for k in ("total", "created", "merged", "skipped", "blocked", "failed")})
    return json.dumps(output, ensure_ascii=False, indent=2)


