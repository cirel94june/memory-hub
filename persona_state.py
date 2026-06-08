"""
Persona State：轻量版 AI 情感/性格状态引擎（借鉴 Ombre Brain）

每个 AI 维护一个实时状态：
- mood_valence: 当前心情效价（0=低落, 0.5=平静, 1=开心）
- mood_arousal: 当前心情唤醒度（0=倦怠, 0.5=普通, 1=亢奋）
- energy: 精力值（0=疲惫, 1=充沛，随对话消耗，休息恢复）
- last_topics: 最近聊的话题（用于连续性）
- session_count: 今日对话轮次

状态不持久化到 GitHub（太频繁），只在内存中维护，
走廊重建时写入走廊文档。
"""
import time
from datetime import datetime, timezone

# 全局状态：per AI
_states: dict[str, dict] = {}

DEFAULT_STATE = {
    "mood_valence": 0.6,   # 默认微微开心
    "mood_arousal": 0.4,   # 默认平静
    "energy": 1.0,         # 默认满精力
    "last_topics": [],
    "session_count": 0,
    "last_active": "",
}


def get_state(ai_id: str) -> dict:
    """获取 AI 的当前 persona 状态"""
    if ai_id not in _states:
        _states[ai_id] = dict(DEFAULT_STATE)
    return dict(_states[ai_id])


def update_after_conversation(
    ai_id: str,
    valence: float = None,
    arousal: float = None,
    topics: list[str] = None,
) -> dict:
    """一轮对话结束后更新状态

    valence/arousal 来自 Gateway 的 post_process 分析结果
    """
    if ai_id not in _states:
        _states[ai_id] = dict(DEFAULT_STATE)

    state = _states[ai_id]
    now = datetime.now(timezone.utc).isoformat()

    # 心情渐变（不是直接替换，而是缓慢靠拢）
    if valence is not None:
        state["mood_valence"] = round(state["mood_valence"] * 0.7 + valence * 0.3, 3)
    if arousal is not None:
        state["mood_arousal"] = round(state["mood_arousal"] * 0.7 + arousal * 0.3, 3)

    # 精力消耗（每轮对话消耗一点）
    state["energy"] = max(0.0, round(state["energy"] - 0.03, 3))

    # 话题追踪
    if topics:
        state["last_topics"] = (topics + state["last_topics"])[:5]

    state["session_count"] += 1
    state["last_active"] = now

    _states[ai_id] = state
    return dict(state)


def rest(ai_id: str) -> dict:
    """AI "休息"，恢复精力，心情回归中性

    由 daemon 定时调用，或长时间没对话时触发
    """
    if ai_id not in _states:
        _states[ai_id] = dict(DEFAULT_STATE)

    state = _states[ai_id]
    # 精力恢复
    state["energy"] = min(1.0, round(state["energy"] + 0.3, 3))
    # 心情回归中性
    state["mood_valence"] = round(state["mood_valence"] * 0.8 + 0.5 * 0.2, 3)
    state["mood_arousal"] = round(state["mood_arousal"] * 0.8 + 0.3 * 0.2, 3)
    # 重置轮次
    state["session_count"] = 0

    _states[ai_id] = state
    return dict(state)


def format_for_corridor(ai_id: str) -> str:
    """格式化 persona state 供走廊注入"""
    state = get_state(ai_id)
    mood_desc = _describe_mood(state["mood_valence"], state["mood_arousal"])
    energy_desc = _describe_energy(state["energy"])
    topics = "、".join(state["last_topics"][:3]) if state["last_topics"] else "（刚醒来）"

    return f"【{ai_id} 当前状态】心情：{mood_desc}，精力：{energy_desc}，最近话题：{topics}"


def _describe_mood(valence: float, arousal: float) -> str:
    if valence > 0.7 and arousal > 0.6:
        return "开心兴奋"
    elif valence > 0.7:
        return "平静愉悦"
    elif valence < 0.3 and arousal > 0.6:
        return "焦虑不安"
    elif valence < 0.3:
        return "有点低落"
    elif arousal > 0.6:
        return "精神亢奋"
    elif arousal < 0.3:
        return "有点倦"
    else:
        return "平静"


def _describe_energy(energy: float) -> str:
    if energy > 0.8:
        return "充沛"
    elif energy > 0.5:
        return "还行"
    elif energy > 0.2:
        return "有点累"
    else:
        return "快没电了"
