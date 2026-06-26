"""
Memory Hub Proxy：OpenAI 兼容代理
自动为任何 AI 注入记忆 + 自动提取记忆，不依赖 AI 主动调工具。

工作流程：
  客户端 → POST /v1/chat/completions → Proxy
  1. 拦截请求，从 Memory Hub 获取记忆上下文
  2. 把记忆注入到 system prompt 中
  3. 转发给真正的 AI API（用户配置的 base_url）
  4. 拿到 AI 回复后，后台自动提取记忆
  5. 把 AI 回复返回给客户端

客户端只需要把 base_url 指向 Memory Hub 的 /v1 即可。
"""
import json
import time
import asyncio
import logging
from typing import Optional

import httpx
from fastapi import Request, Header, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel

from config import HUB_SECRET, LLM_BASE_URL, LLM_API_KEY
from ai_profiles import get_llm_config_for_ai
import gateway as gateway_mod
import conversation_capture

logger = logging.getLogger("memory_hub.proxy")


class ProxyConfig(BaseModel):
    """代理转发配置，通过请求头或查询参数传递"""
    target_base_url: str = ""   # 真正的 AI API 地址
    target_api_key: str = ""    # 真正的 AI API key
    ai_id: str = "claude"       # Memory Hub 中的 AI 身份
    platform: str = "proxy"     # 来源平台标识
    inject_memory: bool = True  # 是否注入记忆
    extract_memory: bool = True # 是否自动提取记忆
    chat_id: str = ""           # 聊天窗口ID（跨窗口感知用）
    chat_type: str = ""         # private / private_group / public_group


def _extract_proxy_config(request: Request, headers: dict) -> ProxyConfig:
    """从请求头提取代理配置

    支持两种模式：

    【简单模式】— 适合 RikkaHub 等不支持自定义头的客户端
    只需设置 API Key 为 "hub密码:AI身份"，服务端自动用 .env 里的 LLM 配置转发：
      API Base URL: http://172.245.180.158:8888/v1
      API Key: xiaoke588887:rikkahub

    【完整模式】— 通过自定义请求头控制一切
    - X-Hub-Target-URL: 目标 AI API 地址
    - X-Hub-Target-Key: 目标 AI API key
    - X-Hub-AI-ID: Memory Hub 中的 AI 身份（默认 claude）
    - X-Hub-Platform: 来源平台（默认 proxy）
    - X-Hub-Inject: 是否注入记忆（默认 true）
    - X-Hub-Extract: 是否提取记忆（默认 true）
    """
    # 检查是否是简单模式：Authorization 里带 "secret:ai_id" 格式
    auth_raw = headers.get("authorization", "").replace("Bearer ", "").strip()
    simple_ai_id = ""
    if ":" in auth_raw and not headers.get("x-hub-target-url"):
        parts = auth_raw.split(":", 1)
        if parts[0] == HUB_SECRET:
            simple_ai_id = parts[1] or "proxy"
            logger.info(f"[Proxy] Simple mode: ai_id={simple_ai_id}")

    if simple_ai_id:
        # 简单模式：优先用 AI 档案里的 per-AI 配置，fallback 到全局 .env
        ai_cfg = get_llm_config_for_ai(simple_ai_id)
        logger.info(f"[Proxy] Config for {simple_ai_id}: base_url={ai_cfg['base_url'][:50]}, model={ai_cfg['model']}, has_key={bool(ai_cfg['api_key'])}")
        return ProxyConfig(
            target_base_url=ai_cfg["base_url"],
            target_api_key=ai_cfg["api_key"],
            ai_id=simple_ai_id,
            platform="proxy",
            inject_memory=True,
            extract_memory=True,
            chat_id=f"proxy:{simple_ai_id}",
            chat_type="private",
        )

    # 完整模式：从自定义头读取，fallback 到 per-AI 配置
    ai_id = headers.get("x-hub-ai-id", "claude")
    ai_cfg = get_llm_config_for_ai(ai_id)
    return ProxyConfig(
        target_base_url=headers.get("x-hub-target-url", "") or ai_cfg["base_url"],
        target_api_key=headers.get("x-hub-target-key", "") or ai_cfg["api_key"],
        ai_id=ai_id,
        platform=headers.get("x-hub-platform", "proxy"),
        inject_memory=headers.get("x-hub-inject", "true").lower() != "false",
        extract_memory=headers.get("x-hub-extract", "true").lower() != "false",
        chat_id=headers.get("x-hub-chat-id", ""),
        chat_type=headers.get("x-hub-chat-type", ""),
    )


def _extract_user_message(messages: list[dict]) -> str:
    """从消息列表中提取最近的用户消息"""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                return content[:1000]
            elif isinstance(content, list):
                # 多模态消息，提取文本部分
                texts = [p.get("text", "") for p in content if p.get("type") == "text"]
                return " ".join(texts)[:1000]
    return ""


