"""
Agent 能力系统（Capabilities）
让 AI 在聊天中拥有 agent 能力：用户在前端说一句话，AI 判断需要做什么，
在回复中写标签，后端自动解析并执行对应操作。

架构：
  1. 注册能力 → 生成 prompt hint 注入 system message
  2. AI 回复中包含 [tag:content] 标签
  3. proxy 收到完整回复后调用 process() → 解析标签 → 执行 → 从可见文本中清除标签
  4. 执行结果作为 metadata 返回给前端（可选展示）

已有的 [draw:xxx] 作为第一个能力纳入统一管理。
"""
import re
import logging
from typing import Callable, Awaitable

log = logging.getLogger("memory_hub.capabilities")

_TAG_PATTERN = re.compile(r'\[([a-zA-Z一-鿿_]+):([^\]]*)\]')


class Capability:
    __slots__ = ("tag", "label", "hint", "handler")

    def __init__(self, tag: str, label: str, hint: str,
                 handler: Callable[[str, str], Awaitable[dict]]):
        self.tag = tag
        self.label = label
        self.hint = hint
        self.handler = handler


_capabilities: dict[str, Capability] = {}


def register(tag: str, label: str, hint: str,
             handler: Callable[[str, str], Awaitable[dict]]):
    _capabilities[tag] = Capability(tag, label, hint, handler)


def capability_hints() -> str:
    if not _capabilities:
        return ""
    lines = ["【你的能力】你可以在回复中使用以下标签来执行操作，系统会自动处理："]
    for cap in _capabilities.values():
        lines.append(f"  {cap.hint}")
    lines.append("标签会在发送前被系统处理掉，用户看不到原始标签。不要滥用，只在合适的时候使用。")
    return "\n".join(lines)


async def process(text: str, ai_id: str = "") -> tuple[str, list[dict]]:
    """扫描 AI 回复中的标签，执行对应能力，返回 (清理后文本, 执行结果列表)。"""
    results = []
    matches = list(_TAG_PATTERN.finditer(text))
    if not matches:
        return text, results

    for m in reversed(matches):
        tag = m.group(1)
        content = m.group(2).strip()
        cap = _capabilities.get(tag)
        if not cap:
            continue
        try:
            result = await cap.handler(content, ai_id)
            result["tag"] = tag
            result["input"] = content
            results.append(result)
            replacement = result.get("replacement", "")
            text = text[:m.start()] + replacement + text[m.end():]
            log.info(f"[Cap] Executed [{tag}:{content[:40]}] → {result.get('status', 'ok')}")
        except Exception as e:
            log.error(f"[Cap] Failed [{tag}:{content[:40]}]: {e}")
            text = text[:m.start()] + text[m.end():]
            results.append({"tag": tag, "input": content, "status": "error", "error": str(e)})

    results.reverse()
    return text, results


# ── 能力：记住 ──

async def _handle_remember(content: str, ai_id: str) -> dict:
    from memory_ops import remember
    result = await remember(
        content=content,
        room="living_room",
        source_ai=ai_id,
        source_platform="capability",
        importance=0.6,
    )
    return {"status": "ok", "memory_id": result.get("id", ""), "action": result.get("action", "created")}


register(
    tag="记住",
    label="记住",
    hint="[记住:内容] — 把重要信息存入记忆（比如用户说的偏好、重要事件、新信息）",
    handler=_handle_remember,
)


# ── 能力：更新状态 ──

async def _handle_update_status(content: str, ai_id: str) -> dict:
    import current_status
    parts = content.split("=", 1)
    if len(parts) != 2:
        return {"status": "error", "error": "格式应为 section=内容，如 职业=在XX公司做运营"}
    section_label, new_text = parts[0].strip(), parts[1].strip()
    label_to_key = {v["label"]: k for k, v in current_status.SECTIONS.items()}
    key = label_to_key.get(section_label)
    if not key:
        short_map = {"职业": "career", "工作": "career", "健康": "health", "身体": "health", "生活": "life"}
        key = short_map.get(section_label)
    if not key:
        return {"status": "error", "error": f"未知状态分类: {section_label}，可选: 职业/工作、健康/身体、生活"}
    from datetime import datetime, timezone
    sections = current_status.get_status().get("sections", {})
    sections[key] = {
        "text": new_text,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "evidence_count": 1,
        "source": f"capability:{ai_id}",
    }
    await current_status.save_status(sections)
    return {"status": "ok", "section": key, "text": new_text}


