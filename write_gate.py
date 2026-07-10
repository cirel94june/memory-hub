"""
写入门卫（Write Gate，参考 Ombre Brain 二改 memory_write_gate.py）
自动捕获的记忆在写入前过几道检查，从源头减少垃圾碎片——
碎片少了，走廊重复、合并漂移的机会就少。

原则：只拦"明显不值得长期记住"的内容，宁可放过不可错杀。
- 只对自动提取路径生效（remember(quick=True)）
- 人工写入（MCP remember、能力标签 [记住:]、前端）不走门卫
- 被拦内容记录到 data/write_gate.jsonl，体检报告可查、可追溯
"""
import json
import os
import re
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("write_gate")

GATE_LOG_PATH = Path(__file__).parent / "data" / "write_gate.jsonl"

# 一次性/易逝内容的标记词：出现且内容很短时说明是"此刻"而不是"长期事实"
EPHEMERAL_TERMS = ("刚才", "刚刚", "现在正在", "等一下", "稍等", "马上", "一会儿", "待会")

# 纯情绪/口头禅：整条内容剥掉这些后没剩什么就不值得记
NOISE_PATTERN = re.compile(
    r"[哈嘿呵嘻噗]|hhh+|233+|www+|[😀-🙏🤀-🧿]|[!！?？。，,.、~～\s]"
)

_PREFIX_RE = re.compile(r"^\[(用户|互动|AI)\]\s*")


def _log_blocked(content: str, reason: str, source: str):
    try:
        os.makedirs(GATE_LOG_PATH.parent, exist_ok=True)
        with open(GATE_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "reason": reason,
                "source": source,
                "content": content[:200],
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass


def recent_blocked(limit: int = 20) -> list[dict]:
    """最近被拦的内容（体检报告用）。"""
    if not GATE_LOG_PATH.exists():
        return []
    try:
        with open(GATE_LOG_PATH, encoding="utf-8") as f:
            lines = f.readlines()
        return [json.loads(l) for l in lines[-limit:] if l.strip()]
    except Exception:
        return []


def check(content: str, room: str = "", source: str = "") -> tuple[bool, str]:
    """返回 (是否放行, 拦截原因)。放行时原因为空串。"""
    body = _PREFIX_RE.sub("", (content or "").strip())

    # 1. 太短：不构成一个可用的事实
    if len(body) < 8:
        _log_blocked(content, "too_short", source)
        return False, "too_short"

    # 2. 纯情绪/口头禅：剥掉笑声、emoji、标点后没剩下实质内容
    stripped = NOISE_PATTERN.sub("", body)
    if len(stripped) < 6:
        _log_blocked(content, "noise_only", source)
        return False, "noise_only"

    # 3. 易逝内容：带"刚才/等一下"这类此刻标记、又很短，说明记的是瞬间不是事实
    if len(body) < 30 and any(t in body for t in EPHEMERAL_TERMS):
        _log_blocked(content, "ephemeral", source)
        return False, "ephemeral"

    # 4. 与现有活跃记忆逐字重复（归一化后完全相同）
    try:
        import github_store as store
        norm = "".join(body.split()).lower()
        for m in store.get_all_memories().values():
            if m.get("status") != "active":
                continue
            if room and m.get("room") != room:
                continue
            exist = _PREFIX_RE.sub("", str(m.get("content", "")).strip())
            if "".join(exist.split()).lower() == norm:
                _log_blocked(content, f"exact_duplicate:{m.get('id')}", source)
                return False, "exact_duplicate"
    except Exception:
        pass  # 门卫故障不能阻塞写入

    return True, ""
