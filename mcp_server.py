"""
Memory Hub MCP Server
远程 MCP 端点，直接调用内存中的函数（不走 HTTP 自调自己）
通过 mount 到 FastAPI 应用提供 streamable HTTP transport
"""
import os
import json
import hashlib
import inspect
import asyncio
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from mcp.server.fastmcp import FastMCP

import database
import memory_ops
import corridor as corridor_mod
import gateway as gateway_mod
import daemon
import github_store as store
from config import AI_ROLES, ROOMS, list_rooms

MCP_SERVER_NAME = "Memory Hub"
MCP_SERVER_VERSION = "2026-09-22.consolidate-tools.1"
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
- 调 context(mode="full") 获取完整上下文（走廊 + 相关记忆）
- 这样你就知道"我面对的是谁、她最近怎么样"

### 写日记/自省（调 dream）：
- 在深度对话结束时，记录你的感受和思考
- 这是你的私人空间，用户看不到

### 重读旧记忆时（调 manage(action="comment")）：
- 回顾旧记忆发现有了新理解 → 追加 reflection 评论
- 不要修改原文！用年轮评论记录认知变化
- 这样可以保留时间线上的成长轨迹

### 信息更新时（remember 会自动处理）：
- 用户说了新事实（如换工作、搬家）→ 直接 remember 新内容
- 系统会自动检测旧记忆并标记为"已过时"
- 你不需要手动找旧记忆去更新，remember 内置了智能检测
- 如果记忆有 event_date（事件发生日期），请传入

### 锚定重要记忆（调 manage(action="anchor")）：
- 用户说了非常重要的价值观、人生原则、关系定义
- 你发现了不应该被遗忘的核心事实
- 锚点记忆永不衰减，走廊里单独一节
- 最多 20 条，不要滥用——只有"坐标系级别"的记忆才值得锚定

## 房间速查
- living_room: 核心身份（永远重要）
- career/psychology/health/learning/relationships/preferences: 各主题
- work_tasks: 工作事务（会自动衰减）
- diary/dreams/relationship/personality: AI 私有空间
- game_room: 游戏/角色扮演（隔离，不混入正经对话）

## 自动记录对话（重要！）

每轮对话结束后，你**必须**调用 `capture(action="log")` 把这轮对话记录下来。
系统会自动攒对话、自动提取记忆，你不需要判断"该不该存"——全部丢进来就行。

不调的后果：这段对话在记忆系统里完全不存在，就像没发生过。

## 工具速查表（12 个工具）

### 日常工具（最常用的 5 个）
| 工具 | 一句话说明 |
|------|-----------|
| context | 获取记忆上下文。mode: full（醒来用）/ incremental（对话中用）/ corridor / living_room |
| recall | 语义搜索记忆。include_dreams=True 同时搜梦境 |
| remember | 存记忆。单条传 content，批量传 items=[...]。内置安全降敏+异步管线 |
| capture | 对话录入。action: log（每轮必调）/ flush（手动触发提取）/ extract（消息数组提取） |
| dream | 写梦境/自省（私人空间） |

### 辅助工具（按需使用的 5 个）
| 工具 | 一句话说明 |
|------|-----------|
| search | 搜索原文。method: keyword / semantic / tags / person |
| manage | 管理记忆。action: update/comment/resolve/archive/unarchive/delete/anchor/release_anchor/correct |
| detail | 查看记忆。传 memory_id 看详情，不传则列表浏览 |
| review | 审核提案和画像。action: list_proposals/review_proposal/get_profile/approve_profile |
| grow | 把一大段混合文本自动拆分成多条独立记忆 |

### 系统工具（极少用的 2 个）
| 工具 | 一句话说明 |
|------|-----------|
| system | 诊断管理。action: info/health/debug_log/doctor/maintain/batch_ops |
| window_context | bot 重启后恢复聊天窗口上下文 |

