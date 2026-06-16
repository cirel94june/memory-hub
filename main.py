"""
Memory Hub - 主服务入口
多 AI 角色共享记忆系统
"""
import json
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Header, Query, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from typing import Optional

from config import HUB_SECRET, AI_ROLES, ROOMS, register_room, list_rooms
import github_store
import memory_ops
import gateway as gateway_mod
import daemon
import corridor
import ai_profiles
from mcp_server import mcp as mcp_server


# ── 鉴权 ──

def verify_secret(authorization: str = Header(default="")):
    token = authorization.replace("Bearer ", "").strip()
    if token != HUB_SECRET:
        raise HTTPException(status_code=401, detail="Invalid token")


# ── App ──

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _mcp_session_manager
    await github_store.load_all()
    await ai_profiles.load_profiles()
    mems = github_store.get_all_memories()
    print(f"[Memory Hub] Loaded {len(mems)} memories from GitHub")
    print(f"[Memory Hub] Roles: {list(AI_ROLES.keys())}")
    print(f"[Memory Hub] Rooms: {list(ROOMS.keys())}")

    # 初始化 MCP session manager（通过 streamable_http_app 触发懒加载）
    _mcp_inst.streamable_http_app()  # 确保 _session_manager 被创建
    _mcp_session_manager = _mcp_inst._session_manager
    async with _mcp_session_manager.run():
        print("[Memory Hub] MCP server ready at /mcp")

        # 启动后台 daemon 定时任务
        daemon_task = asyncio.create_task(_daemon_loop())
        print("[Memory Hub] Daemon scheduler started (every 12h)")
        try:
            yield
        finally:
            daemon_task.cancel()


async def _daemon_loop():
    """每12小时自动跑一次记忆整理"""
    log = logging.getLogger("daemon_scheduler")
    # 启动后等5分钟再跑第一次（让服务先稳定）
    await asyncio.sleep(300)
    while True:
        try:
            log.info("Starting scheduled maintenance...")
            result = await daemon.run_full_maintenance()
            log.info(f"Scheduled maintenance done: {json.dumps(result)}")
            print(f"[Daemon] Maintenance complete: merge={result.get('merge',{}).get('merged',0)}, "
                  f"decay={result.get('decay',{}).get('archived',0)}")
        except Exception as e:
            log.error(f"Scheduled maintenance failed: {e}")
            print(f"[Daemon] Maintenance failed: {e}")
        # 等12小时
        await asyncio.sleep(12 * 3600)

app = FastAPI(title="Memory Hub", lifespan=lifespan)

# ── MCP Server 端点 ──
# 在 FastAPI 的 lifespan 里初始化 MCP session manager
from mcp_server import mcp as _mcp_inst

_mcp_session_manager = None

app.mount("/static", StaticFiles(directory="static"), name="static")


# ── 首页 ──

@app.get("/")
async def index():
    return FileResponse("static/index.html")


# ── 元信息 ──

@app.get("/api/info")
async def info():
    rooms = list_rooms()
    return {"roles": AI_ROLES, "rooms": {k: {"name": v["name"], "icon": v.get("icon", ""), "type": v.get("type", ""), "scope": v.get("scope", "")} for k, v in rooms.items()}}


# ── 房间管理（动态新增） ──

class RoomCreate(BaseModel):
    id: str
    name: str
    description: str = ""
    type: str = "on_demand"  # always / on_demand / isolated
    scope: str = "shared"    # shared / per_ai / public
    icon: str = "📁"
    fast_decay: bool = False

@app.post("/api/rooms")
async def create_room(body: RoomCreate, authorization: str = Header(default="")):
    verify_secret(authorization)
    room_cfg = body.model_dump()
    room_id = room_cfg.pop("id")
    register_room(room_id, room_cfg)
    # 保存到 GitHub 的自定义房间配置
    rooms = list_rooms()
    custom = [{"id": k, **v} for k, v in rooms.items() if k not in __import__("config").DEFAULT_ROOMS]
    await github_store._write_github_file("_config/custom_rooms.json", custom, f"Add room: {body.name}")
    return {"id": room_id, "status": "created"}


