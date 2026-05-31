"""
Memory Hub - 主服务入口
多 AI 角色共享记忆系统
"""
import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Header, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from typing import Optional

from config import HUB_SECRET, AI_ROLES, ROOMS, register_room, list_rooms
import github_store
import memory_ops
import gateway
import daemon
import corridor
from mcp_server import mcp as mcp_server


# ── 鉴权 ──

def verify_secret(authorization: str = Header(default="")):
    token = authorization.replace("Bearer ", "").strip()
    if token != HUB_SECRET:
        raise HTTPException(status_code=401, detail="Invalid token")


# ── App ──

@asynccontextmanager
async def lifespan(app: FastAPI):
    await github_store.load_all()
    mems = github_store.get_all_memories()
    print(f"[Memory Hub] Loaded {len(mems)} memories from GitHub")
    print(f"[Memory Hub] Roles: {list(AI_ROLES.keys())}")
    print(f"[Memory Hub] Rooms: {list(ROOMS.keys())}")
    yield

app = FastAPI(title="Memory Hub", lifespan=lifespan)

# ── MCP Server 端点 ──
# streamable_http_app() 内部路由是 /mcp，所以 mount 到 "/" 让最终路径为 /mcp
from starlette.routing import Mount
mcp_app = mcp_server.streamable_http_app()
app.router.routes.insert(0, Mount("/", app=mcp_app))

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

@app.post("/api/memory/remember")
async def api_remember(body: RememberRequest, authorization: str = Header(default="")):
    verify_secret(authorization)
    result = await memory_ops.remember(
        content=body.content, layer=body.layer, room=body.room,
        category=body.category, owner_ai=body.owner_ai,
        importance=body.importance, emotion_arousal=body.emotion_arousal,
        source_ai=body.source_ai, source_platform=body.source_platform,
        tags=body.tags,
    )
    return result


class UpdateRequest(BaseModel):
    content: Optional[str] = None
    importance: Optional[float] = None
    room: Optional[str] = None
    category: Optional[str] = None
    tags: Optional[list[str]] = None
    changed_by: str = ""

@app.put("/api/memory/{memory_id}")
async def api_update(memory_id: str, body: UpdateRequest, authorization: str = Header(default="")):
    verify_secret(authorization)
    result = await memory_ops.update_memory(
        memory_id=memory_id, content=body.content, importance=body.importance,
        room=body.room, category=body.category, tags=body.tags,
        changed_by=body.changed_by,
    )
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
    return await gateway.build_context(
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
    return await gateway.post_process(
        user_message=body.user_message,
        ai_response=body.ai_response,
        ai_id=body.ai_id,
        platform=body.platform,
    )


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


# ── 导出 ──

@app.get("/api/export")
async def api_export(authorization: str = Header(default="")):
    verify_secret(authorization)
    data = await memory_ops.export_all()
    return JSONResponse(content=data)


# ── 启动 ──

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8888)
