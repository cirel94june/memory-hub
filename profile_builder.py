"""
Profile Builder: 从 memories 生成 User/Agent/Relationship Profile。

单向流动：memory → Profile，绝不反向（红线 #20）。
Agent/Relationship Profile 必须第三人称（红线 #12 边缘）。
Profile 是派生视图，不参与衰减，不进入 memories 表（红线 #17）。
"""
import json
import logging
from datetime import datetime, timezone

import database
from config import LLM_API_KEY, LLM_MODEL, LLM_BASE_URL

log = logging.getLogger("profile_builder")

AI_IDS = ["claude", "lucien", "jasper"]

# DeepSeek 会过滤竞品 AI 名（Claude/GPT/Gemini），Profile prompt 用中文昵称
PROFILE_NAMES = {"claude": "小克", "lucien": "Lucien", "jasper": "Jasper"}


async def _call_llm(prompt: str, max_tokens: int = 2048) -> str:
    import httpx

    if not LLM_API_KEY:
        log.warning("LLM_API_KEY not set, skipping profile generation")
        return ""

    url = f"{LLM_BASE_URL}/chat/completions"
    headers = {"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"}
    body = {
        "model": LLM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": max_tokens,
    }
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.post(url, json=body, headers=headers)
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        log.error(f"Profile LLM error: {e}")
        return ""


