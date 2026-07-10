"""
当前状态画像（Current Status Portrait）
- 解决"旧职业/旧近况被当成现在持续推给 AI"的问题
- 思路借鉴 Ombre Brain portrait_engine：当前状态不是记忆碎片的堆积，
  而是一份定期**整段重写**的文档——重写时旧信息自然被新信息替换
- daemon 每 12h 重写；AI 醒来（走廊）读的是这份画像，不是零散旧碎片
- 记忆库里的旧记忆保持不变（历史留档），只是不再冒充"现状"

存储：GitHub _config/current_status.json
"""
import logging
from datetime import datetime, timezone

import github_store as store

log = logging.getLogger("current_status")

STATUS_PATH = "_config/current_status.json"

# 每个 section 从哪些房间取材料
SECTIONS = {
    "career": {"label": "职业/工作", "rooms": ["career", "work_tasks"]},
    "health": {"label": "身体/健康", "rooms": ["health"]},
    "life": {"label": "生活近况", "rooms": ["living_room", "psychology", "learning"]},
}

_status: dict = {}


async def load_status():
    global _status
    data = await store._read_github_file(STATUS_PATH)
    _status = data if isinstance(data, dict) else {}


def get_status() -> dict:
    return _status or {}


async def save_status(sections: dict):
    """sections: {key: {"text": str, "updated_at": iso, "evidence_count": int}}"""
    global _status
    _status = {
        "sections": sections,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    await store._write_github_file(STATUS_PATH, _status, "refresh current status portrait")


def corridor_block(user_display: str = "小猫") -> str:
    """走廊注入用的文本块。没有画像时返回空串。"""
    sections = get_status().get("sections", {})
    if not sections:
        return ""
    lines = []
    for key, meta in SECTIONS.items():
        sec = sections.get(key) or {}
        text = (sec.get("text") or "").strip()
        if not text:
            continue
        date = (sec.get("updated_at") or "")[:10]
        suffix = f"（更新于 {date}）" if date else ""
        lines.append(f"· {meta['label']}{suffix}：{text}")
    if not lines:
        return ""
    return (
        f"【{user_display}的当前状态】\n"
        "这是后台定期重写的最新画像，以这里为准；如果记忆碎片和这里矛盾，说明碎片是旧信息。\n"
        + "\n".join(lines)
    )


def section_reference(room: str) -> str:
    """给过时检测用：返回某房间对应 section 的当前画像文本。"""
    sections = get_status().get("sections", {})
    for key, meta in SECTIONS.items():
        if room in meta["rooms"]:
            text = ((sections.get(key) or {}).get("text") or "").strip()
            if text:
                return text
    return ""