@app.get("/api/rooms")
async def get_rooms():
    rooms = list_rooms()
    return [{"id": k, **{kk: vv for kk, vv in v.items()}} for k, v in rooms.items()]


# ── 记忆 CRUD ──

class RememberRequest(BaseModel):
    content: str
    layer: str = "shared"
    room: str = "living_room"
    category: str = ""
    owner_ai: str = ""
    importance: float = 0.5
    emotion_arousal: float = 0.3
    source_ai: str = ""
    source_platform: str = ""
    tags: list[str] = []
    event_date: str = ""

@app.post("/api/memory/remember")
async def api_remember(body: RememberRequest, authorization: str = Header(default="")):
    verify_secret(authorization)
    result = await memory_ops.remember(
        content=body.content, layer=body.layer, room=body.room,
        category=body.category, owner_ai=body.owner_ai,
        importance=body.importance, emotion_arousal=body.emotion_arousal,
        source_ai=body.source_ai, source_platform=body.source_platform,
        tags=body.tags, event_date=body.event_date,
    )
    return result


class CommentRequest(BaseModel):
    content: str
    author: str = "claude"
    kind: str = "comment"
    valence: Optional[float] = None
    arousal: Optional[float] = None

@app.post("/api/memory/{memory_id}/comment")
async def api_add_comment(memory_id: str, body: CommentRequest, authorization: str = Header(default="")):
    verify_secret(authorization)
    result = await memory_ops.add_comment(
        memory_id=memory_id,
        content=body.content,
        author=body.author,
        kind=body.kind,
        valence=body.valence,
        arousal=body.arousal,
    )
    return result


class UpdateRequest(BaseModel):
    content: Optional[str] = None
    importance: Optional[float] = None
    room: Optional[str] = None
    category: Optional[str] = None
    tags: Optional[list[str]] = None
    owner_ai: Optional[str] = None
    layer: Optional[str] = None
    changed_by: str = ""

@app.put("/api/memory/{memory_id}")
async def api_update(memory_id: str, body: UpdateRequest, authorization: str = Header(default="")):
    verify_secret(authorization)
    result = await memory_ops.update_memory(
        memory_id=memory_id, content=body.content, importance=body.importance,
        room=body.room, category=body.category, tags=body.tags,
        owner_ai=body.owner_ai, layer=body.layer, changed_by=body.changed_by,
    )
    return result


class GrowRequest(BaseModel):
    content: str
    source_ai: str = ""

@app.post("/api/memory/grow")
async def api_grow(body: GrowRequest, authorization: str = Header(default="")):
    verify_secret(authorization)
    result = await memory_ops.grow(content=body.content, source_ai=body.source_ai)
    return result


@app.get("/api/memory/recall")
async def api_recall(
    q: str, ai_id: str = "", top_k: int = 8,
    authorization: str = Header(default=""),
):
    verify_secret(authorization)
    results = await memory_ops.recall(query=q, ai_id=ai_id, top_k=top_k)
    return {"results": results}


@app.get("/api/memory/list")
async def api_list(
    layer: str = None, room: str = None, owner_ai: str = None,
    status: str = "active", page: int = 1, per_page: int = 20,
    source_ai: str = None,
    authorization: str = Header(default=""),
):
    verify_secret(authorization)
    return await memory_ops.list_memories(
        layer=layer, room=room, owner_ai=owner_ai,
        status=status, page=page, per_page=per_page,
    )


@app.get("/api/memory/living-room")
async def api_living_room(authorization: str = Header(default="")):
    verify_secret(authorization)
    items = await memory_ops.get_living_room()
    return {"items": items}


@app.get("/api/memory/{memory_id}")
async def api_get_memory(memory_id: str, authorization: str = Header(default="")):
    verify_secret(authorization)
    mem = github_store.get_memory(memory_id)
    if not mem:
        raise HTTPException(status_code=404, detail=f"Memory {memory_id} not found")
    safe = {k: v for k, v in mem.items() if k != "embedding"}
    return safe