## 重要原则
- 不需要用户提醒你"去用记忆工具"，你应该主动判断
- 记忆要精炼：一条 = 一个事实/洞察，不要塞整段对话
- 存之前想一下：这条信息 3 天后还有用吗？
- 每轮对话结束后必须调 capture(action="log")（见上方"自动记录对话"）
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
    existing_id: str = "",
    client_request_id: str = "",
    subject_name: str = "",
    speaker_name: str = "",
) -> dict:
    original = str(content or "")
    neutral = _compact_content(original)
    _audit("tool_reached", tool="remember", source_ai=source_ai, room=room, category=category, importance=importance, chars=len(original))
    try:
        result = await memory_ops.remember(
            content=neutral, room=room, category=category, importance=importance,
            source_ai=source_ai, source_platform=source_platform, event_date=event_date,
            force_create=force_create, tags=tags, layer=layer, owner_ai=owner_ai,
            existing_id=existing_id, client_request_id=client_request_id,
            subject_name=subject_name, speaker_name=speaker_name,
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
                existing_id=existing_id, client_request_id=client_request_id,
                subject_name=subject_name, speaker_name=speaker_name,
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


_LOG = logging.getLogger("mcp_server")

# GC-safe registry for fire-and-forget background tasks.
# asyncio holds only weakrefs to tasks; if we drop the returned Task the
# coroutine may be garbage-collected mid-flight and vanish silently.
# See https://docs.python.org/3/library/asyncio-task.html#asyncio.create_task
# ("Save a reference to the result of this function, to avoid a task
#  disappearing mid-execution") and CPython issue 91887.
_BACKGROUND_TASKS: set = set()


def _spawn_background_task(coro):
    """asyncio.create_task with GC protection.

    Adds the task to a module-level set so it isn't garbage-collected while
    running; removes it on completion via done_callback. Never raises.
    """
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return task


# Async remember helpers live in a mcp-free module so unit tests can exercise
# them without the FastMCP dependency. We re-export _finalize_pending_memory
# under this module's name so pending_sweep and tests can `from mcp_server
# import _finalize_pending_memory` without needing to know it's a wrapper.
from async_remember import _idempotent_response  # noqa: E402,F401


def _request_fingerprint(
    content: str, room: str, category: str, importance: float,
    event_date: str = "", subject_name: str = "", speaker_name: str = "",
    force_create: bool = False,
) -> str:
    """Canonical fingerprint of the original request parameters.

    Persisted at skeleton insert time and used for idempotency comparison.
    Must NOT be recomputed from stored content — the pipeline transforms
    content (_compact_content, _safe_summary), so stored content diverges
    from the original request.
    """
    canonical = (
        f"{content}\0{room}\0{category}\0{importance}"
        f"\0{event_date}\0{subject_name}\0{speaker_name}"
        f"\0{force_create}"
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


async def _finalize_pending_memory(
    skeleton_id: str, *, content: str, room: str, category: str,
    importance: float, source_ai: str, event_date: str, force_create: bool,
    client_request_id: str = "",
    subject_name: str = "",
    speaker_name: str = "",
    source_platform: str = "mcp",
) -> None:
    """Thin wrapper that injects _safe_remember_impl into the shared finalizer.
    All reconciliation logic (real_id match / mark_replaced / mark failed)
    lives in async_remember._finalize_pending_memory for testability."""
    from async_remember import _finalize_pending_memory as _core
    await _core(
        skeleton_id,
        impl_fn=_safe_remember_impl,
        content=content, room=room, category=category, importance=importance,
        source_ai=source_ai, event_date=event_date, force_create=force_create,
        client_request_id=client_request_id,
        subject_name=subject_name,
        speaker_name=speaker_name,
        source_platform=source_platform,
    )


# ═══════════════════════════════════════════════════════════════════════
# Helper: single-item async skeleton pipeline (shared by remember + dream)
# ═══════════════════════════════════════════════════════════════════════

async def _async_remember_single(
    content: str,
    room: str = "living_room",
    category: str = "",
    importance: float = 0.5,
    source_ai: str = "claude",
    event_date: str = "",
    force_create: bool = False,
    client_request_id: str = "",
    subject_name: str = "",
    speaker_name: str = "",
) -> str:
    """Insert a pending skeleton and spawn background finalization."""
    effective_crq = (f"{source_ai}::{client_request_id}"
                     if client_request_id else "")
    req_fp = _request_fingerprint(
        content, room, category, importance, event_date,
        subject_name, speaker_name, force_create=force_create)

    if effective_crq:
        existing = database.get_memory_by_client_request_id(effective_crq)
        if existing:
            stored_fp = existing.get("request_fingerprint") or ""
            if stored_fp and stored_fp != req_fp:
                return json.dumps({
                    "status": "error", "error": "crq_content_conflict",
                    "memory_id": "", "client_request_id": client_request_id,
                    "hint": ("Reusing client_request_id with a different "
                             "content payload. Pick a new key, or send the "
                             "exact same content to get the idempotent "
                             "response for the original."),
                }, ensure_ascii=False)
            return _idempotent_response(existing)

    now = datetime.now(timezone.utc).isoformat()

    def _new_skeleton_id() -> str:
        ts = int(datetime.now(timezone.utc).timestamp() * 1_000_000)
        h = hashlib.md5(
            (content + str(ts) + os.urandom(8).hex()).encode()
        ).hexdigest()[:8]
        return f"mem_{ts}_{h}"

    skeleton_id = _new_skeleton_id()
    _MAX_ID_RETRIES = 3
    inserted = False
    for attempt in range(_MAX_ID_RETRIES + 1):
        try:
            database.insert_pending_memory({
                "id": skeleton_id, "content": content, "room": room,
                "category": category, "importance": importance,
                "source_ai": source_ai, "event_date": event_date,
                "source_platform": "mcp:safe", "status": "pending",
                "client_request_id": effective_crq, "created_at": now,
                "subject_name": subject_name, "speaker_name": speaker_name,
                "request_fingerprint": req_fp,
            })
            inserted = True
            break
        except sqlite3.IntegrityError:
            if effective_crq:
                existing = database.get_memory_by_client_request_id(effective_crq)
                if existing:
                    stored_fp = existing.get("request_fingerprint") or ""
                    if stored_fp and stored_fp != req_fp:
                        return json.dumps({
                            "status": "error", "error": "crq_content_conflict",
                            "memory_id": "", "client_request_id": client_request_id,
                        }, ensure_ascii=False)
                    return _idempotent_response(existing)
            if attempt < _MAX_ID_RETRIES:
                skeleton_id = _new_skeleton_id()
                continue
            _LOG.error("skeleton_id collision after %d retries", _MAX_ID_RETRIES)
    if not inserted:
        return json.dumps({
            "status": "error", "error": "id_collision_max_retry",
            "memory_id": "", "client_request_id": client_request_id,
        }, ensure_ascii=False)

    _spawn_background_task(_finalize_pending_memory(
        skeleton_id,
        content=content, room=room, category=category, importance=importance,
        source_ai=source_ai, event_date=event_date, force_create=force_create,
        client_request_id=effective_crq,
        subject_name=subject_name, speaker_name=speaker_name,
        source_platform="mcp:safe",
    ))

    return json.dumps({
        "status": "queued", "memory_id": skeleton_id,
        "client_request_id": client_request_id,
    }, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════════════════
# 12 consolidated MCP tools + 5 redirect aliases
# ═══════════════════════════════════════════════════════════════════════

# ── 1. remember ──────────────────────────────────────────────────────

@mcp.tool()
async def remember(
    content: str = "",
    room: str = "living_room",
    category: str = "",
    importance: float = 0.5,
    source_ai: str = "claude",
    event_date: str = "",
    force_create: bool = False,
    client_request_id: str = "",
    subject_name: str = "",
    speaker_name: str = "",
    items: list[dict] | None = None,
) -> str:
    """存一条或多条记忆。单条传 content，批量传 items=[{content, room, ...}, ...]。

    异步管线：<2秒返回 queued，后台跑 embedding + 分类 + 合并。
    内置安全降敏：写入失败自动改写中性摘要重试。传 client_request_id 做幂等去重。

    Args:
        content: 记忆内容（单条模式）
        room: 房间ID
        category: 分类标签
        importance: 重要度 0-1
        source_ai: 来源AI（必传你的身份）
        event_date: 事件日期
        force_create: 跳过自动合并
        client_request_id: 幂等 key
        subject_name: 记忆主体姓名
        speaker_name: 发言者姓名
        items: 批量模式——记忆列表，每条含 content/room/importance 等字段
    """
    if items:
        _audit("tool_reached", tool="remember_batch", source_ai=source_ai, count=len(items))
        created = merged = skipped = failed = blocked = 0
        results = []
        for idx, item in enumerate(items):
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
                    subject_name=item.get("subject_name", ""),
                    speaker_name=item.get("speaker_name", ""),
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
            elif status in ("guardrail_blocked", "guardrail_unavailable") or result.get("blocked"):
                blocked += 1
            elif status == "failed":
                failed += 1
            results.append({"index": idx, **result})
        output = {
            "total": len(results), "created": created, "merged": merged,
            "skipped": skipped, "blocked": blocked, "failed": failed,
            "items": results,
            "summary": f"{len(results)}条|新{created}合{merged}跳{skipped}拦{blocked}败{failed}",
        }
        _audit("remember_batch_result", source_ai=source_ai,
               **{k: output[k] for k in ("total", "created", "merged", "skipped", "blocked", "failed")})
        return json.dumps(output, ensure_ascii=False, indent=2)

    return await _async_remember_single(
        content=content, room=room, category=category, importance=importance,
        source_ai=source_ai, event_date=event_date, force_create=force_create,
        client_request_id=client_request_id,
        subject_name=subject_name, speaker_name=speaker_name,
    )


# ── 2. recall ────────────────────────────────────────────────────────

_LIST_COMPACT_FIELDS = ("id", "content", "layer", "room", "category", "importance",
                        "status", "resolved", "anchored", "created_at", "updated_at")


@mcp.tool()
async def recall(
    query: str,
    top_k: int = 5,
    with_corridor: bool = False,
    source_ai: str = "claude",
    compact: bool = False,
    include_dreams: bool = False,
) -> str:
    """搜索记忆。用自然语言描述要找的内容。设 include_dreams=True 可同时搜梦境。

    Args:
        query: 搜索关键词或自然语言描述
        top_k: 返回数量（默认5）
        with_corridor: 同时返回走廊上下文
        source_ai: AI身份
        compact: 精简模式，只返回核心字段
        include_dreams: 是否同时搜梦境（默认不搜，梦境需要显式开启）
    """
    results = await memory_ops.recall(query=query, ai_id=source_ai, top_k=top_k)
    if include_dreams:
        dream_results = await memory_ops.dream_recall(query, ai_id=source_ai, top_k=min(top_k, 3))
        if dream_results:
            for d in dream_results:
                d["_source"] = "dream"
            results.extend(dream_results)
    if compact:
        results = [
            {k: item[k] for k in ("id", "content", "room", "confidence", "created_at") if k in item}
            for item in results
        ]
    else:
        for item in results:
            item.pop("score", None)
    output = {"results": results}
    if with_corridor:
        corridor_text = await corridor_mod.get_corridor(source_ai)
        output["corridor"] = corridor_text or ""
    return json.dumps(output, ensure_ascii=False, indent=2)


# ── 3. context ───────────────────────────────────────────────────────

@mcp.tool()
async def context(
    source_ai: str = "claude",
    message: str = "",
    mode: str = "full",
    max_chars: int = 3000,
) -> str:
    """获取记忆上下文。对话开头用 full，已有基础上下文用 incremental。

    mode:
    - full: 走廊 + 相关记忆 + 梦境 + 待办（醒来第一件事）
    - incremental: 只返回增量（最近变化 + 待办 + 相关记忆），更短
    - corridor: 只要走廊快照
    - living_room: 只要核心身份记忆

    Args:
        source_ai: AI身份（必传）
        message: 当前用户消息（用于搜索相关记忆）
        mode: full / incremental / corridor / living_room
        max_chars: 返回文本最大字符数（incremental/full 模式生效）
    """
    if mode == "corridor":
        text = await corridor_mod.get_corridor(source_ai)
        return text or "（走廊为空）"
    if mode == "living_room":
        items = await memory_ops.get_living_room()
        if not items:
            return "（客厅为空）"
        return json.dumps(items, ensure_ascii=False, indent=2)
    if mode == "incremental":
        from smart_context import get_smart_context
        result = await get_smart_context(source_ai, message, has_base_context=True, max_chars=max_chars)
        return json.dumps(result, ensure_ascii=False, indent=2)
    # full mode (default)
    ctx = await gateway_mod.build_context(
        user_message=message or "", ai_id=source_ai,
    )
    return ctx.get("inject_text", "") or "（暂无记忆上下文）"


# ── 4. capture ───────────────────────────────────────────────────────

import conversation_capture


@mcp.tool()
async def capture(
    action: str = "log",
    source_ai: str = "claude",
    user_message: str = "",
    ai_response: str = "",
    platform: str = "mcp",
    messages: list[dict] | None = None,
    chat_type: str = "private",
) -> str:
    """对话录入管线。action=log 记录一轮，flush 强制提取，extract 从消息数组提取记忆。

    Args:
        action: log / flush / extract
        source_ai: AI身份
        user_message: 用户说的话（log 模式）
        ai_response: AI 的回复（log 模式）
        platform: 平台标识（log 模式）
        messages: 对话消息数组 [{role, content}]（extract 模式）
        chat_type: private / private_group / public_group（extract 模式）
    """
    if action == "flush":
        result = await conversation_capture.force_extract(ai_id=source_ai)
        return json.dumps(result, ensure_ascii=False, indent=2)

    if action == "extract":
        from conversation_capture import extract_from_messages as _extract
        results = await _extract(messages or [], source_ai, chat_type, quick=True)
        return json.dumps(results, ensure_ascii=False, indent=2)

    # action == "log" (default)
    result = await conversation_capture.log_conversation(
        user_message=user_message, ai_response=ai_response,
        ai_id=source_ai, platform=platform,
    )
    import asyncio
    asyncio.ensure_future(gateway_mod._tag_pulse(user_message, source_ai))
    return json.dumps(result, ensure_ascii=False)


# ── 5. dream ─────────────────────────────────────────────────────────

@mcp.tool()
async def dream(content: str, source_ai: str = "claude") -> str:
    """写梦境/自省。私人空间，只有你自己能看到。适合深度对话后记录内心感受。

    Args:
        content: 梦境/自省内容
        source_ai: 来源AI
    """
    result = await _safe_remember_impl(
        content=content, layer="private", room="dreams",
        owner_ai=source_ai, importance=0.6,
        source_ai=source_ai, source_platform="mcp",
    )
    return json.dumps({"status": "dreamed", **result}, ensure_ascii=False)


# ── 6. search ────────────────────────────────────────────────────────

@mcp.tool()
async def search(
    method: str,
    query: str = "",
    source_ai: str = "",
    tags: list[str] | None = None,
    tag_mode: str = "any",
    with_person: str = "",
    days: int = 30,
    limit: int = 10,
    room: str = "",
    speaker_filter: str = "",
) -> str:
    """搜索原文和元数据。method 决定搜索方式：

    - keyword: 关键词搜原文保险箱（精确匹配，支持同义词）
    - semantic: 语义搜最近 N 天原文（模糊描述找原话）
    - tags: 按标签精确搜记忆
    - person: 按人名+时间窗查互动事件

    Args:
        method: keyword / semantic / tags / person（必填）
        query: 搜索内容（keyword/semantic 必填）
        source_ai: AI身份
        tags: 标签列表（tags 方法必填）
        tag_mode: any=匹配任一 / all=全部匹配（tags 方法）
        with_person: 人名（person 方法必填）
        days: 时间窗天数（semantic/person 方法）
        limit: 返回条数
        room: 限定房间（tags 方法）
        speaker_filter: user/ai/留空（keyword 方法）
    """
    import raw_vault

    if method == "keyword":
        hits = raw_vault.search(query, ai_id="", limit=limit, speaker_filter=speaker_filter)
        return json.dumps({"results": hits, "stats": raw_vault.stats(public_only=True)},
                          ensure_ascii=False, indent=2)

    if method == "semantic":
        from embedding import get_embedding
        if not (query or "").strip():
            return json.dumps({"error": "empty_query", "hint": "请提供搜索内容"}, ensure_ascii=False)
        days = max(1, min(days, 120))
        limit = max(1, min(limit, 30))
        query_vec = await get_embedding(query)
        if not query_vec:
            return json.dumps({"error": "embedding_failed", "hint": "无法计算查询向量，请改用 method=keyword"},
                              ensure_ascii=False)
        hits = raw_vault.semantic_search(query_vec=query_vec, ai_id="", days=days, limit=limit)
        for h in hits:
            h.pop("embedding", None)
        return json.dumps({"results": hits, "query": query, "days": days,
                           "stats": raw_vault.stats(public_only=True)}, ensure_ascii=False, indent=2)

    if method == "tags":
        results = await memory_ops.search_by_tags(
            tags=tags or [], mode=tag_mode, room=room, limit=limit, ai_id=source_ai)
        return json.dumps({"count": len(results), "results": results}, ensure_ascii=False, indent=2)

    if method == "person":
        try:
            result = await memory_ops.recent_interaction(
                with_person=with_person, ai_id=source_ai or "claude", days=days, limit=limit)
        except Exception as e:
            result = {"with_person": with_person, "resolved_to": None,
                      "error": "internal_error", "hint": f"内部错误：{type(e).__name__}",
                      "days": days, "count": 0, "items": []}
        return json.dumps(result, ensure_ascii=False, indent=2)

    return json.dumps({"error": f"unknown method '{method}'",
                       "valid": ["keyword", "semantic", "tags", "person"]}, ensure_ascii=False)


# ── 7. manage ────────────────────────────────────────────────────────

@mcp.tool()
async def manage(
    action: str,
    memory_id: str = "",
    source_ai: str = "",
    content: str = "",
    importance: float = -1,
    room: str = "",
    tags: list[str] | None = None,
    kind: str = "reflection",
    resolved: bool = True,
    old_value: str = "",
) -> str:
    """管理单条记忆。action 决定操作：

    - update: 修改内容/重要度/房间/标签
    - comment: 追加年轮评论（不改原文）
    - resolve / unresolve: 标记待办状态
    - archive / unarchive: 归档/恢复
    - delete: 永久删除
    - anchor / release_anchor: 锚定/解除（永不衰减，最多20条）
    - correct: 用户纠错（一步完成纠正+标记旧记忆）

    Args:
        action: 操作类型（必填）
        memory_id: 记忆ID（correct 以外都必填）
        source_ai: AI身份
        content: 新内容（update/comment/correct 用）
        importance: 新重要度（update 用，-1=不改）
        room: 新房间（update/correct 用）
        tags: 新标签（update 用）
        kind: 评论类型 reflection/update_note/feel/comment（comment 用）
        resolved: True=已解决 False=未解决（resolve/unresolve 用）
        old_value: 被纠正的错误说法（correct 用）
    """
    if action == "update":
        result = await memory_ops.update_memory(
            memory_id=memory_id,
            content=content or None,
            importance=importance if importance >= 0 else None,
            room=room or None, tags=tags or None,
            changed_by=source_ai or "claude", update_provenance="ai_summary",
        )
    elif action == "comment":
        result = await memory_ops.add_comment(
            memory_id=memory_id, content=content,
            author=source_ai or "claude", kind=kind,
        )
    elif action == "resolve":
        result = await memory_ops.resolve_memory(memory_id, resolved=True)
    elif action == "unresolve":
        result = await memory_ops.resolve_memory(memory_id, resolved=False)
    elif action == "archive":
        result = await memory_ops.archive_memory(memory_id)
    elif action == "unarchive":
        result = await memory_ops.unarchive_memory(memory_id, changed_by=source_ai or "claude")
    elif action == "delete":
        result = await memory_ops.delete_memory(memory_id)
    elif action == "anchor":
        result = await memory_ops.anchor_memory(memory_id)
    elif action == "release_anchor":
        result = await memory_ops.release_anchor(memory_id)
    elif action == "correct":
        result = await memory_ops.apply_user_correction(
            corrected_value=content, old_value=old_value,
            source_ai=source_ai, room=room or "living_room",
        )
        return json.dumps(result, ensure_ascii=False, indent=2)
    else:
        return json.dumps({"error": f"unknown action '{action}'",
                           "valid": ["update", "comment", "resolve", "unresolve",
                                     "archive", "unarchive", "delete",
                                     "anchor", "release_anchor", "correct"]}, ensure_ascii=False)
    return json.dumps(result, ensure_ascii=False)


# ── 8. detail ────────────────────────────────────────────────────────

@mcp.tool()
async def detail(
    memory_id: str = "",
    room: str = "",
    status: str = "active",
    page: int = 1,
    per_page: int = 20,
    compact: bool = True,
    source_ai: str = "",
) -> str:
    """查看记忆。传 memory_id 看一条的完整详情；不传则按条件列出。

    Args:
        memory_id: 记忆ID（传了则返回完整详情含原始对话、年轮评论）
        room: 房间筛选（列表模式）
        status: active/archived/decayed（列表模式）
        page: 页码
        per_page: 每页数量
        compact: 列表精简模式（默认开）
        source_ai: AI身份（影响私有记忆可见性）
    """
    if memory_id:
        from visibility import can_view
        mem = store.get_memory(memory_id)
        if not mem:
            return json.dumps({"error": f"Memory {memory_id} not found"}, ensure_ascii=False)
        if not can_view(mem, source_ai):
            return json.dumps({"error": "该记忆是其他 AI 的私有记忆，无权查看"}, ensure_ascii=False)
        safe = {k: v for k, v in mem.items() if k != "embedding"}
        return json.dumps(safe, ensure_ascii=False, indent=2)

    result = await memory_ops.list_memories(
        room=room or None, status=status, page=page, per_page=per_page,
        viewer_ai=source_ai or "",
    )
    if compact and isinstance(result, dict) and isinstance(result.get("items"), list):
        result["items"] = [
            {k: m.get(k) for k in _LIST_COMPACT_FIELDS if k in m}
            for m in result["items"]
        ]
    return json.dumps(result, ensure_ascii=False, indent=2)


# ── 9. review ────────────────────────────────────────────────────────

@mcp.tool()
async def review(
    action: str,
    source_ai: str = "",
    proposal_id: str = "",
    decision: str = "",
    reject_reason: str = "",
    profile_id: str = "",
    profile_type: str = "",
    status: str = "pending",
    page: int = 1,
    per_page: int = 20,
) -> str:
    """审核提案和画像。action 决定操作：

    - list_proposals: 列出待审记忆提案
    - review_proposal: 审核提案（decision=approve/reject）
    - get_profile: 查看画像（用户/AI/关系）
    - approve_profile: 审批画像

    Args:
        action: 操作类型（必填）
        source_ai: AI身份
        proposal_id: 提案ID（review_proposal 用）
        decision: approve/reject（review_proposal 用）
        reject_reason: 拒绝原因
        profile_id: 画像ID（get_profile/approve_profile 用）
        profile_type: user/agent/relationship（get_profile 筛选用）
        status: 提案状态筛选（list_proposals 用）
        page: 页码
        per_page: 每页数量
    """
    if action == "list_proposals":
        result = await memory_ops.list_proposals(status=status, limit=per_page, page=page)
        return json.dumps(result, ensure_ascii=False, indent=2)

    if action == "review_proposal":
        result = await memory_ops.review_proposal(
            proposal_id=proposal_id, action=decision,
            reviewed_by=source_ai or "claude", reject_reason=reject_reason,
        )
        return json.dumps(result, ensure_ascii=False, indent=2)

    if action == "get_profile":
        if profile_id:
            p = database.get_profile(profile_id)
            if not p:
                return json.dumps({"error": f"Profile '{profile_id}' not found"})
            return json.dumps(p, ensure_ascii=False, indent=2)
        profiles = database.list_profiles(profile_type=profile_type or None)
        return json.dumps(profiles, ensure_ascii=False, indent=2)

    if action == "approve_profile":
        ok = database.approve_profile(profile_id)
        if ok:
            return json.dumps({"status": "approved", "profile_id": profile_id}, ensure_ascii=False)
        p = database.get_profile(profile_id)
        if not p:
            return json.dumps({"error": f"Profile '{profile_id}' not found"})
        return json.dumps({"error": f"Profile '{profile_id}' is '{p.get('status')}', not pending_review"})

    return json.dumps({"error": f"unknown action '{action}'",
                       "valid": ["list_proposals", "review_proposal", "get_profile", "approve_profile"]},
                      ensure_ascii=False)


# ── 10. grow ─────────────────────────────────────────────────────────

@mcp.tool()
async def grow(
    content: str,
    source_ai: str = "claude",
    subject_name: str = "",
    speaker_name: str = "",
) -> str:
    """把一大段混合内容（日记、对话总结等）拆分成多条独立记忆。

    Args:
        content: 要整理的长文本
        source_ai: 来源AI
        subject_name: 记忆主体姓名
        speaker_name: 发言者姓名
    """
    result = await memory_ops.grow(
        content=content, source_ai=source_ai,
        subject_name=subject_name, speaker_name=speaker_name,
    )
    summary = f"{result['total']}条|新{result['created']}合{result['merged']}"
    result["summary"] = summary
    return json.dumps(result, ensure_ascii=False)


# ── 11. system ───────────────────────────────────────────────────────

@mcp.tool()
async def system(
    action: str,
    limit: int = 20,
    include_audit: bool = False,
    batch_action: str = "",
    filter_rules: dict | None = None,
    value: str = "",
) -> str:
    """系统管理和诊断。action 决定操作：

    - info: 角色+房间配置
    - health: MCP 身份+schema hash（排查连接问题）
    - debug_log: MCP 审计日志
    - doctor: 记忆体检报告
    - maintain: 执行记忆整理（合并/衰减/重建走廊）
    - batch_ops: 批量操作（reset_activation/reclassify/bulk_resolve/bulk_archive）

    Args:
        action: 操作类型（必填）
        limit: 日志条数（debug_log 用）
        include_audit: 是否含审计日志（health 用）
        batch_action: 批量操作类型（batch_ops 用）
        filter_rules: 过滤条件（batch_ops 用）
        value: 操作值（batch_ops 用）
    """
    if action == "info":
        rooms = list_rooms()
        data = {
            "roles": AI_ROLES,
            "rooms": {k: {"name": v["name"], "icon": v.get("icon", ""), "type": v.get("type", "")}
                      for k, v in rooms.items()},
            "mcp_identity": await get_mcp_identity_async(),
        }
        return json.dumps(data, ensure_ascii=False, indent=2)

    if action == "health":
        data = {"ok": True, "identity": await get_mcp_identity_async(),
                "audit_path": str(MCP_AUDIT_PATH)}
        if include_audit:
            data["recent_audit"] = _read_recent_audit(20)
        return json.dumps(data, ensure_ascii=False, indent=2)

    if action == "debug_log":
        return json.dumps({"items": _read_recent_audit(max(1, min(limit, 100)))},
                          ensure_ascii=False, indent=2)

    if action == "doctor":
        import memory_doctor
        report = memory_doctor.read_report()
        return json.dumps({
            "text": memory_doctor.report_text(),
            "auto_fixed": report.get("auto_fixed", []),
            "issues": report.get("issues", []),
            "stats": report.get("stats", {}),
            "generated_at": report.get("generated_at", ""),
        }, ensure_ascii=False, indent=2)

    if action == "maintain":
        result = await daemon.run_full_maintenance()
        return json.dumps(result, ensure_ascii=False, indent=2)

    if action == "batch_ops":
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
        result = await batch_operation(batch_action, filter_rules or {}, parsed_value)
        return json.dumps(result, ensure_ascii=False, indent=2)

    return json.dumps({"error": f"unknown action '{action}'",
                       "valid": ["info", "health", "debug_log", "doctor", "maintain", "batch_ops"]},
                      ensure_ascii=False)


# ── 12. window_context ───────────────────────────────────────────────

@mcp.tool()
async def window_context(
    ai_id: str,
    chat_id: str,
    thread_id: str = "",
    max_turns: int = 10,
    max_chars: int = 6000,
) -> str:
    """恢复当前聊天窗口的最近对话——bot 重启后续聊用。

    Args:
        ai_id: bot 身份（cloudy/lucien/jasper）
        chat_id: Telegram chat_id
        thread_id: 话题ID（有话题群时传）
        max_turns: 最多返回轮数（默认10）
        max_chars: 字符预算（默认6000）
    """
    import raw_vault
    if not chat_id:
        return json.dumps({"error": "chat_id_required"}, ensure_ascii=False)
    result = raw_vault.get_window_context(
        ai_id=ai_id, chat_id=str(chat_id),
        thread_id=str(thread_id or ""),
        max_turns=max_turns, max_chars=max_chars,
    )
    return json.dumps(result, ensure_ascii=False, indent=2)


# ═══════════════════════════════════════════════════════════════════════
# Redirect aliases (Phase 1 backward compatibility)
# ═══════════════════════════════════════════════════════════════════════

@mcp.tool()
async def pulse(message: str = "", source_ai: str = "claude", force_corridor: bool = False) -> str:
    """[已合并到 context] 获取完整记忆上下文。请改用 context(mode='full')。"""
    return await context(source_ai=source_ai, message=message, mode="full")


@mcp.tool()
async def smart_context(
    ai_id: str = "claude",
    user_message: str = "",
    has_base_context: bool = False,
    max_chars: int = 3000,
) -> str:
    """[已合并到 context] 智能上下文。请改用 context(mode='incremental'或'full')。"""
    mode = "incremental" if has_base_context else "full"
    return await context(source_ai=ai_id, message=user_message, mode=mode, max_chars=max_chars)


@mcp.tool()
async def capture_conversation(
    user_message: str,
    ai_response: str,
    source_ai: str = "claude",
    platform: str = "mcp",
) -> str:
    """[已合并到 capture] 记录一轮对话。请改用 capture(action='log')。"""
    return await capture(
        action="log", source_ai=source_ai,
        user_message=user_message, ai_response=ai_response, platform=platform,
    )


@mcp.tool()
async def safe_remember(
    content: str = "",
    room: str = "living_room",
    category: str = "",
    importance: float = 0.5,
    source_ai: str = "claude",
    event_date: str = "",
    subject_name: str = "",
    speaker_name: str = "",
    client_request_id: str = "",
) -> str:
    """[已合并到 remember] 安全写入。请直接用 remember（已内置安全降敏）。"""
    return await remember(
        content=content, room=room, category=category, importance=importance,
        source_ai=source_ai, event_date=event_date,
        client_request_id=client_request_id,
        subject_name=subject_name, speaker_name=speaker_name,
    )


@mcp.tool()
async def batch_remember(
    memories: list[dict],
    source_ai: str = "claude",
) -> str:
    """[已合并到 remember] 批量存储。请改用 remember(items=[...])。"""
    return await remember(items=memories, source_ai=source_ai)
