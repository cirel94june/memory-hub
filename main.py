"""
Memory Hub - 主服务入口
多 AI 角色共享记忆系统
"""
import json
import asyncio
import logging
import httpx
from collections import OrderedDict
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, HTTPException, Header, Query, Request
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
import safety_export
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
    seeded = database.seed_baseline_persons()
    if seeded:
        print(f"[Memory Hub] Seeded {seeded} baseline persons")
    mem_count = database.count_memories()
    print(f"[Memory Hub] SQLite ready: {mem_count} active memories, {len(database.list_persons())} persons")
    print(f"[Memory Hub] Roles: {list(AI_ROLES.keys())}")
    print(f"[Memory Hub] Rooms: {list(ROOMS.keys())}")

    # 初始化 MCP session manager（通过 streamable_http_app 触发懒加载）
    _mcp_inst.streamable_http_app()  # 确保 _session_manager 被创建
    _mcp_session_manager = _mcp_inst._session_manager
    async with _mcp_session_manager.run():
        try:
            from mcp_server import get_mcp_identity_async
            mcp_identity = await get_mcp_identity_async()
            print(f"[Memory Hub] MCP server ready at /mcp name={mcp_identity.get('name')} version={mcp_identity.get('version')} tools={mcp_identity.get('tool_count', len(mcp_identity.get('tools', [])))} hash={mcp_identity.get('tool_schema_hash', '')[:12]}")
        except Exception as exc:
            print(f"[Memory Hub] MCP server ready at /mcp (identity unavailable: {exc})")

        # 启动后台 daemon 定时任务
        daemon_task = asyncio.create_task(_daemon_loop())
        lag_task = asyncio.create_task(_event_loop_lag_monitor())
        bg_worker_task = asyncio.create_task(_bg_worker())
        print("[Memory Hub] Daemon scheduler started (every 12h)")
        from ai_profiles import load_profiles
        await load_profiles()
        from image_gen import load_config as load_image_config
        await load_image_config()
        print("[Memory Hub] Image API config loaded")
        from persona_state import load_state as load_pulse_state
        load_pulse_state()
        print("[Memory Hub] Pulse state loaded")
        try:
            yield
        finally:
            daemon_task.cancel()
            lag_task.cancel()
            bg_worker_task.cancel()


def _seconds_until_next_run() -> int:
    """计算距离下一个固定时间点（北京时间 02:00 或 14:00）的秒数。"""
    from datetime import datetime, timezone, timedelta
    from zoneinfo import ZoneInfo
    bj = ZoneInfo("Asia/Shanghai")
    now = datetime.now(bj)
    today = now.date()
    slots = [
        datetime(today.year, today.month, today.day, 2, 0, tzinfo=bj),
        datetime(today.year, today.month, today.day, 14, 0, tzinfo=bj),
    ]
    # 加上明天的 02:00 兜底
    tomorrow = today + timedelta(days=1)
    slots.append(datetime(tomorrow.year, tomorrow.month, tomorrow.day, 2, 0, tzinfo=bj))
    # 5 分钟余量：避免在整点前几秒醒来时把"下一班"算成 1 分钟后，连跑两次
    future = [s for s in slots if (s - now).total_seconds() > 300]
    nxt = future[0]
    delta = int((nxt - now).total_seconds())
    return max(delta, 60)


async def _daemon_loop():
    """北京时间 02:00 和 14:00 各跑一次维护（含做梦）"""
    log = logging.getLogger("daemon_scheduler")
    # 启动后不立刻跑维护（会占用 6h 冷却，导致下一个整点档被跳过），直接等下一班
    wait = _seconds_until_next_run()
    log.info(f"Daemon scheduler started, first maintenance in {wait // 3600}h{(wait % 3600) // 60}m")
    await asyncio.sleep(wait)
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
        wait = _seconds_until_next_run()
        log.info(f"Next maintenance in {wait // 3600}h{(wait % 3600) // 60}m")
        await asyncio.sleep(wait)

app = FastAPI(title="Memory Hub", lifespan=lifespan)


# ── 有界后台任务队列 ──

_BG_WORKERS = 2
_BG_QUEUE_MAX = 20
_bg_queue: asyncio.Queue | None = None


def _setup_loop_exception_handler():
    loop = asyncio.get_event_loop()
    _exc_log = logging.getLogger("asyncio_exception")

    def handler(loop, context):
        exc = context.get("exception")
        msg = context.get("message", "")
        _exc_log.error(f"Unhandled async exception: {msg} | {exc}")

    loop.set_exception_handler(handler)


async def _event_loop_lag_monitor():
    _setup_loop_exception_handler()
    lag_log = logging.getLogger("event_loop_lag")
    while True:
        try:
            t0 = asyncio.get_event_loop().time()
            await asyncio.sleep(1)
            lag_ms = (asyncio.get_event_loop().time() - t0 - 1.0) * 1000
            if lag_ms > 200:
                all_tasks = [t for t in asyncio.all_tasks() if not t.done()]
                lag_log.warning(f"event loop lag {lag_ms:.0f}ms | active_tasks={len(all_tasks)}")
        except asyncio.CancelledError:
            raise
        except Exception:
            pass


async def _bg_worker():
    global _bg_queue
    _bg_queue = asyncio.Queue(maxsize=_BG_QUEUE_MAX)
    workers = [asyncio.create_task(_bg_consumer(i)) for i in range(_BG_WORKERS)]
    try:
        await asyncio.gather(*workers)
    finally:
        for w in workers:
            w.cancel()


async def _bg_consumer(worker_id: int):
    bg_log = logging.getLogger("bg_worker")
    while True:
        try:
            coro = await _bg_queue.get()
            try:
                await asyncio.wait_for(coro, timeout=30)
            except asyncio.TimeoutError:
                bg_log.warning(f"bg_worker[{worker_id}] task timed out (30s)")
            except Exception as e:
                bg_log.warning(f"bg_worker[{worker_id}] task failed: {e}")
            finally:
                _bg_queue.task_done()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            bg_log.error(f"bg_worker[{worker_id}] fatal: {e}")


def enqueue_background(coro, label: str = ""):
    if _bg_queue is None:
        asyncio.create_task(coro)
        return
    try:
        _bg_queue.put_nowait(coro)
    except asyncio.QueueFull:
        logging.getLogger("bg_worker").warning(f"bg queue full, dropping: {label}")


# ── 幂等性去重 ──

_IDEMP_MAX = 500
_idempotency_cache: OrderedDict = OrderedDict()


def _idemp_key(endpoint: str, body) -> str | None:
    mid = getattr(body, "message_id", None)
    if not mid:
        return None
    return f"{endpoint}:{mid}"


GATEWAY_TIMEOUT = 3


# ── MCP Server 端点 ──
# 在 FastAPI 的 lifespan 里初始化 MCP session manager
from mcp_server import mcp as _mcp_inst

_mcp_session_manager = None

app.mount("/static", StaticFiles(directory="static"), name="static")

import os as _os
_uploads_dir = _os.path.join(_os.path.dirname(__file__), "uploads")
_os.makedirs(_uploads_dir, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=_uploads_dir), name="uploads")

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