@app.post("/api/memory/{memory_id}/archive")
async def api_archive(memory_id: str, authorization: str = Header(default="")):
    verify_secret(authorization)
    return await memory_ops.archive_memory(memory_id)


@app.delete("/api/memory/{memory_id}")
async def api_delete(memory_id: str, authorization: str = Header(default="")):
    verify_secret(authorization)
    return await memory_ops.delete_memory(memory_id)


# ── Gateway（核心：自动记忆注入 + 提取） ──

class ContextRequest(BaseModel):
    user_message: str
    ai_id: str
    recent_messages: list[dict] = []

@app.post("/api/gateway/context")
async def api_build_context(body: ContextRequest, authorization: str = Header(default="")):
    verify_secret(authorization)
    return await gateway_mod.build_context(
        user_message=body.user_message,
        ai_id=body.ai_id,
        recent_messages=body.recent_messages,
    )


class PostProcessRequest(BaseModel):
    user_message: str
    ai_response: str
    ai_id: str
    platform: str = ""

@app.post("/api/gateway/post-process")
async def api_post_process(body: PostProcessRequest, authorization: str = Header(default="")):
    verify_secret(authorization)
    return await gateway_mod.post_process(
        user_message=body.user_message,
        ai_response=body.ai_response,
        ai_id=body.ai_id,
        platform=body.platform,
    )


# ── 对话自动捕获 ──

import conversation_capture

class ConversationLogRequest(BaseModel):
    user_message: str
    ai_response: str = ""
    ai_id: str = "claude"
    platform: str = ""
    chat_id: str = ""
    chat_type: str = "private"

@app.post("/api/capture/log")
async def api_log_conversation(body: ConversationLogRequest, authorization: str = Header(default="")):
    """记录一轮对话，缓冲区满时自动提取记忆"""
    verify_secret(authorization)
    return await conversation_capture.log_conversation(
        user_message=body.user_message,
        ai_response=body.ai_response,
        ai_id=body.ai_id,
        platform=body.platform,
        chat_id=body.chat_id,
        chat_type=body.chat_type,
    )

@app.post("/api/capture/extract")
async def api_force_extract(authorization: str = Header(default="")):
    """手动触发对话总结（不等缓冲区满）"""
    verify_secret(authorization)
    return await conversation_capture.force_extract()

@app.get("/api/capture/status")
async def api_capture_status(authorization: str = Header(default="")):
    """查看对话缓冲区状态"""
    verify_secret(authorization)
    return await conversation_capture.get_buffer_status()


# ── 对话历史摘要（供 TG bot 滚动压缩） ──

class SummarizeRequest(BaseModel):
    messages: list[dict]
    ai_id: str = "claude"
    existing_summary: str = ""

@app.post("/api/utils/summarize-history")
async def api_summarize_history(body: SummarizeRequest, authorization: str = Header(default="")):
    """把一段对话历史压缩成摘要，用于 bot 端的滚动历史压缩"""
    verify_secret(authorization)
    lines = []
    for m in body.messages:
        role = "用户" if m.get("role") == "user" else "AI"
        ts = m.get("timestamp", "")
        content = str(m.get("content", ""))[:300]
        lines.append(f"[{ts}] {role}: {content}" if ts else f"{role}: {content}")
    conversation_text = "\n".join(lines)

    prev = ""
    if body.existing_summary:
        prev = f"\n已有的之前的对话摘要：\n{body.existing_summary}\n"

    prompt = f"""把以下对话历史压缩成一段简洁的摘要（中文，400字以内）。
保留：关键话题、重要事实、有趣的梗/笑话的具体内容（保留原话和细节）、情绪转折、未完成的讨论。
丢弃：日常寒暄、重复内容、无信息量的闲聊。
{prev}
对话记录：
{conversation_text[:4000]}

直接输出摘要文本，不要加标题或前缀。"""

    from gateway import _call_llm
    summary = await _call_llm(prompt)
    if not summary or len(summary.strip()) < 10:
        return {"summary": "", "ok": False}
    return {"summary": summary.strip(), "ok": True}


