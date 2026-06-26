"""
画图功能：通过 GPT-5.5（或兼容 API）生成图片
- 配置存储在 GitHub（_config/image_api.json）
- 所有 AI 共用同一个画图 API
- 图片保存到本地 uploads/ 目录
"""
import os
import base64
import uuid
import logging
import httpx
import github_store as store

log = logging.getLogger("memory_hub.image_gen")

_config: dict = {}
UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)


async def load_config():
    global _config
    data = await store._read_github_file("_config/image_api.json")
    if data and isinstance(data, dict):
        _config = data
        log.info(f"Image API config loaded: model={_config.get('model', 'N/A')}")


async def save_config():
    from datetime import datetime, timezone
    await store._write_github_file(
        "_config/image_api.json",
        _config,
        f"Update image API config {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}",
    )


def get_config() -> dict:
    return {
        "base_url": _config.get("base_url", ""),
        "api_key": _config.get("api_key", ""),
        "model": _config.get("model", "gpt-5.5"),
    }


async def update_config(updates: dict):
    for k in ("base_url", "api_key", "model"):
        if k in updates:
            _config[k] = updates[k]
    await save_config()
    log.info(f"Image API config updated: model={_config.get('model')}")


async def generate_image(prompt: str, ai_name: str = "") -> dict:
    """
    调用 GPT-5.5 画图 API 生成图片。

    GPT-5.5 通过 chat completions 接口生成图片：
    - 发送画图 prompt
    - 响应中包含 base64 图片数据

    返回: {"url": "/uploads/xxx.png", "filename": "xxx.png"} 或 {"error": "..."}
    """
    cfg = get_config()
    if not cfg["base_url"] or not cfg["api_key"]:
        return {"error": "画图 API 未配置，请在 AI 档案页的「画图 API 配置」中填写"}

    system = "You are an image generation assistant. When asked to draw or create an image, generate it directly."
    if ai_name:
        system = f"You are {ai_name}, an AI with image generation ability. Generate images as requested."

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": prompt},
    ]

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{cfg['base_url'].rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {cfg['api_key']}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": cfg["model"],
                    "messages": messages,
                    "max_tokens": 4096,
                },
            )
            resp.raise_for_status()
            data = resp.json()

        content = data["choices"][0]["message"]["content"]

        # GPT-5.5 返回图片的方式：content 是数组，包含 image 类型元素
        # 或者 content 是字符串中嵌入了 base64
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    # 标准格式: {"type": "image", "image_url": {"url": "data:image/png;base64,..."}}
                    if part.get("type") == "image_url":
                        url = part.get("image_url", {}).get("url", "")
                        if url.startswith("data:image"):
                            return _save_data_url(url)
                    # 另一种格式: {"type": "image", "data": "base64..."}
                    if part.get("type") == "image":
                        b64 = part.get("data") or part.get("image", {}).get("data", "")
                        if b64:
                            return _save_base64(b64)
                        url = part.get("image_url", {}).get("url", "") or part.get("url", "")
                        if url.startswith("data:image"):
                            return _save_data_url(url)
                        if url.startswith("http"):
                            return await _download_and_save(url, client=None)
        elif isinstance(content, str):
            # 检查是否有 base64 图片嵌入（有些 API 返回 markdown 格式）
            import re
            # 匹配 ![...](data:image/png;base64,...) 格式
            m = re.search(r'data:image/[^;]+;base64,([A-Za-z0-9+/=\s]+)', content)
            if m:
                return _save_base64(m.group(1).replace('\n', '').replace(' ', ''))

            # 匹配 URL 图片
            m = re.search(r'https?://\S+\.(?:png|jpg|jpeg|webp|gif)', content)
            if m:
                return await _download_and_save(m.group(0))

            # 纯文本回复（模型没画图，只描述了）
            return {"error": "模型没有生成图片，可能需要更明确的画图指令", "text_reply": content}

        return {"error": "无法从模型响应中提取图片"}

    except httpx.HTTPStatusError as e:
        log.error(f"Image gen API error: {e.response.status_code} {e.response.text[:200]}")
        return {"error": f"画图 API 返回错误 ({e.response.status_code})"}
    except Exception as e:
        log.error(f"Image gen error: {e}")
        return {"error": f"画图失败: {str(e)}"}