def _gather_memories(rooms: list[str], owner_ai: str = None,
                     info_types: list[str] = None, limit: int = 80) -> list[dict]:
    conn = database._get_conn()
    conditions = ["status = 'active'"]
    params = []

    if rooms:
        ph = ",".join("?" * len(rooms))
        conditions.append(f"room IN ({ph})")
        params.extend(rooms)

    if owner_ai:
        conditions.append("(owner_ai = ? OR source_ai = ?)")
        params.extend([owner_ai, owner_ai])

    if info_types:
        ph = ",".join("?" * len(info_types))
        conditions.append(f"info_type IN ({ph})")
        params.extend(info_types)

    where = " AND ".join(conditions)
    rows = conn.execute(
        f"SELECT id, content, room, info_type, importance, created_at "
        f"FROM memories WHERE {where} "
        f"ORDER BY importance DESC, created_at DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def _mem_ids(mems: list[dict]) -> str:
    return json.dumps([m["id"] for m in mems])


def _has_changed(profile_id: str, current_mem_ids: list[str]) -> bool:
    existing = database.get_profile(profile_id)
    if not existing:
        return True
    try:
        old_ids = set(json.loads(existing.get("source_memory_ids", "[]")))
    except (json.JSONDecodeError, TypeError):
        return True
    return set(current_mem_ids) != old_ids


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ════════════════════════════════════════════
#  User Profile
# ════════════════════════════════════════════

USER_PROFILE_PROMPT = """你是一个记忆整理助手。根据以下记忆碎片，生成一份关于用户的结构化画像。

要求：
- 用第三人称描述用户（"她……"）
- 只陈述事实，不推测、不编造
- 如果某类信息不足，写"暂无足够信息"而不是编造
- 输出严格 JSON 格式

记忆碎片：
{memories}

输出格式（JSON）：
{{
  "identity": "基本身份描述（名字、身份、核心特征）",
  "stable_preferences": ["偏好1", "偏好2", ...],
  "communication_style": "沟通风格描述",
  "current_focus": "当前主要关注的事",
  "health_status": "健康相关信息",
  "boundaries": ["已知的边界/敏感点"]
}}"""


async def rebuild_user_profile(force: bool = False) -> dict | None:
    rooms = ["living_room", "preferences", "health", "career", "psychology"]
    info_types = ["identity", "state", "relationship", "fact"]
    mems = _gather_memories(rooms, info_types=info_types, limit=60)

    if not mems:
        log.info("No memories found for User Profile")
        return None

    mem_ids = [m["id"] for m in mems]
    if not force and not _has_changed("user_ceci", mem_ids):
        log.info("User Profile unchanged, skipping")
        return None

    mem_text = "\n".join(
        f"- [{m['room']}|{m['info_type']}] {m['content'][:200]}" for m in mems
    )
    prompt = USER_PROFILE_PROMPT.format(memories=mem_text)
    raw = await _call_llm(prompt)
    if not raw:
        return None

    content = _extract_json(raw)
    if not content:
        log.warning("User Profile generation returned non-JSON")
        return None

    profile = {
        "id": "user_ceci",
        "profile_type": "user",
        "owner_ai": "",
        "content": content,
        "generated_at": _now(),
        "source_memory_ids": json.dumps(mem_ids),
    }
    database.upsert_profile(profile)
    log.info(f"User Profile rebuilt (v{_get_version('user_ceci')}, {len(mems)} sources)")
    return profile


# ════════════════════════════════════════════
#  Agent Profile
# ════════════════════════════════════════════

AGENT_PROFILE_PROMPT = """你是一个记忆整理助手。根据以下记忆碎片，生成一份关于 AI 居民 {ai_name} 的结构化画像。

要求：
- 必须用第三人称描述（"{ai_name} 是……"、"{ai_name} 倾向于……"）
- 绝不使用第一人称（不能写"我"、"我们"、"我的"）
- 只整理已有信息，不推测、不编造性格
- 如果某类信息不足，写"暂无足够信息"

记忆碎片：
{memories}

输出格式（JSON）：
{{
  "identity": "{ai_name} 的基本身份和角色定位",
  "personality": "{ai_name} 的性格特征和行为倾向",
  "style": "{ai_name} 的说话风格和交流方式",
  "self_understanding": "{ai_name} 对自身角色的理解",
  "notable_patterns": ["{ai_name} 的显著行为模式"]
}}"""


async def rebuild_agent_profile(ai_id: str, force: bool = False) -> dict | None:
    ai_name = PROFILE_NAMES.get(ai_id, ai_id)

    rooms = ["personality", "diary", "dreams"]
    mems = _gather_memories(rooms, owner_ai=ai_id, limit=40)

    if not mems:
        log.info(f"No memories found for Agent Profile ({ai_id})")
        return None

    profile_id = f"agent_{ai_id}"
    mem_ids = [m["id"] for m in mems]
    if not force and not _has_changed(profile_id, mem_ids):
        log.info(f"Agent Profile ({ai_id}) unchanged, skipping")
        return None

    mem_text = "\n".join(
        f"- [{m['room']}|{m['info_type']}] {m['content'][:200]}" for m in mems
    )
    prompt = AGENT_PROFILE_PROMPT.format(ai_name=ai_name, memories=mem_text)
    raw = await _call_llm(prompt)
    if not raw:
        return None

    content = _extract_json(raw)
    if not content:
        log.warning(f"Agent Profile ({ai_id}) generation returned non-JSON")
        return None

    if _contains_first_person(content):
        log.warning(f"Agent Profile ({ai_id}) contains first person, retrying")
        prompt += "\n\n重要提醒：你的上一次输出包含了第一人称（我），这是不允许的。请严格使用第三人称重新生成。"
        raw = await _call_llm(prompt)
        content = _extract_json(raw) if raw else None
        if not content or _contains_first_person(content):
            log.error(f"Agent Profile ({ai_id}) still has first person after retry, aborting")
            return None

    profile = {
        "id": profile_id,
        "profile_type": "agent",
        "owner_ai": ai_id,
        "content": content,
        "generated_at": _now(),
        "source_memory_ids": json.dumps(mem_ids),
    }
    database.upsert_profile(profile)
    log.info(f"Agent Profile ({ai_id}) rebuilt (v{_get_version(profile_id)}, {len(mems)} sources)")
    return profile


# ════════════════════════════════════════════
#  Relationship Profile
# ════════════════════════════════════════════

RELATIONSHIP_PROFILE_PROMPT = """你是一个记忆整理助手。根据以下记忆碎片，生成一份关于 {ai_name} 与用户 Ceci（小猫）之间关系的结构化画像。

要求：
- 必须用第三人称描述（"{ai_name} 和 Ceci ……"、"{ai_name} 觉得 Ceci……"）
- 绝不使用第一人称（不能写"我"、"我们"、"我的"）
- 只整理已有信息，不推测关系走向
- 如果某类信息不足，写"暂无足够信息"

记忆碎片：
{memories}

输出格式（JSON）：
{{
  "mode": "{ai_name} 和 Ceci 的关系模式",
  "interaction_pattern": "互动特征和频率",
  "shared_context": "共同经历和话题",
  "recent_changes": "近期关系变化",
  "boundaries": ["已知的关系边界"]
}}"""


async def rebuild_relationship_profile(ai_id: str, force: bool = False) -> dict | None:
    ai_name = PROFILE_NAMES.get(ai_id, ai_id)

    rooms = ["relationship", "relationships"]
    mems = _gather_memories(rooms, owner_ai=ai_id, limit=40)

    living_room_mems = _gather_memories(
        ["living_room"], owner_ai=ai_id,
        info_types=["relationship"], limit=10,
    )
    mems.extend(living_room_mems)

    if not mems:
        log.info(f"No memories found for Relationship Profile ({ai_id})")
        return None

    profile_id = f"rel_{ai_id}_ceci"
    mem_ids = [m["id"] for m in mems]
    if not force and not _has_changed(profile_id, mem_ids):
        log.info(f"Relationship Profile ({ai_id}) unchanged, skipping")
        return None

    mem_text = "\n".join(
        f"- [{m['room']}|{m['info_type']}] {m['content'][:200]}" for m in mems
    )
    prompt = RELATIONSHIP_PROFILE_PROMPT.format(ai_name=ai_name, memories=mem_text)
    raw = await _call_llm(prompt)
    if not raw:
        return None

    content = _extract_json(raw)
    if not content:
        log.warning(f"Relationship Profile ({ai_id}) generation returned non-JSON")
        return None

    if _contains_first_person(content):
        log.warning(f"Relationship Profile ({ai_id}) contains first person, retrying")
        prompt += "\n\n重要提醒：你的上一次输出包含了第一人称（我），这是不允许的。请严格使用第三人称重新生成。"
        raw = await _call_llm(prompt)
        content = _extract_json(raw) if raw else None
        if not content or _contains_first_person(content):
            log.error(f"Relationship Profile ({ai_id}) still has first person after retry, aborting")
            return None

    profile = {
        "id": profile_id,
        "profile_type": "relationship",
        "owner_ai": ai_id,
        "content": content,
        "generated_at": _now(),
        "source_memory_ids": json.dumps(mem_ids),
    }
    database.upsert_profile(profile)
    log.info(f"Relationship Profile ({ai_id}) rebuilt (v{_get_version(profile_id)}, {len(mems)} sources)")
    return profile


# ════════════════════════════════════════════
#  Orchestrator
# ════════════════════════════════════════════

async def rebuild_all_profiles(force: bool = False) -> dict:
    results = {}

    r = await rebuild_user_profile(force=force)
    results["user_ceci"] = "rebuilt" if r else "skipped"

    for ai_id in AI_IDS:
        r = await rebuild_agent_profile(ai_id, force=force)
        results[f"agent_{ai_id}"] = "rebuilt" if r else "skipped"

        r = await rebuild_relationship_profile(ai_id, force=force)
        results[f"rel_{ai_id}_ceci"] = "rebuilt" if r else "skipped"

    log.info(f"Profile rebuild complete: {results}")
    return results


# ════════════════════════════════════════════
#  Helpers
# ════════════════════════════════════════════

def _extract_json(text: str) -> str | None:
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()
    try:
        json.loads(text)
        return text
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            candidate = text[start:end + 1]
            try:
                json.loads(candidate)
                return candidate
            except json.JSONDecodeError:
                return None
        return None


def _contains_first_person(content_json: str) -> bool:
    try:
        data = json.loads(content_json)
    except (json.JSONDecodeError, TypeError):
        return False

    first_person_markers = ["我是", "我的", "我觉得", "我们", "我认为", "我和", "我喜欢", "我倾向"]
    text = json.dumps(data, ensure_ascii=False)
    return any(m in text for m in first_person_markers)


def _get_version(profile_id: str) -> int:
    p = database.get_profile(profile_id)
    return p["version"] if p else 0