# ── Daemon 操作 ──

@app.post("/api/daemon/decay")
async def api_decay(authorization: str = Header(default="")):
    verify_secret(authorization)
    return await memory_ops.run_decay()


@app.post("/api/daemon/maintain")
async def api_maintain(authorization: str = Header(default="")):
    """一键执行完整记忆整理（合并+压缩+归档+衰减+走廊重建）"""
    verify_secret(authorization)
    return await daemon.run_full_maintenance()


@app.get("/api/daemon/test-llm")
async def api_test_llm(authorization: str = Header(default="")):
    """测试 daemon 小模型是否连通"""
    verify_secret(authorization)
    result = await daemon._call_llm("请回复两个字：正常")
    from config import LLM_API_KEY, LLM_MODEL, LLM_BASE_URL, EMBEDDING_MODEL
    from embedding import get_embedding
    emb = await get_embedding("测试向量化")
    return {
        "llm_result": result,
        "llm_model": LLM_MODEL,
        "llm_base_url": LLM_BASE_URL,
        "llm_key_set": bool(LLM_API_KEY),
        "embedding_model": EMBEDDING_MODEL,
        "embedding_ok": emb is not None,
        "embedding_dim": len(emb) if emb else 0,
    }


# ── 走廊 ──

@app.get("/api/corridor/{ai_id}")
async def api_corridor(ai_id: str, authorization: str = Header(default="")):
    """获取 AI 的走廊文档（醒来时读的第一份记忆）"""
    verify_secret(authorization)
    text = await corridor.get_corridor(ai_id)
    return {"ai_id": ai_id, "corridor": text}


@app.post("/api/corridor/{ai_id}/rebuild")
async def api_rebuild_corridor(ai_id: str, authorization: str = Header(default="")):
    """手动重建某个 AI 的走廊"""
    verify_secret(authorization)
    text = await corridor.build_corridor(ai_id)
    return {"ai_id": ai_id, "corridor": text, "status": "rebuilt"}


# ── 对话导入（从 JSON/TXT 自动提取记忆 + 用户画像） ──

import conversation_import

class ImportRequest(BaseModel):
    content: str  # 对话文本或 JSON 字符串
    format: str = "auto"  # auto / json / txt
    ai_id: str = "claude"
    platform: str = "import"

@app.post("/api/import/conversation")
async def api_import_conversation(body: ImportRequest, authorization: str = Header(default="")):
    """导入对话记录，自动提取记忆和用户画像"""
    verify_secret(authorization)
    return await conversation_import.import_conversation(
        content=body.content,
        format=body.format,
        ai_id=body.ai_id,
        platform=body.platform,
    )


# ── 过时记忆检测（手动触发） ──

@app.post("/api/daemon/stale-check")
async def api_stale_check(authorization: str = Header(default="")):
    """手动触发过时记忆检测"""
    verify_secret(authorization)
    from daemon import detect_stale_memories
    return await detect_stale_memories()


# ── OpenAI 兼容代理（自动记忆注入 + 提取） ──

import proxy as proxy_mod

