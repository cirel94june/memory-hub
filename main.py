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
import activity_log
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
    import database
    activity_log.init_activity_table()
    mem_count = database.count_memories()
    print(f"[Memory Hub] SQLite ready: {mem_count} active memories")
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
        from ai_profiles import load_profiles
        await load_profiles()
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

# ── React SPA 前端 (/app/) ──
import os
_SPA_DIR = os.path.join(os.path.dirname(__file__), "static-app")
if os.path.isdir(os.path.join(_SPA_DIR, "assets")):
    app.mount("/app/assets", StaticFiles(directory=os.path.join(_SPA_DIR, "assets")), name="spa-assets")


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
    ai_response: str
    ai_id: str = "claude"
    platform: str = ""

@app.post("/api/capture/log")
async def api_log_conversation(body: ConversationLogRequest, authorization: str = Header(default="")):
    """记录一轮对话，缓冲区满时自动提取记忆"""
    verify_secret(authorization)
    return await conversation_capture.log_conversation(
        user_message=body.user_message,
        ai_response=body.ai_response,
        ai_id=body.ai_id,
        platform=body.platform,
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


# ── 模型设置 API ──

import analyzer

class LLMConfigUpdate(BaseModel):
    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key: str = ""

@app.get("/api/settings/llm")
async def api_get_llm_settings(authorization: str = Header(default="")):
    """获取当前 LLM 配置（不返回完整 key）"""
    verify_secret(authorization)
    cfg = analyzer.get_llm_config()
    import config as _cfg
    return {
        "current": {
            "llm_base_url": cfg["llm_base_url"],
            "llm_model": cfg["llm_model"],
            "llm_api_key_set": bool(cfg["llm_api_key"]),
            "llm_api_key_preview": cfg["llm_api_key"][:8] + "..." if len(cfg["llm_api_key"]) > 8 else "***",
        },
        "defaults": {
            "llm_base_url": _cfg.LLM_BASE_URL,
            "llm_model": _cfg.LLM_MODEL,
        },
        "is_overridden": bool(analyzer._runtime_config["llm_base_url"] or
                              analyzer._runtime_config["llm_model"] or
                              analyzer._runtime_config["llm_api_key"]),
    }

@app.put("/api/settings/llm")
async def api_update_llm_settings(body: LLMConfigUpdate, authorization: str = Header(default="")):
    """动态更新 LLM 配置（不重启服务）"""
    verify_secret(authorization)
    analyzer.set_llm_config(
        base_url=body.llm_base_url,
        model=body.llm_model,
        api_key=body.llm_api_key,
    )
    return {"status": "ok", "config": analyzer.get_llm_config()}

@app.post("/api/settings/llm/reset")
async def api_reset_llm_settings(authorization: str = Header(default="")):
    """重置 LLM 配置为 .env 默认值"""
    verify_secret(authorization)
    analyzer.reset_llm_config()
    return {"status": "ok", "message": "已重置为默认配置"}

@app.post("/api/settings/llm/test")
async def api_test_llm_settings(authorization: str = Header(default="")):
    """测试当前 LLM 配置是否能正常调用"""
    verify_secret(authorization)
    import time
    cfg = analyzer.get_llm_config()
    t0 = time.time()
    try:
        result = await analyzer._call_llm("请回复两个字：正常", "测试", temperature=0.0)
        duration = int((time.time() - t0) * 1000)
        activity_log.log_activity(
            "config", f"LLM 连通测试成功: {result[:50]}",
            model=cfg["llm_model"], duration_ms=duration,
        )
        return {
            "status": "ok",
            "response": result[:100],
            "model": cfg["llm_model"],
            "base_url": cfg["llm_base_url"],
            "duration_ms": duration,
        }
    except Exception as e:
        duration = int((time.time() - t0) * 1000)
        return {
            "status": "error",
            "error": str(e)[:300],
            "model": cfg["llm_model"],
            "base_url": cfg["llm_base_url"],
            "duration_ms": duration,
        }


# ── 活动日志 API ──

@app.get("/api/activity/log")
async def api_activity_log(
    limit: int = Query(default=50, le=200),
    action: str = Query(default=""),
    since: float = Query(default=0),
    authorization: str = Header(default=""),
):
    """获取最近的活动日志"""
    verify_secret(authorization)
    logs = activity_log.get_recent(limit=limit, action_filter=action, since_epoch=since)
    return {"logs": logs, "total": len(logs)}

@app.get("/api/activity/stats")
async def api_activity_stats(authorization: str = Header(default="")):
    """获取活动统计"""
    verify_secret(authorization)
    return activity_log.get_stats()




# ── AI Profile API + get_llm_config_for_ai ──

@app.get("/api/ai-profiles")
async def api_list_profiles(authorization: str = Header(default="")):
    """获取所有 AI 档案"""
    verify_secret(authorization)
    from ai_profiles import get_all_profiles
    from config import AI_ROLES, LLM_BASE_URL, LLM_MODEL, LLM_API_KEY
    all_p = get_all_profiles()
    profiles = []
    for ai_id, role in AI_ROLES.items():
        p = all_p.get(ai_id, {})
        profiles.append({
            "ai_id": ai_id,
            "name": p.get("name") or role.get("name", ai_id),
            "emoji": p.get("emoji") or role.get("emoji", ""),
            "color": p.get("color") or role.get("color", "#888"),
            "platform": p.get("platform", ""),
            "greeting": p.get("greeting", ""),
            "persona": p.get("persona", ""),
            "llm_base_url": p.get("model_url", ""),
            "llm_model": p.get("model_name", ""),
            "llm_api_key_set": bool(p.get("model_key")),
        })
    return {"profiles": profiles}


@app.get("/api/ai-profiles/{ai_id}/memories")
async def api_ai_memories(ai_id: str, limit: int = Query(default=30, le=100), authorization: str = Header(default="")):
    """获取某 AI 的相关记忆"""
    verify_secret(authorization)
    import database as db
    owned = db.query_memories(owner_ai=ai_id, status="active", order_by="updated_at DESC", limit=limit)
    sourced = db.query_memories(source_ai=ai_id, status="active", order_by="updated_at DESC", limit=limit)
    seen = set()
    combined = []
    for m in owned + sourced:
        if m["id"] not in seen:
            seen.add(m["id"])
            combined.append({
                "id": m["id"],
                "content": m["content"][:120],
                "room": m.get("room", ""),
                "importance": float(m.get("importance") or 0.5),
                "created_at": m.get("created_at", ""),
                "owner_ai": m.get("owner_ai", ""),
                "source_ai": m.get("source_ai", ""),
            })
    combined.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return {"memories": combined[:limit], "total": len(combined)}


@app.put("/api/ai-profiles/{ai_id}")
async def api_update_profile(ai_id: str, request: Request, authorization: str = Header(default="")):
    """更新 AI 档案"""
    verify_secret(authorization)
    from ai_profiles import update_profile
    body = await request.json()
    field_map = {"llm_base_url": "model_url", "llm_model": "model_name", "llm_api_key": "model_key"}
    allowed_fields = {"name", "emoji", "color", "platform", "greeting", "persona", "llm_base_url", "llm_model", "llm_api_key"}
    updates = {}
    for k, v in body.items():
        if k in allowed_fields:
            storage_key = field_map.get(k, k)
            if k == "llm_api_key" and not v:
                continue
            updates[storage_key] = v
    result = await update_profile(ai_id, updates)
    return {"status": "ok", "profile": result}


# ── Phase 6 P0: 前端可观测性 API ──

@app.get("/api/memory/{memory_id}/detail")
async def api_memory_detail(memory_id: str, authorization: str = Header(default="")):
    """完整记忆详情：正文 + source_context + history + supersede 链 + 关联记忆"""
    verify_secret(authorization)
    import database as db

    mem = db.get_memory(memory_id)
    if not mem:
        return JSONResponse({"error": "not found"}, status_code=404)

    mem.pop("embedding", None)

    for key in ("domain", "tags", "linked_memories", "supersedes"):
        val = mem.get(key)
        if isinstance(val, str):
            try:
                mem[key] = json.loads(val)
            except Exception:
                mem[key] = []

    supersede_chain = []
    supersedes_ids = mem.get("supersedes") or []
    if isinstance(supersedes_ids, str):
        try:
            supersedes_ids = json.loads(supersedes_ids)
        except Exception:
            supersedes_ids = []
    for sid in supersedes_ids:
        old = db.get_memory(sid)
        if old:
            supersede_chain.append({
                "id": old["id"],
                "content": old["content"],
                "status": old.get("status", ""),
                "created_at": old.get("created_at", ""),
                "direction": "superseded_by_current",
            })
    if mem.get("superseded_by"):
        new = db.get_memory(mem["superseded_by"])
        if new:
            supersede_chain.append({
                "id": new["id"],
                "content": new["content"],
                "status": new.get("status", ""),
                "created_at": new.get("created_at", ""),
                "direction": "supersedes_current",
            })

    tags = mem.get("tags") or []
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except Exception:
            tags = []
    related = []
    if tags:
        tag_set = set(tags)
        for other in db.iter_memories(status="active"):
            if other["id"] == memory_id:
                continue
            other_tags = other.get("tags")
            if isinstance(other_tags, str):
                try:
                    other_tags = json.loads(other_tags)
                except Exception:
                    other_tags = []
            if not other_tags:
                continue
            shared = tag_set & set(other_tags)
            if len(shared) >= 2:
                related.append({
                    "id": other["id"],
                    "content": other["content"][:100],
                    "room": other.get("room", ""),
                    "shared_tags": list(shared),
                    "shared_count": len(shared),
                })
        related.sort(key=lambda x: x["shared_count"], reverse=True)
        related = related[:8]

    from datetime import datetime, timezone
    import math
    from config import DECAY_LAMBDA, DECAY_LAMBDA_FAST, DECAY_THRESHOLD, ROOMS
    try:
        created = datetime.fromisoformat(mem["created_at"])
        days = (datetime.now(timezone.utc) - created).total_seconds() / 86400
    except Exception:
        days = 0
    importance = float(mem.get("importance") or 0.5)
    arousal = float(mem.get("emotion_arousal") or 0.3)
    activations = int(float(mem.get("activation_count") or 0))
    room_cfg = ROOMS.get(mem.get("room", ""), {})
    lam = DECAY_LAMBDA_FAST if room_cfg.get("fast_decay") else DECAY_LAMBDA
    emotion_weight = 1.0 + (arousal * 0.8)
    current_decay = min(1.0, importance * (max(activations, 1) ** 0.3) * math.exp(-lam * days) * emotion_weight)

    return {
        "memory": mem,
        "supersede_chain": supersede_chain,
        "related_memories": related,
        "decay": {
            "current_score": round(current_decay, 4),
            "threshold": DECAY_THRESHOLD,
            "days_alive": round(days, 1),
            "will_archive": current_decay < DECAY_THRESHOLD and mem.get("room") != "living_room",
        },
    }


@app.get("/api/memory/timeline")
async def api_memory_timeline(
    days: int = Query(default=90, le=365),
    authorization: str = Header(default=""),
):
    """按日期分组的记忆摘要 + 每日计数/热度"""
    verify_secret(authorization)
    import database as db
    from datetime import datetime, timezone, timedelta

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()[:10]
    all_mems = db.query_memories(status="active", order_by="created_at DESC")

    by_date = {}
    for m in all_mems:
        created = m.get("created_at", "")[:10]
        if not created or created < cutoff:
            continue
        if created not in by_date:
            by_date[created] = []
        by_date[created].append({
            "id": m["id"],
            "content": m["content"][:80],
            "room": m.get("room", ""),
            "importance": float(m.get("importance") or 0.5),
            "source_ai": m.get("source_ai", ""),
        })

    timeline = []
    for date_str in sorted(by_date.keys(), reverse=True):
        items = by_date[date_str]
        avg_importance = sum(x["importance"] for x in items) / len(items) if items else 0
        timeline.append({
            "date": date_str,
            "count": len(items),
            "heat": round(min(1.0, len(items) / 10.0 + avg_importance * 0.3), 2),
            "memories": items[:6],
        })

    return {"timeline": timeline, "total_days": len(timeline), "date_range": days}


@app.get("/api/memory/graph")
async def api_memory_graph(authorization: str = Header(default="")):
    """记忆星图数据：nodes + edges（共 tag / 同日 / supersede）"""
    verify_secret(authorization)
    import database as db

    all_mems = db.query_memories(status="active", order_by="importance DESC")

    nodes = []
    date_groups = {}
    tag_index = {}

    for m in all_mems:
        mid = m["id"]
        tags_raw = m.get("tags")
        if isinstance(tags_raw, str):
            try:
                tags_raw = json.loads(tags_raw)
            except Exception:
                tags_raw = []
        tags = tags_raw or []

        nodes.append({
            "id": mid,
            "content": m["content"][:60],
            "room": m.get("room", ""),
            "importance": float(m.get("importance") or 0.5),
            "tags": tags[:5],
            "created_at": m.get("created_at", "")[:10],
        })

        date_key = m.get("created_at", "")[:10]
        if date_key:
            date_groups.setdefault(date_key, []).append(mid)

        for tag in tags:
            tag_index.setdefault(tag, []).append(mid)

    edges = []
    edge_set = set()

    for tag, mids in tag_index.items():
        if len(mids) > 20:
            continue
        for i in range(len(mids)):
            for j in range(i + 1, len(mids)):
                pair = tuple(sorted([mids[i], mids[j]]))
                if pair not in edge_set:
                    edge_set.add(pair)
                    edges.append({"source": pair[0], "target": pair[1], "type": "shared_tag", "label": tag})

    for date_key, mids in date_groups.items():
        if len(mids) > 15:
            continue
        for i in range(len(mids)):
            for j in range(i + 1, len(mids)):
                pair = tuple(sorted([mids[i], mids[j]]))
                if pair not in edge_set:
                    edge_set.add(pair)
                    edges.append({"source": pair[0], "target": pair[1], "type": "same_day"})

    for m in all_mems:
        supersedes = m.get("supersedes")
        if isinstance(supersedes, str):
            try:
                supersedes = json.loads(supersedes)
            except Exception:
                supersedes = []
        if supersedes:
            for sid in supersedes:
                pair = tuple(sorted([m["id"], sid]))
                if pair not in edge_set:
                    edge_set.add(pair)
                    edges.append({"source": m["id"], "target": sid, "type": "supersede"})

    return {"nodes": nodes, "edges": edges, "node_count": len(nodes), "edge_count": len(edges)}


@app.get("/api/memory/emotion-map")
async def api_memory_emotion_map(authorization: str = Header(default="")):
    """所有记忆的 valence/arousal 散点数据"""
    verify_secret(authorization)
    import database as db

    all_mems = db.query_memories(status="active")
    points = []
    for m in all_mems:
        valence = float(m.get("valence") or 0.5)
        arousal = float(m.get("emotion_arousal") or 0.3)
        points.append({
            "id": m["id"],
            "content": m["content"][:60],
            "valence": round(valence, 3),
            "arousal": round(arousal, 3),
            "room": m.get("room", ""),
            "importance": float(m.get("importance") or 0.5),
            "source_ai": m.get("source_ai", ""),
            "created_at": m.get("created_at", "")[:10],
        })

    return {"points": points, "count": len(points)}


@app.get("/api/memory/decay-scores")
async def api_memory_decay_scores(authorization: str = Header(default="")):
    """每条记忆的当前衰减分数 + 健康状态"""
    verify_secret(authorization)
    import database as db
    from datetime import datetime, timezone
    import math
    from config import DECAY_LAMBDA, DECAY_LAMBDA_FAST, DECAY_THRESHOLD, ROOMS

    now_dt = datetime.now(timezone.utc)
    results = []

    for m in db.iter_memories(status="active"):
        try:
            created = datetime.fromisoformat(m["created_at"])
            days = (now_dt - created).total_seconds() / 86400
        except Exception:
            days = 0

        importance = float(m.get("importance") or 0.5)
        arousal = float(m.get("emotion_arousal") or 0.3)
        activations = int(float(m.get("activation_count") or 0))
        room_cfg = ROOMS.get(m.get("room", ""), {})
        lam = DECAY_LAMBDA_FAST if room_cfg.get("fast_decay") else DECAY_LAMBDA
        emotion_weight = 1.0 + (arousal * 0.8)
        score = min(1.0, importance * (max(activations, 1) ** 0.3) * math.exp(-lam * days) * emotion_weight)

        if score >= 0.6:
            health = "healthy"
        elif score >= DECAY_THRESHOLD:
            health = "decaying"
        else:
            health = "critical"

        results.append({
            "id": m["id"],
            "content": m["content"][:60],
            "room": m.get("room", ""),
            "decay_score": round(score, 4),
            "health": health,
            "days_alive": round(days, 1),
            "activation_count": activations,
            "last_activated": m.get("last_activated", ""),
            "importance": importance,
        })

    results.sort(key=lambda x: x["decay_score"])
    return {
        "memories": results,
        "summary": {
            "total": len(results),
            "healthy": sum(1 for r in results if r["health"] == "healthy"),
            "decaying": sum(1 for r in results if r["health"] == "decaying"),
            "critical": sum(1 for r in results if r["health"] == "critical"),
            "threshold": DECAY_THRESHOLD,
        },
    }


@app.get("/api/breath-debug")
async def api_breath_debug(
    q: str = Query(..., min_length=1),
    authorization: str = Header(default=""),
):
    """搜索打分分解：向量分/BM25分/精确分/时间衰减/RRF合并分"""
    verify_secret(authorization)
    import database as db
    from embedding import get_embedding
    from memory_ops import _distance_to_cosine, _bm25_score, _exact_match_score, _safe_float, _rrf_merge
    from datetime import datetime, timezone
    import math

    query_vec = await get_embedding(q)

    vec_raw = db.vector_search(query_vec, top_k=30, status="active") if query_vec else []
    vec_details = {}
    for mem in vec_raw:
        distance = mem.pop("distance", 0.0)
        vec_sim = _distance_to_cosine(distance)
        try:
            created = datetime.fromisoformat(mem["created_at"])
            days = (datetime.now(timezone.utc) - created).total_seconds() / 86400
            time_score = math.exp(-0.02 * days)
        except Exception:
            days, time_score = 0, 0.5
        importance = _safe_float(mem.get("importance"), 0.5)
        final = vec_sim * 0.6 + 0.5 * 0.15 + time_score * 0.1 + importance * 0.15
        vec_details[mem["id"]] = {
            "vec_sim": round(vec_sim, 4),
            "time_score": round(time_score, 4),
            "importance": round(importance, 4),
            "vec_final": round(final, 4),
            "days": round(days, 1),
        }

    bm25_raw = db.fts_search(q, top_k=30, status="active")
    bm25_details = {}
    for mem in bm25_raw:
        rank = mem.pop("rank", 0.0)
        bm25 = min(1.0, abs(rank) / 10.0) if rank else 0.0
        bm25_details[mem["id"]] = {"bm25": round(bm25, 4), "raw_rank": round(rank, 4)}

    all_ids = set(vec_details.keys()) | set(bm25_details.keys())
    exact_details = {}
    for mid in all_ids:
        mem = db.get_memory(mid)
        if mem:
            exact = _exact_match_score(q, mem)
            if exact > 0:
                exact_details[mid] = {"exact": round(exact, 4)}

    candidates = {}
    for mid in all_ids:
        mem = db.get_memory(mid)
        if not mem:
            continue
        candidates[mid] = {
            "id": mid,
            "content": mem["content"][:80],
            "room": mem.get("room", ""),
            "scores": {
                "vector": vec_details.get(mid, {}),
                "bm25": bm25_details.get(mid, {}),
                "exact": exact_details.get(mid, {}),
            },
        }

    vec_list = [{"id": mid, "score": d["vec_final"]} for mid, d in sorted(vec_details.items(), key=lambda x: x[1]["vec_final"], reverse=True)]
    bm25_list = [{"id": mid, "score": d["bm25"]} for mid, d in sorted(bm25_details.items(), key=lambda x: x[1]["bm25"], reverse=True)]
    exact_list = [{"id": mid, "score": d["exact"]} for mid, d in sorted(exact_details.items(), key=lambda x: x[1]["exact"], reverse=True)]

    rrf_merged = _rrf_merge(vec_list, bm25_list, exact_list)

    results = []
    for i, item in enumerate(rrf_merged[:15]):
        mid = item["id"]
        entry = candidates.get(mid, {"id": mid, "content": "?", "room": "", "scores": {}})
        entry["rrf_rank"] = i + 1
        entry["rrf_score"] = round(item["score"], 4)
        results.append(entry)

    return {"query": q, "results": results, "paths": {"vector": len(vec_details), "bm25": len(bm25_details), "exact": len(exact_details)}}


# ── 导出 ──

@app.get("/api/export")
async def api_export(authorization: str = Header(default="")):
    verify_secret(authorization)
    data = await memory_ops.export_all()
    return JSONResponse(content=data)


# ── React SPA catch-all（必须在所有 API 路由之后）──

@app.get("/app/{path:path}")
async def spa_catchall(path: str = ""):
    """SPA 路由 fallback：所有 /app/* 请求返回 index.html，由 React Router 处理"""
    spa_index = os.path.join(_SPA_DIR, "index.html")
    if os.path.exists(spa_index):
        return FileResponse(spa_index)
    return JSONResponse({"error": "Frontend not built. Run: cd frontend && npm run build"}, status_code=404)


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