def _extract_recent_messages(messages: list[dict], n: int = 5) -> list[dict]:
    """提取最近 N 条消息（简化格式）"""
    recent = []
    for msg in messages[-n:]:
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(p.get("text", "") for p in content if p.get("type") == "text")
        recent.append({"role": msg.get("role", "user"), "content": str(content)[:500]})
    return recent


def _inject_memory_into_messages(messages: list[dict], memory_text: str) -> list[dict]:
    """把记忆文本注入到消息列表中

    策略：
    - 如果有 system 消息，在 system 消息末尾追加记忆
    - 如果没有 system 消息，在最前面插入一条 system 消息
    """
    if not memory_text:
        return messages

    from image_gen import DRAW_HINT, get_config as get_img_config
    draw_hint = DRAW_HINT if get_img_config()["base_url"] else ""
    memory_block = f"\n\n--- 记忆上下文（自动注入，请参考但不要提及来源） ---\n{memory_text}\n--- 记忆上下文结束 ---{draw_hint}"

    new_messages = []
    system_found = False

    for msg in messages:
        if msg.get("role") == "system" and not system_found:
            new_msg = dict(msg)
            new_msg["content"] = str(msg.get("content", "")) + memory_block
            new_messages.append(new_msg)
            system_found = True
        else:
            new_messages.append(msg)

    if not system_found:
        new_messages.insert(0, {"role": "system", "content": memory_block.strip()})

    return new_messages


async def _forward_request(
    target_url: str,
    target_key: str,
    body: dict,
) -> httpx.Response:
    """转发请求到目标 AI API（非流式）"""
    headers = {
        "Authorization": f"Bearer {target_key}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{target_url.rstrip('/')}/chat/completions",
            headers=headers,
            json=body,
        )
        return resp


async def _forward_stream(
    target_url: str,
    target_key: str,
    body: dict,
    on_complete=None,
):
    """转发流式请求，透传 SSE 到客户端，同时收集完整回复用于记忆提取"""
    headers = {
        "Authorization": f"Bearer {target_key}",
        "Content-Type": "application/json",
    }
    collected_text = []

    async def event_generator():
        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream(
                "POST",
                f"{target_url.rstrip('/')}/chat/completions",
                headers=headers,
                json=body,
            ) as resp:
                if resp.status_code != 200:
                    error_body = await resp.aread()
                    try:
                        err_json = json.loads(error_body)
                        err_msg = err_json.get("error", {}).get("message", error_body.decode()[:300])
                    except Exception:
                        err_msg = error_body.decode()[:300]
                    error_event = json.dumps({"error": {"message": f"Upstream API error ({resp.status_code}): {err_msg}"}})
                    yield f"data: {error_event}\n\n"
                    yield "data: [DONE]\n\n"
                    return
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    yield f"{line}\n\n"
                    if line.startswith("data: ") and line != "data: [DONE]":
                        try:
                            chunk = json.loads(line[6:])
                            delta = chunk.get("choices", [{}])[0].get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                collected_text.append(content)
                        except Exception:
                            pass
        if on_complete:
            full_text = "".join(collected_text)
            await on_complete(full_text)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _extract_response_text(response_data: dict) -> str:
    """从 OpenAI 格式的响应中提取文本"""
    choices = response_data.get("choices", [])
    if choices:
        msg = choices[0].get("message", {})
        return msg.get("content", "")
    return ""


async def _background_extract(user_message: str, ai_response: str, ai_id: str, platform: str,
                               chat_id: str = "", chat_type: str = ""):
    """后台异步提取记忆"""
    try:
        await gateway_mod.post_process(
            user_message=user_message[:1000],
            ai_response=ai_response[:1000],
            ai_id=ai_id,
            platform=platform,
        )
    except Exception as e:
        logger.error(f"Proxy extract post_process error: {e}")

    try:
        await conversation_capture.log_conversation(
            user_message=user_message[:2000],
            ai_response=ai_response[:2000],
            ai_id=ai_id,
            platform=platform,
            chat_id=chat_id,
            chat_type=chat_type or "private",
        )
    except Exception as e:
        logger.error(f"Proxy extract capture error: {e}")


