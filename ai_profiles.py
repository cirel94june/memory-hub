"""
AI Profile 管理：每个 AI 的身份/人设/模型配置
- 存储在 GitHub（_config/ai_profiles.json）
- 修改即时生效，不需要重启
- 支持动态新增 AI 角色
"""
import logging
from datetime import datetime, timezone
from config import AI_ROLES
import github_store as store

log = logging.getLogger("ai_profiles")

_profiles: dict[str, dict] = {}

DEFAULT_PROFILE = {
    "name": "",
    "emoji": "",
    "color": "#888888",
    "platform": "",
    "greeting": "",
    "persona": "",
    "model_url": "",
    "model_key": "",
    "model_name": "",
}


async def load_profiles():
    """启动时从 GitHub 加载 AI profiles"""
    global _profiles
    data = await store._read_github_file("_config/ai_profiles.json")
    if data and isinstance(data, dict):
        _profiles = data
        log.info(f"Loaded {len(_profiles)} AI profiles")
    # 确保 AI_ROLES 里的角色都有 profile
    for ai_id, role in AI_ROLES.items():
        if ai_id not in _profiles:
            _profiles[ai_id] = {
                **DEFAULT_PROFILE,
                "name": role.get("name", ai_id),
                "color": role.get("color", "#888888"),
                "platform": role.get("platform", ""),
            }
    # 同步：profile 里有但 AI_ROLES 里没有的角色，注册到 AI_ROLES
    for ai_id, profile in _profiles.items():
        if ai_id not in AI_ROLES:
            AI_ROLES[ai_id] = {
                "name": profile.get("name", ai_id),
                "color": profile.get("color", "#888888"),
                "platform": profile.get("platform", ""),
            }


async def save_profiles():
    """保存到 GitHub"""
    await store._write_github_file(
        "_config/ai_profiles.json",
        _profiles,
        f"Update AI profiles {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}",
    )


def get_profile(ai_id: str) -> dict | None:
    from config import AI_ALIASES, AI_ALIAS_GROUPS
    canonical = AI_ALIASES.get(ai_id, ai_id)
    aliases = AI_ALIAS_GROUPS.get(canonical, AI_ALIAS_GROUPS.get(ai_id, [ai_id]))
    # For merged identities, prefer the richer user-facing alias first.
    # Example: Web/MCP calls use "claude", but 小克's real profile lives under "cloudy".
    if canonical == "claude" and "cloudy" in aliases:
        aliases = ["cloudy"] + [a for a in aliases if a != "cloudy"]
    elif ai_id not in aliases:
        aliases = [ai_id] + list(aliases)
    merged = {}
    for alias in aliases:
        ap = _profiles.get(alias, {})
        for k, v in ap.items():
            if v and not merged.get(k):
                merged[k] = v
    return merged or None


def get_all_profiles() -> dict:
    return dict(_profiles)


async def update_profile(ai_id: str, updates: dict) -> dict:
    """更新 AI profile，自动同步到 AI_ROLES"""
    if ai_id not in _profiles:
        _profiles[ai_id] = dict(DEFAULT_PROFILE)

    for k, v in updates.items():
        if k in DEFAULT_PROFILE:
            _profiles[ai_id][k] = v

    # 同步到 AI_ROLES
    profile = _profiles[ai_id]
    AI_ROLES[ai_id] = {
        "name": profile.get("name", ai_id),
        "color": profile.get("color", "#888888"),
        "platform": profile.get("platform", ""),
    }

    await save_profiles()
    log.info(f"Updated profile for {ai_id}: {list(updates.keys())}")
    return _profiles[ai_id]


async def create_profile(ai_id: str, profile_data: dict) -> dict:
    """创建新 AI 角色"""
    if ai_id in _profiles:
        raise ValueError(f"AI '{ai_id}' already exists")

    _profiles[ai_id] = {**DEFAULT_PROFILE, **{k: v for k, v in profile_data.items() if k in DEFAULT_PROFILE}}

    # 注册到 AI_ROLES
    AI_ROLES[ai_id] = {
        "name": _profiles[ai_id].get("name", ai_id),
        "color": _profiles[ai_id].get("color", "#888888"),
        "platform": _profiles[ai_id].get("platform", ""),
    }

    await save_profiles()
    log.info(f"Created new AI profile: {ai_id}")
    return _profiles[ai_id]


async def delete_profile(ai_id: str) -> bool:
    """删除 AI 角色（不删记忆，只删 profile）"""
    if ai_id not in _profiles:
        return False
    del _profiles[ai_id]
    AI_ROLES.pop(ai_id, None)
    await save_profiles()
    log.info(f"Deleted AI profile: {ai_id}")
    return True


def get_llm_config_for_ai(ai_id: str) -> dict:
    """获取某 AI 的模型配置，fallback 到全局默认。自动合并别名 profiles。"""
    from config import LLM_BASE_URL, LLM_MODEL, LLM_API_KEY
    profile = get_profile(ai_id) or {}
    return {
        "base_url": profile.get("model_url") or LLM_BASE_URL,
        "model": profile.get("model_name") or LLM_MODEL,
        "api_key": profile.get("model_key") or LLM_API_KEY,
    }