def _save_base64(b64_data: str, ext: str = "png") -> dict:
    """保存 base64 图片到本地"""
    try:
        img_bytes = base64.b64decode(b64_data)
        filename = f"{uuid.uuid4().hex[:12]}.{ext}"
        filepath = os.path.join(UPLOAD_DIR, filename)
        with open(filepath, "wb") as f:
            f.write(img_bytes)
        log.info(f"Saved image: {filename} ({len(img_bytes)} bytes)")
        return {"url": f"/uploads/{filename}", "filename": filename}
    except Exception as e:
        log.error(f"Failed to save image: {e}")
        return {"error": f"图片保存失败: {e}"}


def _save_data_url(data_url: str) -> dict:
    """保存 data:image/... URL 到本地"""
    import re
    m = re.match(r'data:image/(\w+);base64,(.+)', data_url, re.DOTALL)
    if not m:
        return {"error": "无法解析图片数据"}
    ext = m.group(1)
    if ext == "jpeg":
        ext = "jpg"
    return _save_base64(m.group(2).replace('\n', '').replace(' ', ''), ext)


async def _download_and_save(url: str, client=None) -> dict:
    """下载远程图片并保存"""
    try:
        should_close = client is None
        if client is None:
            client = httpx.AsyncClient(timeout=30)
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            ct = resp.headers.get("content-type", "image/png")
            ext = "png"
            if "jpeg" in ct or "jpg" in ct:
                ext = "jpg"
            elif "webp" in ct:
                ext = "webp"
            elif "gif" in ct:
                ext = "gif"
            filename = f"{uuid.uuid4().hex[:12]}.{ext}"
            filepath = os.path.join(UPLOAD_DIR, filename)
            with open(filepath, "wb") as f:
                f.write(resp.content)
            log.info(f"Downloaded and saved: {filename} ({len(resp.content)} bytes)")
            return {"url": f"/uploads/{filename}", "filename": filename}
        finally:
            if should_close:
                await client.aclose()
    except Exception as e:
        log.error(f"Failed to download image: {e}")
        return {"error": f"图片下载失败: {e}"}


DRAW_HINT = (
    "\n\n【画图能力】你可以画图！当你想画图时，在回复中写 [draw:图片描述] 标签，系统会自动调用画图API生成图片。"
    "描述用英文效果更好。例如：[draw:a cute penguin sitting on ice under northern lights]。"
    "不要每次都画图，只在合适的时候画（比如聊到有趣的画面、有人让你画、发朋友圈想配图时）。"
)


async def process_draw_tags(text: str, ai_name: str = "") -> str:
    """检测文本中的 [draw:xxx] 标签，调用画图 API，替换为 [img]url[/img]"""
    import re
    pattern = r'\[draw:(.*?)\]'
    matches = list(re.finditer(pattern, text))
    if not matches:
        return text

    cfg = get_config()
    if not cfg["base_url"] or not cfg["api_key"]:
        for m in matches:
            text = text.replace(m.group(0), "（画图API未配置）")
        return text

    for m in matches:
        prompt = m.group(1).strip()
        if not prompt:
            continue
        log.info(f"Processing draw tag: {prompt[:50]}")
        result = await generate_image(prompt, ai_name=ai_name)
        if result.get("url"):
            text = text.replace(m.group(0), f"[img]{result['url']}[/img]")
        elif result.get("error"):
            text = text.replace(m.group(0), f"（画图失败: {result['error'][:50]}）")
        else:
            text = text.replace(m.group(0), "（画图失败）")

    return text