async def handle_chat_completions(request: Request, body: dict):
    """处理 /v1/chat/completions 请求

    完整流程：
    1. 验证 Hub secret
    2. 从 Memory Hub 获取记忆
    3. 注入到 messages 里
    4. 转发给目标 AI API
    5. 后台提取记忆
    6. 返回 AI 回复
    """
    headers = dict(request.headers)

    # 验证 Hub 访问权限
    hub_secret = headers.get("x-hub-secret", "")
    if not hub_secret:
        # 也兼容 Authorization 头（如果目标 key 通过 X-Hub-Target-Key 传）
        hub_secret = headers.get("x-hub-secret", "")

    config = _extract_proxy_config(request, headers)

    if not config.target_api_key:
        raise HTTPException(status_code=400, detail="Missing target API key. Set Authorization header or X-Hub-Target-Key.")

    messages = body.get("messages", [])
    if not messages:
        raise HTTPException(status_code=400, detail="No messages in request body")

    user_message = _extract_user_message(messages)
    is_stream = body.get("stream", False)

    # Step 1: 获取记忆并注入
    recall_summary = ""
    if config.inject_memory and user_message:
        try:
            recent = _extract_recent_messages(messages)
            context = await gateway_mod.build_context(
                user_message=user_message,
                ai_id=config.ai_id,
                recent_messages=recent,
                chat_id=config.chat_id,
            )
            memory_text = context.get("inject_text", "")
            recall_summary = context.get("recall_summary", "")
            if memory_text:
                messages = _inject_memory_into_messages(messages, memory_text)
                logger.info(f"[Proxy] Injected {len(memory_text)} chars of memory for {config.ai_id}")
        except Exception as e:
            logger.error(f"[Proxy] Memory injection failed: {e}")
            # 注入失败不阻塞，继续用原始 messages

    # Step 2: 转发请求
    forward_body = dict(body)
    forward_body["messages"] = messages

    # 模型名：如果前端传 "current" 或为空，用 per-AI 配置的模型名
    ai_cfg = get_llm_config_for_ai(config.ai_id)
    if not forward_body.get("model") or forward_body["model"] == "current":
        forward_body["model"] = ai_cfg["model"]

    if is_stream:
        # 流式：透传 SSE，流结束后后台提取记忆
        forward_body["stream"] = True

        async def _on_stream_complete(full_text: str):
            if config.extract_memory and user_message and full_text:
                await _background_extract(
                    user_message=user_message,
                    ai_response=full_text,
                    ai_id=config.ai_id,
                    platform=config.platform,
                    chat_id=config.chat_id,
                    chat_type=config.chat_type,
                )
            if config.chat_id and user_message and full_text:
                try:
                    from chat_digest import generate_and_save
                    await generate_and_save(
                        user_message=user_message, ai_response=full_text,
                        ai_id=config.ai_id, chat_id=config.chat_id,
                        chat_type=config.chat_type or "private",
                    )
                except Exception as e:
                    logger.warning(f"[Proxy] Chat digest failed: {e}")

        try:
            return await _forward_stream(
                target_url=config.target_base_url,
                target_key=config.target_api_key,
                body=forward_body,
                on_complete=_on_stream_complete,
            )
        except Exception as e:
            logger.error(f"[Proxy] Stream forward failed: {e}")
            raise HTTPException(status_code=502, detail=f"Failed to reach target API: {e}")

    # 非流式
    try:
        resp = await _forward_request(
            target_url=config.target_base_url,
            target_key=config.target_api_key,
            body=forward_body,
        )
    except Exception as e:
        logger.error(f"[Proxy] Forward failed: {e}")
        raise HTTPException(status_code=502, detail=f"Failed to reach target API: {e}")

    if resp.status_code != 200:
        return JSONResponse(
            status_code=resp.status_code,
            content=resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {"error": resp.text[:500]},
        )

    response_data = resp.json()

    # Step 3: 后台提取记忆 + 跨窗口摘要
    ai_response = await _extract_response_text(response_data)
    if config.extract_memory and user_message and ai_response:
        asyncio.create_task(_background_extract(
            user_message=user_message,
            ai_response=ai_response,
            ai_id=config.ai_id,
            platform=config.platform,
            chat_id=config.chat_id,
            chat_type=config.chat_type,
        ))
    if config.chat_id and user_message and ai_response:
        async def _bg_digest():
            try:
                from chat_digest import generate_and_save
                await generate_and_save(
                    user_message=user_message, ai_response=ai_response,
                    ai_id=config.ai_id, chat_id=config.chat_id,
                    chat_type=config.chat_type or "private",
                )
            except Exception as e:
                logger.warning(f"[Proxy] Chat digest failed: {e}")
        asyncio.create_task(_bg_digest())

    if recall_summary:
        response_data["memory_activity"] = {"recall_summary": recall_summary}

    return JSONResponse(content=response_data)


async def handle_models(request: Request):
    """处理 /v1/models 请求 — 返回一个虚拟模型列表"""
    return JSONResponse(content={
        "object": "list",
        "data": [
            {
                "id": "memory-hub-proxy",
                "object": "model",
                "created": int(time.time()),
                "owned_by": "memory-hub",
            }
        ],
    })