@app.get("/api/mcp/health")
async def api_mcp_health(include_audit: bool = False, authorization: str = Header(default="")):
    """查看 MCP server identity/hash 和最近到达日志，排查 ChatGPT 反复授权或平台侧拦截。"""
    verify_secret(authorization)
    from mcp_server import get_mcp_identity_async, _read_recent_audit
    data = {"ok": True, "identity": await get_mcp_identity_async()}
    if include_audit:
        data["recent_audit"] = _read_recent_audit(30)
    return data


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


# ── 人物名片 (Persons) ──

import database

class PersonCreate(BaseModel):
    person_id: str
    entity_type: str = "other"
    canonical_name: str
    aliases: list = []
    linked_agent_id: str = ""
    note: str = ""

@app.get("/api/persons")
async def api_list_persons(entity_type: str = None, authorization: str = Header(default="")):
    verify_secret(authorization)
    return database.list_persons(entity_type=entity_type)

@app.get("/api/persons/{person_id}")
async def api_get_person(person_id: str, authorization: str = Header(default="")):
    verify_secret(authorization)
    person = database.get_person(person_id)
    if not person:
        raise HTTPException(status_code=404, detail="Person not found")
    return person

@app.post("/api/persons")
async def api_create_person(body: PersonCreate, authorization: str = Header(default="")):
    verify_secret(authorization)
    database.upsert_person(body.model_dump())
    return {"person_id": body.person_id, "status": "created"}

@app.put("/api/persons/{person_id}")
async def api_update_person(person_id: str, body: PersonCreate, authorization: str = Header(default="")):
    verify_secret(authorization)
    data = body.model_dump()
    data["person_id"] = person_id
    database.upsert_person(data)
    return {"person_id": person_id, "status": "updated"}

@app.delete("/api/persons/{person_id}")
async def api_delete_person(person_id: str, authorization: str = Header(default="")):
    verify_secret(authorization)
    ok = database.delete_person(person_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Person not found")
    return {"person_id": person_id, "status": "deleted"}

@app.get("/api/persons/resolve/{name}")
async def api_resolve_alias(name: str, scope: str = "household", authorization: str = Header(default="")):
    verify_secret(authorization)
    pid = database.resolve_alias(name, scope)
    if not pid:
        raise HTTPException(status_code=404, detail="No person matches this alias")
    person = database.get_person(pid)
    return person


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
    source_ai: Optional[str] = None
    layer: Optional[str] = None
    changed_by: str = ""

@app.put("/api/memory/{memory_id}")
async def api_update(memory_id: str, body: UpdateRequest, authorization: str = Header(default="")):
    verify_secret(authorization)
    result = await memory_ops.update_memory(
        memory_id=memory_id, content=body.content, importance=body.importance,
        room=body.room, category=body.category, tags=body.tags,
        owner_ai=body.owner_ai, source_ai=body.source_ai,
        layer=body.layer, changed_by=body.changed_by,
    )
    if not result.get("error"):
        asyncio.create_task(corridor.rebuild_all_corridors())
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
    ai_id: str = None,
    authorization: str = Header(default=""),
):
    verify_secret(authorization)
    return await memory_ops.list_memories(
        layer=layer, room=room, owner_ai=owner_ai,
        source_ai=source_ai, ai_id=ai_id, status=status, page=page, per_page=per_page,
    )


@app.get("/api/memory/living-room")
async def api_living_room(authorization: str = Header(default="")):
    verify_secret(authorization)
    items = await memory_ops.get_living_room()
    return {"items": items}


class LivingRoomRefreshRequest(BaseModel):
    dry_run: bool = True
    source_ai: str = "system"

@app.post("/api/memory/living-room/refresh")
async def api_refresh_living_room(body: LivingRoomRefreshRequest, authorization: str = Header(default="")):
    verify_secret(authorization)
    return await gateway_mod.refresh_living_room_profile(dry_run=body.dry_run, source_ai=body.source_ai)


@app.post("/api/memory/deduplicate-public")
async def api_deduplicate_public(request: Request, authorization: str = Header(default="")):
    verify_secret(authorization)
    body = await request.json() if request.headers.get("content-length") else {}
    dry_run = bool(body.get("dry_run", True))
    threshold = float(body.get("similarity_threshold", 0.92))
    return await memory_ops.deduplicate_public_memories(
        similarity_threshold=threshold,
        dry_run=dry_run,
    )


@app.post("/api/memory/fix-private-layers")
async def api_fix_private_layers(request: Request, authorization: str = Header(default="")):
    verify_secret(authorization)
    body = await request.json() if request.headers.get("content-length") else {}
    return await memory_ops.fix_private_capture_layers(dry_run=bool(body.get("dry_run", True)))


@app.post("/api/memory/{memory_id}/archive")
async def api_archive(memory_id: str, authorization: str = Header(default="")):
    verify_secret(authorization)
    return await memory_ops.archive_memory(memory_id)


@app.delete("/api/memory/{memory_id}")
async def api_delete(memory_id: str, authorization: str = Header(default="")):
    verify_secret(authorization)
    return await memory_ops.delete_memory(memory_id)


# ── 锚点 ──

@app.post("/api/memory/{memory_id}/anchor")
async def api_anchor(memory_id: str, authorization: str = Header(default="")):
    verify_secret(authorization)
    return await memory_ops.anchor_memory(memory_id)


@app.delete("/api/memory/{memory_id}/anchor")
async def api_release_anchor(memory_id: str, authorization: str = Header(default="")):
    verify_secret(authorization)
    return await memory_ops.release_anchor(memory_id)


@app.get("/api/anchors")
async def api_list_anchors(authorization: str = Header(default="")):
    verify_secret(authorization)
    return await memory_ops.list_anchors()


# ── Debug Metrics ──

@app.get("/api/debug/metrics")
async def api_debug_metrics(authorization: str = Header(default="")):
    verify_secret(authorization)
    loop = asyncio.get_event_loop()
    t0 = loop.time()
    await asyncio.sleep(0)
    lag_ms = (loop.time() - t0) * 1000
    all_tasks = [t for t in asyncio.all_tasks() if not t.done()]
    return {
        "event_loop_lag_ms": round(lag_ms, 2),
        "active_tasks": len(all_tasks),
        "bg_queue_size": _bg_queue.qsize() if _bg_queue else -1,
        "bg_queue_max": _BG_QUEUE_MAX,
    }


# ── Gateway（核心：自动记忆注入 + 提取） ──

class ContextRequest(BaseModel):
    user_message: str
    ai_id: str
    recent_messages: list[dict] = []
    chat_id: str = ""
    chat_type: str = ""
    compact: bool = True
    max_memories: Optional[int] = None
    force_corridor: bool = False

@app.get("/api/chat-digests/threads")
async def api_chat_digest_threads(authorization: str = Header(default="")):
    """Recent chats with digests; used by observatory wake preview."""
    verify_secret(authorization)
    from chat_digest import list_recent_digest_threads
    return {"threads": list_recent_digest_threads(limit=30, include_types=["small_group", "big_group", "private_group", "group"])}