@app.post("/v1/chat/completions")
async def proxy_chat_completions(request: Request):
    """OpenAI 兼容代理端点

    使用方法（两种模式）：

    【简单模式】适合 RikkaHub 等只能设 URL + Key 的客户端：
      API Base URL: http://172.245.180.158:8888/v1
      API Key: {HUB_SECRET}:{AI身份}   例如 xiaoke588887:rikkahub
      服务端自动用 .env 里的 LLM_BASE_URL 和 LLM_API_KEY 转发

    【完整模式】通过自定义请求头控制转发目标：
      X-Hub-Secret: Hub密码
      X-Hub-Target-URL: 真正的 AI API 地址
      X-Hub-Target-Key: 真正的 AI API key
      X-Hub-AI-ID: AI 身份
    """
    # 验证 Hub Secret
    hub_secret = request.headers.get("x-hub-secret", "")
    auth_raw = request.headers.get("authorization", "").replace("Bearer ", "").strip()

    # 简单模式：API Key = "secret:ai_id"
    is_simple = False
    if ":" in auth_raw:
        secret_part = auth_raw.split(":", 1)[0]
        if secret_part == HUB_SECRET:
            is_simple = True

    if not is_simple:
        # 完整模式鉴权
        if hub_secret != HUB_SECRET:
            if auth_raw != HUB_SECRET and not request.headers.get("x-hub-target-key"):
                raise HTTPException(status_code=401, detail="Invalid Hub Secret. Use 'secret:ai_id' as API key, or set X-Hub-Secret header.")

    body = await request.json()
    return await proxy_mod.handle_chat_completions(request, body)


@app.get("/v1/models")
async def proxy_models(request: Request):
    """OpenAI 兼容 models 端点"""
    return await proxy_mod.handle_models(request)


# ── AI Profile 管理 ──

class ProfileUpdate(BaseModel):
    name: Optional[str] = None
    emoji: Optional[str] = None
    color: Optional[str] = None
    platform: Optional[str] = None
    greeting: Optional[str] = None
    persona: Optional[str] = None
    model_url: Optional[str] = None
    model_key: Optional[str] = None
    model_name: Optional[str] = None


@app.get("/api/ai-profiles")
async def api_list_profiles(authorization: str = Header(default="")):
    verify_secret(authorization)
    return ai_profiles.get_all_profiles()


@app.get("/api/ai-profiles/{ai_id}")
async def api_get_profile(ai_id: str, authorization: str = Header(default="")):
    verify_secret(authorization)
    profile = ai_profiles.get_profile(ai_id)
    if not profile:
        raise HTTPException(status_code=404, detail=f"AI '{ai_id}' not found")
    return profile


@app.post("/api/ai-profiles/{ai_id}")
async def api_create_profile(ai_id: str, body: ProfileUpdate, authorization: str = Header(default="")):
    verify_secret(authorization)
    data = {k: v for k, v in body.model_dump().items() if v is not None}
    try:
        return await ai_profiles.create_profile(ai_id, data)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.put("/api/ai-profiles/{ai_id}")
async def api_update_profile(ai_id: str, body: ProfileUpdate, authorization: str = Header(default="")):
    verify_secret(authorization)
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    return await ai_profiles.update_profile(ai_id, updates)


@app.delete("/api/ai-profiles/{ai_id}")
async def api_delete_profile(ai_id: str, authorization: str = Header(default="")):
    verify_secret(authorization)
    ok = await ai_profiles.delete_profile(ai_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"AI '{ai_id}' not found")
    return {"status": "deleted", "ai_id": ai_id}


# ── 导出 ──

@app.get("/api/export")
async def api_export(authorization: str = Header(default="")):
    verify_secret(authorization)
    data = await memory_ops.export_all()
    return JSONResponse(content=data)


# ── 启动 ──

class MCPGateway:
    """顶层 ASGI 应用：/mcp 走 MCP session manager，其他走 FastAPI"""
    def __init__(self, fastapi_app):
        self.fastapi_app = fastapi_app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].rstrip("/") == "/mcp":
            if _mcp_session_manager is not None:
                try:
                    await _mcp_session_manager.handle_request(scope, receive, send)
                except Exception as e:
                    import traceback
                    traceback.print_exc()
                    await send({"type": "http.response.start", "status": 500,
                               "headers": [(b"content-type", b"text/plain")]})
                    await send({"type": "http.response.body",
                               "body": f"MCP Error: {e}".encode()})
            else:
                await send({"type": "http.response.start", "status": 503,
                           "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"MCP not initialized"})
        else:
            await self.fastapi_app(scope, receive, send)

asgi_app = MCPGateway(app)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(asgi_app, host="0.0.0.0", port=8888, lifespan="on")