register(
    tag="更新状态",
    label="更新用户状态",
    hint="[更新状态:分类=内容] — 更新用户的当前状态画像（分类：职业、健康、生活），如 [更新状态:职业=在XX公司做新媒体运营]",
    handler=_handle_update_status,
)


# ── 能力：这是谁 ──

async def _handle_who_is(content: str, ai_id: str) -> dict:
    import identity_registry
    parts = content.split("=", 1)
    if len(parts) != 2:
        return {"status": "error", "error": "格式应为 昵称=说明，如 狗蛋=Jasper的绰号"}
    nickname, description = parts[0].strip(), parts[1].strip()
    reg = identity_registry.get_registry()
    # check if it's an AI nickname
    ai_nicknames = reg.get("ai_nicknames", {})
    for real_ai_id, nicks in ai_nicknames.items():
        if nickname in nicks or nickname.lower() == real_ai_id:
            return {"status": "ok", "note": f"{nickname} 已经在注册表里了（= {real_ai_id}）"}
    # check if it matches a known person
    for p in reg.get("people", []):
        if nickname == p.get("canonical") or nickname in p.get("aliases", []):
            return {"status": "ok", "note": f"{nickname} 已经在注册表里了（= {p['canonical']}）"}
    # add as new person
    reg.setdefault("people", []).append({
        "canonical": nickname,
        "aliases": [],
        "relation": description,
        "note": f"由 {ai_id} 通过聊天添加",
        "source": "capability",
    })
    identity_registry._registry = reg
    await identity_registry.save_registry(f"capability: add person {nickname}")
    return {"status": "ok", "added": nickname, "description": description}


register(
    tag="这是谁",
    label="登记人物",
    hint="[这是谁:昵称=关系说明] — 登记新出现的人物（如 [这是谁:小王=用户的同事]），帮助所有AI正确理解人物关系",
    handler=_handle_who_is,
)


# ── 能力：画图（整合已有的 [draw:xxx]） ──

async def _handle_draw(content: str, ai_id: str) -> dict:
    from image_gen import process_draw_tags
    tag_text = f"[draw:{content}]"
    result_text = await process_draw_tags(tag_text, ai_name=ai_id)
    if "[img]" in result_text:
        return {"status": "ok", "replacement": result_text}
    return {"status": "ok", "replacement": result_text}


register(
    tag="draw",
    label="画图",
    hint="[draw:图片描述] — 画图（描述用英文效果更好），不要每次都画，只在合适的时候",
    handler=_handle_draw,
)


# ── 能力：忘记 ──

async def _handle_forget(content: str, ai_id: str) -> dict:
    from memory_ops import recall
    results = await recall(content, top_k=1)
    if not results:
        return {"status": "ok", "note": "没有找到匹配的记忆"}
    best = results[0]
    mem_id = best.get("id", "")
    score = best.get("score", 0)
    if score < 0.02:
        return {"status": "ok", "note": "没有找到足够相关的记忆"}
    import database
    database.update_memory_status(mem_id, "archived")
    return {"status": "ok", "archived": mem_id, "content_preview": best.get("content", "")[:60]}


register(
    tag="忘记",
    label="忘记",
    hint="[忘记:描述] — 归档一条不再需要的记忆（用关键词描述要忘记的内容）",
    handler=_handle_forget,
)


# ── 能力：体检报告 ──

async def _handle_checkup_report(content: str, ai_id: str) -> dict:
    import memory_doctor
    return {"status": "ok", "replacement": memory_doctor.report_text()}


register(
    tag="体检报告",
    label="记忆体检报告",
    hint="[体检报告:] — 用户问记忆系统最近怎么样/有没有问题/体检结果时，用这个标签把最新体检报告念出来",
    handler=_handle_checkup_report,
)


# ── 能力：查原话 ──

async def _handle_raw_search(content: str, ai_id: str) -> dict:
    import raw_vault
    hits = raw_vault.search(content, limit=3)
    if not hits:
        return {"status": "ok", "replacement": f"（原文保险箱里没找到关于「{content}」的原话）"}
    lines = []
    for h in hits:
        date = h["created_at"][:10]
        if h.get("user_text"):
            lines.append(f"{date} 用户说：「{h['user_text'][:100]}」")
        if h.get("ai_text"):
            lines.append(f"{date} AI 回：「{h['ai_text'][:100]}」")
    return {"status": "ok", "replacement": "\n".join(lines[:4])}


register(
    tag="查原话",
    label="查原话",
    hint="[查原话:关键词] — 记忆存疑或用户问「当时到底怎么说的」时，从原文保险箱调出当时的原始对话",
    handler=_handle_raw_search,
)