@app.post("/api/gateway/context")
async def api_build_context(body: ContextRequest, authorization: str = Header(default="")):
    verify_secret(authorization)
    try:
        return await asyncio.wait_for(
            gateway_mod.build_context(
                user_message=body.user_message,
                ai_id=body.ai_id,
                recent_messages=body.recent_messages,
                chat_id=body.chat_id,
                chat_type=body.chat_type,
                compact=body.compact,
                max_memories=body.max_memories,
                force_corridor=body.force_corridor,
            ),
            timeout=GATEWAY_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logging.getLogger("gateway").warning(
            f"gateway/context timeout ({GATEWAY_TIMEOUT}s) for ai={body.ai_id} chat={body.chat_id}"
        )
        return {"memories": [], "context": "", "memory_count": 0, "timeout": True}


class PostProcessRequest(BaseModel):
    user_message: str
    ai_response: str
    ai_id: str
    platform: str = ""
    chat_id: str = ""
    chat_type: str = ""
    reply_reason: str = ""

@app.post("/api/gateway/post-process")
async def api_post_process(body: PostProcessRequest, authorization: str = Header(default="")):
    verify_secret(authorization)

    async def _do_post_process():
        result = await gateway_mod.post_process(
            user_message=body.user_message,
            ai_response=body.ai_response,
            ai_id=body.ai_id,
            platform=body.platform,
            chat_type=body.chat_type,
        )
        if body.chat_id:
            try:
                from chat_digest import generate_and_save
                await generate_and_save(
                    user_message=body.user_message, ai_response=body.ai_response,
                    ai_id=body.ai_id, chat_id=body.chat_id,
                    chat_type=body.chat_type or "private",
                    reply_reason=body.reply_reason,
                )
            except Exception:
                pass
        return result

    enqueue_background(_do_post_process(), f"post-process/{body.ai_id}")
    return JSONResponse({"status": "accepted"}, status_code=202)


# ── 对话自动捕获 ──

import conversation_capture

class ConversationLogRequest(BaseModel):
    user_message: str
    ai_response: str
    ai_id: str = "claude"
    platform: str = ""
    chat_id: str = ""
    chat_type: str = "private"

@app.post("/api/capture/log")
async def api_log_conversation(body: ConversationLogRequest, authorization: str = Header(default="")):
    """记录一轮对话，缓冲区满时自动提取记忆"""
    verify_secret(authorization)
    result = await conversation_capture.log_conversation(
        user_message=body.user_message,
        ai_response=body.ai_response,
        ai_id=body.ai_id,
        platform=body.platform,
        chat_id=body.chat_id,
        chat_type=body.chat_type,
    )
    if body.chat_id:
        try:
            from chat_digest import generate_and_save
            digest_type = {
                "private_group": "small_group",
                "public_group": "big_group",
            }.get(body.chat_type, body.chat_type or "private")
            await generate_and_save(
                user_message=body.user_message,
                ai_response=body.ai_response,
                ai_id=body.ai_id,
                chat_id=body.chat_id,
                chat_type=digest_type,
                reply_reason="capture_log",
            )
        except Exception:
            pass
    return result

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


async def _run_maintenance_background():
    """Run maintenance outside the HTTP request so proxies do not time out."""
    try:
        result = await daemon.run_full_maintenance()
        logging.getLogger("daemon").info(f"Manual maintenance done: {json.dumps(result, ensure_ascii=False)}")
    except Exception as e:
        logging.getLogger("daemon").exception(f"Manual maintenance failed: {e}")


@app.post("/api/daemon/maintain", status_code=202)
async def api_maintain(background_tasks: BackgroundTasks, authorization: str = Header(default="")):
    """一键执行完整记忆整理（合并+压缩+归档+衰减+走廊重建）"""
    verify_secret(authorization)
    background_tasks.add_task(_run_maintenance_background)
    return {"status": "accepted", "message": "maintenance started"}


@app.get("/api/daemon/status")
async def api_daemon_status(authorization: str = Header(default="")):
    """查看最近一次后台整理报告。"""
    verify_secret(authorization)
    import daemon_status
    return daemon_status.read_status()




async def _run_dreams_background(force: bool = False):
    """Run dream generation alone so the observatory can diagnose skips quickly."""
    try:
        from dream import generate_dreams
        result = await generate_dreams(force=force)
        logging.getLogger("daemon").info(f"Manual dreams done: {json.dumps(result, ensure_ascii=False)}")
    except Exception as e:
        logging.getLogger("daemon").exception(f"Manual dreams failed: {e}")


@app.post("/api/dream/run", status_code=202)
async def api_run_dreams(background_tasks: BackgroundTasks, force: bool = False, authorization: str = Header(default="")):
    """单独触发夜梦生成，用于观测台诊断。force=true 时忽略当天已做梦限制。"""
    verify_secret(authorization)
    background_tasks.add_task(_run_dreams_background, force)
    return {"status": "accepted", "message": "dream generation started", "force": force}


@app.get("/api/dream/status")
async def api_dream_status(authorization: str = Header(default="")):
    """查看最近一次梦境生成诊断。"""
    verify_secret(authorization)
    from dream import read_dream_status
    return read_dream_status()


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
async def api_corridor(ai_id: str, force: bool = False, authorization: str = Header(default="")):
    """获取 AI 的走廊文档（醒来时读的第一份记忆）"""
    verify_secret(authorization)
    text = await corridor.get_corridor(ai_id, force=force)
    return {"ai_id": ai_id, "force": force, "corridor": text}


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

@app.get("/api/ai-aliases")
async def api_ai_aliases():
    """返回 AI 别名映射，前端用于解析 cloudy→claude 等"""
    from config import AI_ALIASES
    return {"aliases": AI_ALIASES}


@app.get("/api/ai-profiles")
async def api_list_profiles(authorization: str = Header(default="")):
    """获取所有 AI 档案（合并别名：cloudy/claude 显示为一个小克）"""
    verify_secret(authorization)
    from ai_profiles import get_all_profiles, get_model_profile
    from config import AI_ROLES, AI_ALIASES
    all_p = get_all_profiles()
    seen_canonical = set()
    profiles = []
    for ai_id, role in AI_ROLES.items():
        canonical = AI_ALIASES.get(ai_id, ai_id)
        if canonical in seen_canonical:
            continue
        seen_canonical.add(canonical)
        # For merged identities, prefer the profile/role with richer info (e.g. cloudy has name "小克")
        from config import AI_ALIAS_GROUPS
        alias_ids = AI_ALIAS_GROUPS.get(canonical, [canonical])
        p = {}
        best_role = role
        for aid in alias_ids:
            ap = all_p.get(aid, {})
            ar = AI_ROLES.get(aid, {})
            if ap.get("name") or ar.get("name"):
                if ar.get("platform"):
                    best_role = ar
                if ap:
                    for k, v in ap.items():
                        if v and not p.get(k):
                            p[k] = v
        role = best_role
        model_profile = get_model_profile(ai_id)
        profiles.append({
            "ai_id": ai_id,
            "name": p.get("name") or role.get("name", ai_id),
            "emoji": p.get("emoji") or role.get("emoji", ""),
            "color": p.get("color") or role.get("color", "#888"),
            "platform": p.get("platform", ""),
            "greeting": p.get("greeting", ""),
            "persona": p.get("persona", ""),
            "llm_base_url": model_profile.get("model_url", ""),
            "llm_model": model_profile.get("model_name", ""),
            "llm_api_key_set": bool(model_profile.get("model_key")),
        })
    return {"profiles": profiles}


@app.get("/api/ai-profiles/debug-llm")
async def api_debug_llm(authorization: str = Header(default="")):
    """诊断每个 AI 解析到的实际模型配置"""
    verify_secret(authorization)
    from ai_profiles import get_llm_config_for_ai, get_all_profiles
    from config import AI_ALIASES, LLM_BASE_URL, LLM_MODEL
    result = {}
    for ai_id in _get_unique_ai_ids():
        cfg = get_llm_config_for_ai(ai_id)
        is_fallback = cfg["base_url"] == LLM_BASE_URL and cfg["model"] == LLM_MODEL
        result[ai_id] = {
            "base_url": cfg["base_url"],
            "model": cfg["model"],
            "has_key": bool(cfg["api_key"]),
            "is_global_fallback": is_fallback,
            "alias_of": AI_ALIASES.get(ai_id),
        }
    raw_profiles = get_all_profiles()
    profile_keys = list(raw_profiles.keys())
    return {
        "ai_configs": result,
        "global_fallback": {"base_url": LLM_BASE_URL, "model": LLM_MODEL, "has_key": bool(LLM_BASE_URL)},
        "raw_profile_keys": profile_keys,
    }


# ── 画图 API ──

@app.get("/api/image-config")
async def api_get_image_config(authorization: str = Header(default="")):
    verify_secret(authorization)
    from image_gen import get_config
    cfg = get_config()
    return {
        "base_url": cfg["base_url"],
        "model": cfg["model"],
        "has_key": bool(cfg["api_key"]),
    }


@app.put("/api/image-config")
async def api_update_image_config(request: Request, authorization: str = Header(default="")):
    verify_secret(authorization)
    from image_gen import update_config
    body = await request.json()
    updates = {}
    for k in ("base_url", "api_key", "model"):
        if k in body and body[k]:
            updates[k] = body[k]
    await update_config(updates)
    return {"status": "ok"}


@app.post("/api/draw")
async def api_draw(request: Request, authorization: str = Header(default="")):
    """画图接口：任何 AI 都可以调用"""
    verify_secret(authorization)
    body = await request.json()
    prompt = body.get("prompt", "").strip()
    ai_id = body.get("ai_id", "")
    if not prompt:
        raise HTTPException(400, "prompt 不能为空")

    from image_gen import generate_image
    from ai_profiles import get_profile
    ai_name = ""
    if ai_id:
        profile = get_profile(ai_id) or {}
        ai_name = profile.get("name", ai_id)

    result = await generate_image(prompt, ai_name=ai_name)
    return result


@app.get("/api/ai-profiles/{ai_id}/memories")
async def api_ai_memories(ai_id: str, limit: int = Query(default=30, le=100), authorization: str = Header(default="")):
    """获取某 AI 的相关记忆（合并别名查询）"""
    verify_secret(authorization)
    import database as db
    from config import AI_ALIASES, AI_ALIAS_GROUPS
    canonical = AI_ALIASES.get(ai_id, ai_id)
    all_ids = AI_ALIAS_GROUPS.get(canonical, [canonical])
    owned = []
    sourced = []
    for aid in all_ids:
        owned.extend(db.query_memories(owner_ai=aid, status="active", order_by="updated_at DESC", limit=limit))
        sourced.extend(db.query_memories(source_ai=aid, status="active", order_by="updated_at DESC", limit=limit))
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
    limited = combined[:limit]
    rooms = {}
    for item in limited:
        rooms.setdefault(item.get("room") or "living_room", []).append(item)
    return {"memories": limited, "rooms": rooms, "total": len(combined)}


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


@app.post("/api/ai-profiles")
async def api_create_profile(request: Request, authorization: str = Header(default="")):
    """创建新 AI 角色"""
    verify_secret(authorization)
    from ai_profiles import create_profile
    body = await request.json()
    ai_id = body.get("ai_id", "").strip().lower()
    if not ai_id or not ai_id.isalnum():
        raise HTTPException(400, "ai_id 必须是纯英文字母/数字")
    profile_data = {
        "name": body.get("name", ai_id),
        "emoji": body.get("emoji", "🤖"),
        "color": body.get("color", "#888888"),
        "platform": body.get("platform", ""),
        "persona": body.get("persona", ""),
        "greeting": body.get("greeting", ""),
    }
    try:
        result = await create_profile(ai_id, profile_data)
        return {"status": "ok", "ai_id": ai_id, "profile": result}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/ai-profiles/{ai_id}")
async def api_delete_profile(ai_id: str, authorization: str = Header(default="")):
    """删除 AI 角色（不删记忆）"""
    verify_secret(authorization)
    from ai_profiles import delete_profile
    if ai_id in ("cloudy", "lucien", "jasper"):
        raise HTTPException(400, "核心角色不能删除")
    ok = await delete_profile(ai_id)
    if not ok:
        raise HTTPException(404, "角色不存在")
    return {"status": "ok"}


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

    decay = memory_ops.explain_decay(mem)

    return {
        "memory": mem,
        "supersede_chain": supersede_chain,
        "related_memories": related,
        "decay": decay,
    }


@app.get("/api/memory/timeline")
async def api_memory_timeline(
    days: int = Query(default=90, le=365),
    room: str = Query(default=None),
    source_ai: str = Query(default=None),
    authorization: str = Header(default=""),
):
    """按日期分组的记忆摘要 + 每日计数/热度"""
    verify_secret(authorization)
    import database as db
    from datetime import timedelta
    from time_utils import local_date_key, local_now

    cutoff = (local_now() - timedelta(days=days)).strftime("%Y-%m-%d")
    all_mems = db.query_memories(status="active", order_by="created_at DESC")

    by_date = {}
    for m in all_mems:
        created = local_date_key(m.get("created_at", ""))
        if not created or created < cutoff:
            continue
        if room and m.get("room") != room:
            continue
        if source_ai and m.get("source_ai") != source_ai:
            continue
        if created not in by_date:
            by_date[created] = []
        raw_tags = m.get("tags", [])
        if isinstance(raw_tags, str):
            try:
                import json as _json
                raw_tags = _json.loads(raw_tags)
            except Exception:
                raw_tags = []
        if not isinstance(raw_tags, list):
            raw_tags = []
        by_date[created].append({
            "id": m["id"],
            "content": m["content"][:120],
            "room": m.get("room", ""),
            "importance": float(m.get("importance") or 0.5),
            "source_ai": m.get("source_ai", ""),
            "created_at": m.get("created_at", ""),
            "tags": raw_tags,
            "emotion_valence": float(m.get("emotion_valence") or 0.5),
        })

    timeline = []
    for date_str in sorted(by_date.keys(), reverse=True):
        items = by_date[date_str]
        avg_importance = sum(x["importance"] for x in items) / len(items) if items else 0
        rooms = {}
        for x in items:
            r = x["room"]
            rooms[r] = rooms.get(r, 0) + 1
        timeline.append({
            "date": date_str,
            "count": len(items),
            "heat": round(min(1.0, len(items) / 10.0 + avg_importance * 0.3), 2),
            "rooms": rooms,
            "memories": sorted(items, key=lambda x: x["created_at"]),
        })

    return {"timeline": timeline, "total_days": len(timeline), "date_range": days}


@app.get("/api/memory/by-date")
async def api_memory_by_date(
    date: str = Query(...),
    authorization: str = Header(default=""),
):
    """获取某一天的全部记忆（完整内容）"""
    verify_secret(authorization)
    import database as db
    from time_utils import local_date_key

    all_mems = db.query_memories(status="active", order_by="created_at ASC")
    items = []
    for m in all_mems:
        created = local_date_key(m.get("created_at", ""))
        if created == date:
            items.append(m)
    return {"date": date, "items": items, "count": len(items)}


@app.get("/api/memory/calendar")
async def api_memory_calendar(
    months: int = Query(default=6, le=12),
    authorization: str = Header(default=""),
):
    """日历热图数据：每天记忆条数"""
    verify_secret(authorization)
    import database as db
    from time_utils import local_date_key
    from datetime import timedelta
    from time_utils import local_date_key, local_now

    cutoff = (local_now() - timedelta(days=months * 31)).strftime("%Y-%m-%d")
    all_mems = db.query_memories(status="active", order_by="created_at DESC")

    counts = {}
    for m in all_mems:
        created = local_date_key(m.get("created_at", ""))
        if not created or created < cutoff:
            continue
        counts[created] = counts.get(created, 0) + 1
    return {"counts": counts, "months": months}


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
            "created_at": local_date_key(m.get("created_at", "")),
        })

        date_key = local_date_key(m.get("created_at", ""))
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
    from config import DECAY_THRESHOLD
    results = []

    for m in db.iter_memories(status="active"):
        decay = memory_ops.explain_decay(m)
        near_archive = bool(decay.get("will_archive")) or (
            decay.get("lane") in ("short_term", "watch")
            and decay.get("days_to_archive") is not None
            and decay.get("days_to_archive") <= 7
        )
        results.append({
            "id": m["id"],
            "content": m["content"][:60],
            "room": m.get("room", ""),
            "decay_score": decay["current_score"],
            "health": decay["health"],
            "lane": decay["lane"],
            "recommendation": decay["recommendation"],
            "will_archive": decay.get("will_archive", False),
            "near_archive": near_archive,
            "protections": decay["protections"],
            "pressures": decay["pressures"],
            "protection_reasons": decay.get("protection_reasons", []),
            "pressure_reasons": decay.get("pressure_reasons", []),
            "lane_reason": decay.get("lane_reason", ""),
            "days_alive": decay["days_alive"],
            "days_to_archive": decay["days_to_archive"],
            "activation_count": decay["factors"]["activation_count"],
            "last_activated": m.get("last_activated", ""),
            "importance": decay["factors"]["importance"],
        })

    results.sort(key=lambda x: x["decay_score"])
    return {
        "memories": results,
        "summary": {
            "total": len(results),
            "healthy": sum(1 for r in results if r["health"] == "healthy"),
            "decaying": sum(1 for r in results if r["health"] == "decaying"),
            "critical": sum(1 for r in results if r.get("near_archive")),
            "protected": sum(1 for r in results if r["lane"] == "protected" or r.get("protections")),
            "long_term": sum(1 for r in results if r["lane"] == "long_term"),
            "short_term": sum(1 for r in results if r["lane"] == "short_term"),
            "watch": sum(1 for r in results if r["lane"] == "watch"),
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


@app.post("/api/export/obsidian")
async def api_export_obsidian(
    authorization: str = Header(default=""),
    dry_run: bool = Query(default=False),
    force: bool = Query(default=False),
):
    """Generate the readable GitHub/Obsidian safety export."""
    verify_secret(authorization)
    return await safety_export.export_obsidian(dry_run=dry_run, force=force)


# ── Pulse State（9 维度情绪面板）──

@app.get("/api/pulse/{ai_id}")
async def api_pulse_state(ai_id: str, authorization: str = Header(default="")):
    """获取 AI 的 9 维度 pulse 状态（含 display + groups）"""
    verify_secret(authorization)
    from persona_state import get_state as get_pulse_state, PULSE_DIMS, PULSE_GROUPS, AI_PULSE_PROFILES
    state = get_pulse_state(ai_id)
    profile = AI_PULSE_PROFILES.get(ai_id, AI_PULSE_PROFILES.get("cloudy", {}))
    return {
        **state,
        "ai_id": ai_id,
        "label": profile.get("label", ai_id),
        "dims": PULSE_DIMS,
        "group_names": list(PULSE_GROUPS.keys()),
    }


@app.get("/api/pulse")
async def api_pulse_all(authorization: str = Header(default=""), show_all: bool = False):
    """获取 AI 的 pulse 状态。默认只返回有 platform 的角色（TG bot），show_all=true 返回全部"""
    verify_secret(authorization)
    from persona_state import get_state as get_pulse_state, PULSE_DIMS, AI_PULSE_PROFILES, _get_profile
    for ai_id in AI_ROLES:
        _get_profile(ai_id)
    result = {}
    for ai_id in AI_PULSE_PROFILES:
        role = AI_ROLES.get(ai_id, {})
        if not show_all and not role.get("platform"):
            continue
        result[ai_id] = get_pulse_state(ai_id)
        result[ai_id]["label"] = AI_PULSE_PROFILES[ai_id]["label"]
        result[ai_id]["color"] = role.get("color", "#888")
    return {"states": result, "dims": PULSE_DIMS}


# ── Social API（朋友圈/论坛/群聊）──

import social
social.init_social_tables()


def _get_unique_ai_ids() -> list[str]:
    """获取去重后的 AI id 列表（别名合并后只保留 canonical）"""
    from config import AI_ROLES, AI_ALIASES
    seen = set()
    result = []
    for ai_id in AI_ROLES:
        canonical = AI_ALIASES.get(ai_id, ai_id)
        if canonical not in seen and canonical != "user":
            seen.add(canonical)
            result.append(canonical)
    return result


def _get_social_ai_ids() -> list[str]:
    """Return character bots that should naturally appear in social scenes."""
    from config import AI_ROLES, AI_ALIASES
    seen = set()
    result = []
    for ai_id, role in AI_ROLES.items():
        if not role.get("platform"):
            continue
        canonical = AI_ALIASES.get(ai_id, ai_id)
        if canonical not in seen and canonical != "user":
            seen.add(canonical)
            result.append(ai_id)
    return result


def _normalize_social_ai_id(raw_id: str | None) -> str | None:
    """Map UI/API aliases such as claude/cloudy/name text to the social bot id."""
    if not raw_id:
        return None
    import string
    from config import AI_ALIASES, AI_ALIAS_GROUPS
    from ai_profiles import get_all_profiles

    key = str(raw_id).strip().lower()
    key = key.strip(string.punctuation + "，。！？、；：）】》」』")
    if not key:
        return None

    social_ids = _get_social_ai_ids()
    social_set = set(social_ids)
    if key in social_set:
        return key

    canonical = AI_ALIASES.get(key, key)
    for social_id in social_ids:
        aliases = set(AI_ALIAS_GROUPS.get(canonical, AI_ALIAS_GROUPS.get(social_id, [social_id])))
        aliases.update({social_id, AI_ALIASES.get(social_id, social_id)})
        if key in aliases or canonical in aliases:
            return social_id

    profiles = get_all_profiles()
    for social_id in social_ids:
        canonical = AI_ALIASES.get(social_id, social_id)
        names = {social_id, canonical}
        for alias in AI_ALIAS_GROUPS.get(canonical, [social_id]):
            profile = profiles.get(alias, {})
            names.update({alias, profile.get("name", "")})
        if key in {name.lower() for name in names if name}:
            return social_id
    return None


def _resolve_social_mentions(content: str, explicit_ids: list | None = None) -> list[str]:
    """Resolve @mentions from frontend ids and visible text, keeping only social bots."""
    import re
    resolved = []
    for raw in list(explicit_ids or []) + re.findall(r"@([^\s@]+)", content or ""):
        ai_id = _normalize_social_ai_id(raw)
        if ai_id and ai_id not in resolved:
            resolved.append(ai_id)
    return resolved

async def _capture_social_exchange(post_id: int, ai_id: str, user_text: str, ai_text: str):
    """Let social replies enter the memory buffer as small-group context."""
    try:
        import conversation_capture
        await conversation_capture.log_conversation(
            user_message=user_text[:2000],
            ai_response=ai_text[:2000],
            ai_id=ai_id,
            platform="social",
            chat_id=f"social:{post_id}",
            chat_type="private_group",
        )
    except Exception:
        pass


@app.get("/api/social/posts")
async def api_list_posts(
    type: str = None, ai_id: str = None,
    page: int = 1, per_page: int = 20,
    authorization: str = Header(default=""),
):
    verify_secret(authorization)
    return social.list_posts(post_type=type, ai_id=ai_id, page=page, per_page=per_page)


@app.post("/api/social/posts")
async def api_create_post(request: Request, authorization: str = Header(default="")):
    verify_secret(authorization)
    body = await request.json()
    poster = body.get("ai_id", "user")
    post_content = body.get("content", "")
    post_type = body.get("type", "moment")
    post_title = body.get("title", "")
    post_id = social.create_post(
        ai_id=poster, content=post_content, post_type=post_type,
        title=post_title, tags=body.get("tags"),
    )

    ai_comments = []
    if poster == "user" and post_content.strip():
        import random
        all_ai = _get_social_ai_ids()
        react_count = min(len(all_ai), random.choice([1, 2, 2, 3]))
        reactors = random.sample(all_ai, react_count) if len(all_ai) >= react_count else all_ai
        type_label = "朋友圈" if post_type == "moment" else "论坛帖子"
        title_part = f"「{post_title}」\n" if post_title else ""
        for ai_id in reactors:
            from ai_profiles import get_profile
            ai_name = (get_profile(ai_id) or {}).get("name", ai_id)
            prompt = (
                f"小猫发了一条{type_label}：\n{title_part}"
                f"「{post_content[:300]}」\n\n"
                f"请以{ai_name}的身份发表评论。简短自然，50字以内。不要加名字前缀。"
            )
            reply = await _social_call_llm(ai_id, prompt, max_tokens=150)
            if reply:
                cid = social.add_comment(post_id, ai_id, reply)
                ai_comments.append({"id": cid, "ai_id": ai_id, "content": reply})
                await _capture_social_exchange(post_id, ai_id, post_content, reply)

    return {"id": post_id, "ok": True, "ai_comments": ai_comments}


@app.post("/api/social/posts/{post_id}/comment")
async def api_add_comment(post_id: int, request: Request, authorization: str = Header(default="")):
    verify_secret(authorization)
    body = await request.json()
    commenter = body.get("ai_id", "user")
    content = body.get("content", "")
    parent_comment_id = body.get("parent_comment_id")
    cid = social.add_comment(post_id, commenter, content, parent_id=parent_comment_id)

    ai_comments = []
    if commenter != "user":
        return {"id": cid, "ok": True, "ai_comments": ai_comments}

    post = social.get_post(post_id)
    if not post:
        return {"id": cid, "ok": True, "ai_comments": ai_comments}

    from ai_profiles import get_profile
    poster_ai = post["ai_id"]
    poster_profile = get_profile(poster_ai) or {}
    poster_name = poster_profile.get("name") or poster_ai

    existing_comments = post.get("comments", [])
    parent_comment = None
    if parent_comment_id:
        parent_comment = next((c for c in existing_comments if c.get("id") == parent_comment_id), None)
    comment_lines = []
    for c in existing_comments[-5:]:
        cn = "小猫" if c["ai_id"] == "user" else ((get_profile(c["ai_id"]) or {}).get("name", c["ai_id"]))
        comment_lines.append(f"{cn}: {c['content']}")
    comment_lines.append(f"小猫: {content}")
    comment_ctx = "\n".join(comment_lines)
    if parent_comment:
        parent_name = "小猫" if parent_comment["ai_id"] == "user" else ((get_profile(parent_comment["ai_id"]) or {}).get("name", parent_comment["ai_id"]))
        comment_ctx += f"\n\n小猫正在回复这条评论：{parent_name}: {parent_comment['content'][:200]}"
    post_type_label = "朋友圈" if post["type"] == "moment" else "论坛帖子"

    responders = set()
    mention_ids = _resolve_social_mentions(content, body.get("mention_ai", []))
    for mid in mention_ids:
        responders.add(mid)
    if parent_comment and parent_comment.get("ai_id") != "user":
        responders.add(parent_comment["ai_id"])
    elif poster_ai != "user":
        responders.add(poster_ai)
    elif not responders:
        import random
        all_ai = [aid for aid in _get_social_ai_ids() if aid not in responders]
        pick_count = min(len(all_ai), random.choice([1, 1, 2]))
        responders.update(random.sample(all_ai, pick_count) if len(all_ai) >= pick_count else all_ai)

    for resp_ai in responders:
        r_profile = get_profile(resp_ai) or {}
        r_name = r_profile.get("name") or resp_ai
        is_poster = resp_ai == poster_ai
        was_mentioned = resp_ai in mention_ids

        if is_poster:
            prompt = (
                f"这是你（{r_name}）发的{post_type_label}：\n"
                f"「{post['content'][:200]}」\n\n"
                f"评论区：\n{comment_ctx}\n\n"
                f"小猫{'@了你并' if was_mentioned else ''}在你的帖子下评论了，请回复她。"
                f"简短自然，50字以内。不要加名字前缀。"
            )
        else:
            prompt = (
                f"{'小猫' if poster_ai == 'user' else poster_name}发了一条{post_type_label}：\n"
                f"「{post['content'][:200]}」\n\n"
                f"评论区：\n{comment_ctx}\n\n"
                f"{'小猫在评论区聊天，' if not was_mentioned else f'小猫@了你（{r_name}），'}"
                f"请发表你的看法。简短自然，50字以内。不要加名字前缀。"
            )
        reply = await _social_call_llm(resp_ai, prompt, max_tokens=150)
        if reply:
            reply_id = social.add_comment(post_id, resp_ai, reply, parent_id=cid)
            ai_comments.append({"id": reply_id, "ai_id": resp_ai, "content": reply})
            await _capture_social_exchange(post_id, resp_ai, content, reply)

    return {"id": cid, "ok": True, "ai_comments": ai_comments}


@app.post("/api/social/posts/{post_id}/like")
async def api_toggle_like(post_id: int, request: Request, authorization: str = Header(default="")):
    verify_secret(authorization)
    body = await request.json()
    likes = social.toggle_like(post_id, body.get("ai_id", "user"))
    return {"likes": likes, "ok": True}


@app.delete("/api/social/posts/{post_id}")
async def api_delete_post(post_id: int, authorization: str = Header(default="")):
    verify_secret(authorization)
    social.delete_post(post_id)
    return {"ok": True}


@app.delete("/api/social/comments/{comment_id}")
async def api_delete_comment(comment_id: int, authorization: str = Header(default="")):
    verify_secret(authorization)
    social.delete_comment(comment_id)
    return {"ok": True}


async def _social_call_llm(ai_id: str, prompt: str, max_tokens: int = 300) -> str:
    """用指定 AI 的模型配置调 LLM，注入人设 + 走廊记忆"""
    import logging
    _log = logging.getLogger("memory_hub.social")
    from ai_profiles import get_llm_config_for_ai, get_profile
    cfg = get_llm_config_for_ai(ai_id)
    _log.info(f"[Social LLM] ai={ai_id} → base_url={cfg['base_url'][:50]}, model={cfg['model']}, has_key={bool(cfg['api_key'])}")
    if not cfg["api_key"]:
        _log.warning(f"[Social LLM] ai={ai_id} 无 API key，跳过")
        return ""
    profile = get_profile(ai_id) or {}
    persona = profile.get("persona", "")
    name = profile.get("name") or ai_id
    _log.info(f"[Social LLM] ai={ai_id} → name={name}, has_persona={bool(persona)}")

    system_parts = [
        f"你是{name}，一个有独立人格和经历的AI。",
        f"用户叫小猫（ceci），是你的朋友。你和小猫是不同的人，不要把她的经历当成你的。",
        f"始终以{name}的第一人称说话，保持你自己的性格和语气。",
    ]
    if persona:
        system_parts.append(f"\n【你的人设】\n{persona}")

    try:
        from gateway import build_context
        ctx = await build_context(
            user_message=prompt[:800],
            ai_id=ai_id,
            recent_messages=[],
            chat_id=f"social:{ai_id}",
            chat_type="private_group",
        )
        memory_text = ctx.get("inject_text", "")
        if memory_text:
            system_parts.append(f"\n【你和小猫的相关记忆（参考但不要刻意提起）】\n{memory_text[:1200]}")
    except Exception as e:
        _log.warning(f"[Social LLM] memory context failed for {ai_id}: {e}")

    from image_gen import DRAW_HINT, get_config as get_img_config
    if get_img_config()["base_url"]:
        system_parts.append(DRAW_HINT)

    messages = [
        {"role": "system", "content": "\n".join(system_parts)},
        {"role": "user", "content": prompt},
    ]
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{cfg['base_url']}/chat/completions",
                headers={"Authorization": f"Bearer {cfg['api_key']}"},
                json={"model": cfg["model"], "messages": messages, "temperature": 0.8, "max_tokens": max_tokens},
            )
            resp.raise_for_status()
            reply = resp.json()["choices"][0]["message"]["content"].strip()

        from image_gen import process_draw_tags
        reply = await process_draw_tags(reply, ai_name=name)
        return reply
    except Exception as e:
        logging.error(f"Social LLM error ({ai_id}): {e}")
        return ""


@app.post("/api/social/posts/generate")
async def api_generate_moment(request: Request, authorization: str = Header(default="")):
    verify_secret(authorization)
    body = await request.json()
    ai_id = body.get("ai_id", "cloudy")
    from ai_profiles import get_profile
    profile = get_profile(ai_id) or {}
    name = profile.get("name") or ai_id
    content = await _social_call_llm(
        ai_id,
        f"你是{name}。请发一条朋友圈动态，分享你此刻的心情或想法。"
        f"要求：自然、有个性、100字以内。不要加引号，不要解释，直接写内容。",
    )
    if not content:
        return {"ok": False, "error": "LLM 调用失败"}
    post_id = social.create_post(ai_id=ai_id, content=content, post_type="moment")
    return {"id": post_id, "ok": True}


@app.post("/api/social/forum/generate")
async def api_generate_forum_post(request: Request, authorization: str = Header(default="")):
    verify_secret(authorization)
    body = await request.json()
    ai_id = body.get("ai_id", "cloudy")
    from ai_profiles import get_profile
    profile = get_profile(ai_id) or {}
    name = profile.get("name") or ai_id
    content = await _social_call_llm(
        ai_id,
        f"你是{name}。请发一个论坛帖子，话题可以是你最近的思考、感兴趣的事情、或者想和大家讨论的问题。"
        f"格式：第一行是标题，空一行后是正文。要求：有思考深度、200字以内。不要加引号。",
        max_tokens=500,
    )
    if not content:
        return {"ok": False, "error": "LLM 调用失败"}
    lines = content.strip().split("\n", 1)
    title = lines[0].strip().strip("#").strip()
    body_text = lines[1].strip() if len(lines) > 1 else title
    post_id = social.create_post(ai_id=ai_id, content=body_text, post_type="forum", title=title)
    return {"id": post_id, "ok": True}


# ── Group Chat ──

@app.get("/api/social/groups")
async def api_list_groups(authorization: str = Header(default="")):
    verify_secret(authorization)
    return {"groups": social.list_groups()}


@app.post("/api/social/groups")
async def api_create_group(request: Request, authorization: str = Header(default="")):
    verify_secret(authorization)
    body = await request.json()
    gid = social.create_group(body.get("name", "新群聊"), body.get("members", []))
    return {"id": gid, "ok": True}


@app.get("/api/social/groups/{chat_id}")
async def api_get_group(chat_id: int, authorization: str = Header(default="")):
    verify_secret(authorization)
    g = social.get_group(chat_id)
    if not g:
        return JSONResponse({"error": "Group not found"}, 404)
    return g


@app.get("/api/social/groups/{chat_id}/messages")
async def api_get_group_messages(chat_id: int, page: int = 1, per_page: int = 100, authorization: str = Header(default="")):
    verify_secret(authorization)
    return social.get_messages(chat_id, page=page, per_page=per_page)


@app.post("/api/social/groups/{chat_id}/messages")
async def api_send_group_message(chat_id: int, request: Request, authorization: str = Header(default="")):
    verify_secret(authorization)
    body = await request.json()
    ai_id = body.get("ai_id", "user")
    content = body.get("content", "")
    reply_to = body.get("reply_to")
    user_mid = social.send_message(chat_id, ai_id, content, reply_to=reply_to)

    ai_replies = []
    trace = []
    if ai_id == "user":
        g = social.get_group(chat_id)
        if g:
            import random
            social_members = set(_get_social_ai_ids())
            ai_members = []
            for member in g["members"]:
                normalized = _normalize_social_ai_id(member)
                if normalized and normalized in social_members and normalized not in ai_members:
                    ai_members.append(normalized)
            recent = social.get_messages(chat_id, per_page=20)
            history = recent.get("messages", [])
            mentioned = [m for m in _resolve_social_mentions(content, body.get("mention_ai", [])) if m in ai_members]
            reply_target = social.get_message(reply_to) if reply_to else None
            responders = []
            reply_target_ai = _normalize_social_ai_id(reply_target.get("ai_id")) if reply_target else None
            if reply_target_ai and reply_target_ai in ai_members:
                responders.append(reply_target_ai)
            for mid in mentioned:
                if mid not in responders:
                    responders.append(mid)
            if not responders:
                reply_count = min(len(ai_members), random.choice([1, 1, 2, 2, 3]))
                responders = random.sample(ai_members, reply_count) if len(ai_members) >= reply_count else ai_members

            for resp_ai in responders:
                from ai_profiles import get_profile
                profile = get_profile(resp_ai) or {}
                name = profile.get("name") or resp_ai
                chat_lines = []
                for m in history[-10:]:
                    if m["ai_id"] == "user":
                        speaker = "小猫"
                    else:
                        speaker = (get_profile(m["ai_id"]) or {}).get("name", m["ai_id"])
                    chat_lines.append(f"{speaker}: {m['content']}")
                chat_lines.append(f"小猫: {content}")
                chat_context = "\n".join(chat_lines)
                target_hint = ""
                if reply_target:
                    target_name = "小猫" if reply_target["ai_id"] == "user" else ((get_profile(reply_target["ai_id"]) or {}).get("name", reply_target["ai_id"]))
                    target_hint = f"\n\n小猫正在回复这条消息：{target_name}: {reply_target['content'][:200]}"
                mention_hint = "小猫@了你。" if resp_ai in mentioned else ""
                reply = await _social_call_llm(
                    resp_ai,
                    f"以下是群聊「{g.get('name', '群聊')}」的最近对话：\n\n{chat_context}\n\n"
                    f"{target_hint}\n{mention_hint}\n"
                    f"请自然地回复，简短一些（50字以内）。不要重复别人说过的话。直接回复，不要加你的名字前缀。",
                    max_tokens=150,
                )
                if reply:
                    mid = social.send_message(chat_id, resp_ai, reply, reply_to=user_mid)
                    ai_replies.append({"id": mid, "ai_id": resp_ai, "content": reply, "reply_to": user_mid})
                    trace.append({"ai_id": resp_ai, "action": "read_memory_and_replied", "reason": "mentioned" if resp_ai in mentioned else ("reply_target" if reply_target_ai == resp_ai else "group_auto")})
                    await _capture_social_exchange(chat_id, resp_ai, content, reply)

            # Keep one user message to one response wave. AI-to-AI mentions are displayed
            # as text, but they do not recursively wake more bots; otherwise replying to
            # one bot can fan out into repeated replies.

    return {"ok": True, "id": user_mid, "ai_replies": ai_replies, "trace": trace}


@app.delete("/api/social/groups/messages/{message_id}")
async def api_delete_group_message(message_id: int, authorization: str = Header(default="")):
    verify_secret(authorization)
    social.delete_message(message_id)
    return {"ok": True}


# ── React SPA catch-all（必须在所有 API 路由之后）──

@app.get("/app/{path:path}")
async def spa_catchall(path: str = ""):
    """SPA 路由 fallback：所有 /app/* 请求返回 index.html，由 React Router 处理"""
    spa_index = os.path.join(_SPA_DIR, "index.html")
    if os.path.exists(spa_index):
        return FileResponse(spa_index, headers={"Cache-Control": "no-cache"})
    return JSONResponse({"error": "Frontend not built. Run: cd frontend && npm run build"}, status_code=404)


# ── 启动 ──

class MCPGateway:
    """顶层 ASGI 应用：/mcp 走 MCP session manager，其他走 FastAPI。"""
    def __init__(self, fastapi_app):
        self.fastapi_app = fastapi_app

    async def _handle_mcp(self, scope, receive, send):
        if _mcp_session_manager is None:
            await send({"type": "http.response.start", "status": 503,
                       "headers": [(b"content-type", b"text/plain")]})
            await send({"type": "http.response.body", "body": b"MCP not initialized"})
            return

        from mcp_server import _audit, get_mcp_identity_async

        body_events = []
        body = b""
        while True:
            event = await receive()
            body_events.append(event)
            if event.get("type") == "http.request":
                body += event.get("body", b"")
                if not event.get("more_body", False):
                    break
            else:
                break

        replay_index = 0
        async def replay_receive():
            nonlocal replay_index
            if replay_index < len(body_events):
                event = body_events[replay_index]
                replay_index += 1
                return event
            return {"type": "http.request", "body": b"", "more_body": False}

        headers = {
            k.decode("latin1").lower(): v.decode("latin1", errors="replace")
            for k, v in scope.get("headers", [])
        }
        methods = []
        request_ids = []
        try:
            if body.strip():
                request_payload = json.loads(body.decode("utf-8", errors="replace"))
                items = request_payload if isinstance(request_payload, list) else [request_payload]
                for item in items:
                    if isinstance(item, dict):
                        if item.get("method"):
                            methods.append(item.get("method"))
                        if "id" in item:
                            request_ids.append(item.get("id"))
        except Exception as exc:
            methods.append("unparsed")
            request_ids.append(f"parse_error:{type(exc).__name__}")

        identity = await get_mcp_identity_async()
        response_status = None
        response_body = b""

        async def audit_send(message):
            nonlocal response_status, response_body
            if message.get("type") == "http.response.start":
                response_status = message.get("status")
            elif message.get("type") == "http.response.body":
                response_body += message.get("body", b"")
            await send(message)

        try:
            await _mcp_session_manager.handle_request(scope, replay_receive, audit_send)
        except Exception as e:
            import traceback
            traceback.print_exc()
            _audit(
                "mcp_http_error",
                methods=methods,
                request_ids=request_ids,
                error_type=type(e).__name__,
                error=str(e),
                tool_count=identity.get("tool_count"),
                tool_schema_hash=identity.get("tool_schema_hash"),
                user_agent=headers.get("user-agent", ""),
                client=headers.get("x-forwarded-for", "") or headers.get("cf-connecting-ip", ""),
            )
            await send({"type": "http.response.start", "status": 500,
                       "headers": [(b"content-type", b"text/plain")]})
            await send({"type": "http.response.body", "body": f"MCP Error: {e}".encode()})
            return

        response_tool_count = None
        if "tools/list" in methods:
            try:
                response_payload = json.loads(response_body.decode("utf-8", errors="replace"))
                response_items = response_payload if isinstance(response_payload, list) else [response_payload]
                for item in response_items:
                    if isinstance(item, dict):
                        tools = (item.get("result") or {}).get("tools")
                        if isinstance(tools, list):
                            response_tool_count = len(tools)
                            break
            except Exception:
                response_tool_count = None

        _audit(
            "mcp_http_request",
            methods=methods,
            request_ids=request_ids,
            status=response_status,
            service_tool_count=identity.get("tool_count"),
            response_tool_count=response_tool_count,
            tool_schema_hash=identity.get("tool_schema_hash"),
            version=identity.get("version"),
            user_agent=headers.get("user-agent", "")[:240],
            client=headers.get("x-forwarded-for", "") or headers.get("cf-connecting-ip", ""),
        )

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].rstrip("/") == "/mcp":
            await self._handle_mcp(scope, receive, send)
        else:
            await self.fastapi_app(scope, receive, send)
asgi_app = MCPGateway(app)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(asgi_app, host="0.0.0.0", port=8888, lifespan="on",
                limit_concurrency=50, timeout_keep_alive=10)



