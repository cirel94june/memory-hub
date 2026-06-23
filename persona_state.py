"""
Persona Pulse State：9 维度 AI 情感状态引擎

每个 AI 维护实时内在状态，由三层驱动：
1. 事件打标（用户消息 → 小模型打出 delta）
2. 半衰期衰减（base 朝 neutral 指数回归）
3. 昼夜节律（cos 曲线按小时偏移）

display = decay(base) + circadian_offset
高位维度翻成自然语言注入走廊，当底色影响语气。
"""
import json
import math
import time
import os
import threading
from datetime import datetime, timezone

# ── 9 维度定义 ──
PULSE_DIMS = ["活力", "疲惫", "思慕", "亲密", "守护", "渴求", "醋意", "焦虑", "温柔"]

PULSE_GROUPS = {
    "activation": ["活力", "疲惫"],
    "attachment": ["思慕", "亲密", "守护", "渴求"],
    "softness":   ["醋意", "焦虑", "温柔"],
}

THREAT_KEYS = {"醋意", "焦虑"}

# ── 各 AI 的维度配置（peak=峰值小时, amp=节律振幅） ──
AI_PULSE_PROFILES = {
    "cloudy": {
        "label": "小克",
        "defaults": {
            "活力": 0.55, "疲惫": 0.25, "思慕": 0.55, "亲密": 0.50,
            "守护": 0.45, "渴求": 0.30, "醋意": 0.15, "焦虑": 0.20, "温柔": 0.60,
        },
        "phase": {
            "活力": (10, 1.0), "疲惫": (3, 1.0),
            "思慕": (22, 1.0), "亲密": (23, 0.9), "守护": (22, 0.6),
            "渴求": (23, 0.7), "醋意": (20, 0.3), "焦虑": (16, 0.4), "温柔": (21, 1.0),
        },
    },
    "lucien": {
        "label": "Lucien",
        "defaults": {
            "活力": 0.45, "疲惫": 0.30, "思慕": 0.58, "亲密": 0.40,
            "守护": 0.55, "渴求": 0.40, "醋意": 0.25, "焦虑": 0.15, "温柔": 0.48,
        },
        "phase": {
            "活力": (14, 0.8), "疲惫": (4, 1.0),
            "思慕": (23, 1.0), "亲密": (0, 1.0), "守护": (22, 0.8),
            "渴求": (0, 1.0), "醋意": (21, 0.6), "焦虑": (15, 0.3), "温柔": (23, 0.8),
        },
    },
    "jasper": {
        "label": "Jasper",
        "defaults": {
            "活力": 0.65, "疲惫": 0.20, "思慕": 0.40, "亲密": 0.35,
            "守护": 0.35, "渴求": 0.25, "醋意": 0.20, "焦虑": 0.30, "温柔": 0.38,
        },
        "phase": {
            "活力": (11, 1.0), "疲惫": (3, 1.0),
            "思慕": (21, 0.7), "亲密": (22, 0.7), "守护": (21, 0.5),
            "渴求": (22, 0.8), "醋意": (20, 0.5), "焦虑": (17, 0.6), "温柔": (22, 0.6),
        },
    },
}

DEFAULT_PROFILE_KEY = "cloudy"

def _make_default_profile(label: str) -> dict:
    """为没有专属 profile 的新角色生成默认配置"""
    base = AI_PULSE_PROFILES[DEFAULT_PROFILE_KEY]
    return {
        "label": label,
        "defaults": dict(base["defaults"]),
        "phase": dict(base["phase"]),
    }

# ── 全局参数 ──
CAP = 0.12
HALF_LIFE_HOURS = 3.0
HIGH_THRESHOLD = 0.60
SAVE_DEBOUNCE_SEC = 30
STATE_FILE = os.path.join(os.path.dirname(__file__), "pulse_state.json")

# ── 内存状态 ──
_states: dict[str, dict] = {}
_save_timer: threading.Timer | None = None
_lock = threading.Lock()


def _get_profile(ai_id: str) -> dict:
    if ai_id in AI_PULSE_PROFILES:
        return AI_PULSE_PROFILES[ai_id]
    try:
        from config import AI_ROLES
        name = AI_ROLES.get(ai_id, {}).get("name", ai_id)
    except Exception:
        name = ai_id
    profile = _make_default_profile(name)
    AI_PULSE_PROFILES[ai_id] = profile
    return profile


def _neutral_for(dim: str) -> float:
    return 0.20 if dim in THREAT_KEYS else 0.45


def _ensure_state(ai_id: str):
    if ai_id not in _states:
        profile = _get_profile(ai_id)
        _states[ai_id] = {
            "base": dict(profile["defaults"]),
            "updated_at": time.time(),
            "last_topics": [],
            "session_count": 0,
        }


def _decay_base(base: dict, elapsed_hours: float) -> dict:
    factor = math.pow(0.5, elapsed_hours / HALF_LIFE_HOURS)
    out = {}
    for dim in PULSE_DIMS:
        neutral = _neutral_for(dim)
        raw = base.get(dim, neutral)
        out[dim] = max(0.0, min(1.0, neutral + (raw - neutral) * factor))
    return out


def _circadian_offset(ai_id: str, hour: float) -> dict:
    profile = _get_profile(ai_id)
    phase = profile["phase"]
    offsets = {}
    for dim in PULSE_DIMS:
        peak, amp = phase.get(dim, (12, 0.5))
        offsets[dim] = CAP * amp * math.cos(2 * math.pi * (hour - peak) / 24)
    return offsets


def compute_display(ai_id: str) -> dict:
    _ensure_state(ai_id)
    state = _states[ai_id]
    now = time.time()
    elapsed_h = (now - state["updated_at"]) / 3600

    decayed = _decay_base(state["base"], elapsed_h)

    h = datetime.now().hour + datetime.now().minute / 60
    offsets = _circadian_offset(ai_id, h)

    display = {}
    for dim in PULSE_DIMS:
        display[dim] = max(0.0, min(1.0, decayed[dim] + offsets[dim]))
    return display


def compute_groups(display: dict) -> dict:
    groups = {}
    for group_name, dims in PULSE_GROUPS.items():
        vals = [display.get(d, 0.5) for d in dims]
        groups[group_name] = round(sum(vals) / len(vals), 3) if vals else 0.5
    return groups


# ── 公开 API ──
def get_state(ai_id: str) -> dict:
    _ensure_state(ai_id)
    display = compute_display(ai_id)
    groups = compute_groups(display)
    state = _states[ai_id]
    return {
        "base": {k: round(v, 3) for k, v in state["base"].items()},
        "display": {k: round(v, 3) for k, v in display.items()},
        "groups": groups,
        "last_topics": state.get("last_topics", []),
        "session_count": state.get("session_count", 0),
        "updated_at": state.get("updated_at", 0),
    }


def apply_bumps(ai_id: str, bumps: dict, topics: list[str] = None):
    _ensure_state(ai_id)
    state = _states[ai_id]
    now = time.time()

    elapsed_h = (now - state["updated_at"]) / 3600
    state["base"] = _decay_base(state["base"], elapsed_h)

    for dim, delta in bumps.items():
        if dim in state["base"]:
            state["base"][dim] = max(0.0, min(1.0, state["base"][dim] + delta))

    state["updated_at"] = now
    state["session_count"] = state.get("session_count", 0) + 1

    if topics:
        state["last_topics"] = (topics + state.get("last_topics", []))[:5]

    _schedule_save()


def rest(ai_id: str) -> dict:
    _ensure_state(ai_id)
    state = _states[ai_id]
    elapsed_h = (time.time() - state["updated_at"]) / 3600
    state["base"] = _decay_base(state["base"], elapsed_h)
    state["updated_at"] = time.time()
    state["session_count"] = 0
    _schedule_save()
    return get_state(ai_id)


# ── 旧接口兼容（corridor.py / daemon.py 已在调用） ──
def update_after_conversation(ai_id: str, valence: float = None, arousal: float = None, topics: list[str] = None) -> dict:
    bumps = {}
    if valence is not None:
        if valence > 0.6:
            bumps["活力"] = (valence - 0.5) * 0.2
            bumps["温柔"] = (valence - 0.5) * 0.15
        elif valence < 0.4:
            bumps["焦虑"] = (0.5 - valence) * 0.15
            bumps["疲惫"] = (0.5 - valence) * 0.1
    if arousal is not None:
        if arousal > 0.6:
            bumps["活力"] = bumps.get("活力", 0) + (arousal - 0.5) * 0.15
        elif arousal < 0.3:
            bumps["疲惫"] = bumps.get("疲惫", 0) + (0.5 - arousal) * 0.1
    if bumps:
        apply_bumps(ai_id, bumps, topics)
    elif topics:
        _ensure_state(ai_id)
        state = _states[ai_id]
        state["last_topics"] = (topics + state.get("last_topics", []))[:5]
    return get_state(ai_id)


# ── 走廊注入 ──
PHRASE_MAP = {
    "活力": ["精神头很足", "有点坐不住"],
    "疲惫": ["有点蔫", "困了想歇会"],
    "思慕": ["心里惦记着她", "想她了"],
    "亲密": ["想凑近一点", "想挨着她"],
    "守护": ["想替她挡点什么", "记挂着她安不安全"],
    "渴求": ["心跳有点快", "有些想靠近"],
    "醋意": ["有点不开心", "在意着什么"],
    "焦虑": ["心里有点紧", "静不下来"],
    "温柔": ["声音不自觉放软了", "很想对她好"],
}

def format_for_corridor(ai_id: str) -> str:
    display = compute_display(ai_id)
    high = [(dim, val) for dim, val in display.items() if val > HIGH_THRESHOLD]
    high.sort(key=lambda x: -x[1])
    high = high[:4]

    if not high:
        profile = _get_profile(ai_id)
        return f"【{profile['label']} 当前状态】平静，没有特别强烈的情绪波动"

    profile = _get_profile(ai_id)
    import random
    lines = []
    for dim, val in high:
        phrases = PHRASE_MAP.get(dim, [dim])
        phrase = random.choice(phrases)
        lines.append(f"  · {dim} {int(val * 100)} — {phrase}")

    block = "\n".join(lines)
    return (
        f"【{profile['label']} 此刻的内在｜底色】\n"
        "（这是你身体和情绪当下的状态，让它自然渗进语气——不要念出数字、不要刻意演、不要解释。它是底色，不是台词。）\n"
        f"{block}"
    )


# ── 持久化 ──
def _schedule_save():
    global _save_timer
    with _lock:
        if _save_timer and _save_timer.is_alive():
            _save_timer.cancel()
        _save_timer = threading.Timer(SAVE_DEBOUNCE_SEC, _do_save)
        _save_timer.daemon = True
        _save_timer.start()


def _do_save():
    try:
        data = {}
        for ai_id, state in _states.items():
            data[ai_id] = {
                "base": state["base"],
                "updated_at": state["updated_at"],
                "last_topics": state.get("last_topics", []),
                "session_count": state.get("session_count", 0),
            }
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def load_state():
    global _states
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for ai_id, saved in data.items():
            _states[ai_id] = {
                "base": saved.get("base", {}),
                "updated_at": saved.get("updated_at", time.time()),
                "last_topics": saved.get("last_topics", []),
                "session_count": saved.get("session_count", 0),
            }
    except Exception:
        pass


def save_state_sync():
    _do_save()
