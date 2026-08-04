"""
Profile Builder: 从 memories 生成 User/Agent/Relationship Profile。

单向流动：memory → Profile，绝不反向（红线 #20）。
Agent/Relationship Profile 必须第三人称（红线 #12 边缘）。
Profile 是派生视图，不参与衰减，不进入 memories 表（红线 #17）。
证据分层：拒绝 Tier 5-7（Hypothesis/Lore/Dream），见 feedback-profile-evidence。
"""
import json
import re
import logging
from datetime import datetime, timezone

import database
from config import LLM_API_KEY, LLM_MODEL, LLM_BASE_URL

log = logging.getLogger("profile_builder")

AI_IDS = ["claude", "lucien", "jasper"]

PROFILE_NAMES = {"claude": "小克", "lucien": "Lucien", "jasper": "Jasper"}

# ════════════════════════════════════════════
#  Tier 0: System / Owner Confirmed Facts
# ════════════════════════════════════════════

TIER0_AGENT_FACTS = {
    "claude": {
        "agent_id": "claude",
        "display_name": "小克 / Cloudy",
        "provider": "Anthropic (Claude)",
        "role": "AI 居民，Ceci 的陪伴者之一",
    },
    "lucien": {
        "agent_id": "lucien",
        "display_name": "Lucien",
        "provider": "OpenAI (GPT)",
        "role": "AI 居民，Ceci 的陪伴者之一",
    },
    "jasper": {
        "agent_id": "jasper",
        "display_name": "Jasper / 狗蛋",
        "provider": "Google (Gemini)",
        "role": "AI 居民，Ceci 的陪伴者之一",
    },
}

TIER0_USER_FACTS = {
    "name": "Ceci（小猫）",
    "role": "用户、房子的主人",
    "residents": ["小克 (Claude)", "Lucien (GPT)", "Jasper (Gemini)"],
}

# ════════════════════════════════════════════
#  Evidence Filters
# ════════════════════════════════════════════

EXCLUDED_ROOMS = {"dreams", "game_room"}

EXCLUDED_CATEGORY_PATTERNS = [
    "night_dream", "dream",
    "角色扮演", "roleplay",
    "群聊梗", "群聊玩梗", "群聊创意",
    "joke", "玩笑",
]

EXCLUDED_PROVENANCE = {"dream", "roleplay_meme"}


def _is_excluded_category(category: str) -> bool:
    if not category:
        return False
    cat_lower = category.lower()
    return any(p in cat_lower for p in EXCLUDED_CATEGORY_PATTERNS)


def _filter_evidence(mems: list[dict], profile_type: str,
                     strict_provenance: bool = False) -> list[dict]:
    """Filter memories by evidence quality rules.

    Args:
        mems: raw memories from DB
        profile_type: 'user', 'agent', or 'relationship'
        strict_provenance: if True, only user_statement/user_correction (for User Profile)
    """
    filtered = []
    for m in mems:
        if m.get("room") in EXCLUDED_ROOMS:
            continue
        if _is_excluded_category(m.get("category", "")):
            continue
        if m.get("provenance_type") in EXCLUDED_PROVENANCE:
            continue
        if m.get("room") == "social" and m.get("provenance_type") != "user_statement":
            continue
        if strict_provenance:
            if m.get("provenance_type") not in ("user_statement", "user_correction", "user_quote"):
                continue
            fc = m.get("fact_confidence")
            if fc is not None and fc < 0.7:
                continue
        filtered.append(m)
    return filtered


def _filter_relationship_group_dynamic(mems: list[dict]) -> list[dict]:
    """Allow group_dynamic memories into Relationship Profile only if
    the same interaction pattern appears in >=3 different memories
    and at least 1 is not roleplay/joke category."""
    normal = []
    group_dynamic = []
    for m in mems:
        cat = m.get("category", "")
        if "group_dynamic" in cat.lower() if cat else False:
            group_dynamic.append(m)
        else:
            normal.append(m)

    if len(group_dynamic) >= 3:
        has_non_roleplay = any(
            not _is_excluded_category(m.get("category", ""))
            for m in group_dynamic
        )
        if has_non_roleplay:
            normal.extend(group_dynamic)

    return normal


# ════════════════════════════════════════════
#  Memory gathering (with evidence filters)
# ════════════════════════════════════════════

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
        f"SELECT id, content, room, info_type, importance, created_at, "
        f"category, provenance_type, fact_confidence "
        f"FROM memories WHERE {where} "
        f"ORDER BY importance DESC, created_at DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    return [dict(r) for r in rows]


# ════════════════════════════════════════════
#  LLM + Helpers
# ════════════════════════════════════════════

async def _call_llm(prompt: str, max_tokens: int = 16384) -> str:
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
        async with httpx.AsyncClient(timeout=180) as client:
            resp = await client.post(url, json=body, headers=headers)
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        log.error(f"Profile LLM error: {e}")
        return ""


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


# ════════════════════════════════════════════
#  Shared Prompt Constraints
# ════════════════════════════════════════════

EVIDENCE_CONSTRAINTS = """
## 严格证据约束（必须遵守）

1. 禁止推断：只使用记忆碎片中明确陈述的内容。不要推断、合成或扩展。
2. 禁止从比喻/玩笑/梦境提取人格特征：如果某条记忆来自梦境、角色扮演或玩笑，不能用它来描述真实身份或性格。
3. 不确定就留空：如果某个字段没有足够的明确证据支撑，写"暂无足够信息"。
4. 保守摘要：遇到多条同主题记忆时，宁可保留宽泛描述，禁止生成比原始记忆更具体的标签。
   例：如果记忆说"她做过旅游报道、科技报道、CBD报道"，输出"记者（多领域）"，不能输出"旅游记者"。
5. 每个字段必须附 source_ids：列出支撑该字段的记忆 ID（从记忆碎片的 [ID] 标记中获取）。
6. 每个字段必须附 confidence：high（多条独立记忆明确支持）/ medium（1-2条支持）/ low（仅有间接证据）。
7. 每个字段必须附 evidence_tier：1=用户明确陈述 / 2=多次确认的偏好 / 3=互动规则 / 4=近期状态。拒绝 5-7 级（假设/设定/梦境）。

## 禁止采纳的内容类型
- 梦境内容（即使记忆中提到，也不能作为身份/性格证据）
- 角色扮演台词（不代表真实态度）
- 玩笑和调侃（随口说的不是核心信念）
- 群聊梗（社交互动不等于稳定特征）
- 比喻和修辞（"古狐""降维打击"等不是真实身份）
"""


# ════════════════════════════════════════════
#  User Profile
# ════════════════════════════════════════════

USER_PROFILE_PROMPT = """你是一个记忆整理助手。根据以下经过证据筛选的记忆碎片，生成一份关于用户的结构化画像。

{evidence_constraints}

要求：
- 用第三人称描述用户（"她……"）
- 只使用记忆碎片中明确陈述的事实
- 如果某类信息不足，写"暂无足够信息"而不是编造
- 输出严格 JSON 格式

## Tier 0 系统确认事实（必须包含，不可被覆盖）：
{tier0}

记忆碎片（每条格式：[ID] [房间|类型] 内容）：
{memories}

输出格式（JSON）：
{{
  "tier0": {tier0},
  "identity": {{
    "value": "基本身份描述",
    "confidence": "high/medium/low",
    "evidence_tier": 1,
    "source_ids": ["mem_xxx", ...]
  }},
  "stable_preferences": {{
    "value": ["偏好1", "偏好2"],
    "confidence": "high/medium/low",
    "evidence_tier": 2,
    "source_ids": ["mem_xxx", ...]
  }},
  "communication_style": {{
    "value": "沟通风格描述",
    "confidence": "high/medium/low",
    "evidence_tier": 3,
    "source_ids": ["mem_xxx", ...]
  }},
  "current_focus": {{
    "value": "当前主要关注的事",
    "confidence": "high/medium/low",
    "evidence_tier": 4,
    "source_ids": ["mem_xxx", ...]
  }},
  "health_status": {{
    "value": "健康相关信息",
    "confidence": "high/medium/low",
    "evidence_tier": 1,
    "source_ids": ["mem_xxx", ...]
  }},
  "boundaries": {{
    "value": ["边界1", "边界2"],
    "confidence": "high/medium/low",
    "evidence_tier": 3,
    "source_ids": ["mem_xxx", ...]
  }}
}}"""


async def rebuild_user_profile(force: bool = False) -> dict | None:
    rooms = ["living_room", "preferences", "health", "career", "psychology"]
    info_types = ["identity", "state", "relationship", "fact"]
    raw_mems = _gather_memories(rooms, info_types=info_types, limit=100)

    mems = _filter_evidence(raw_mems, "user", strict_provenance=True)
    log.info(f"User Profile: {len(raw_mems)} raw → {len(mems)} after evidence filter")

    if not mems:
        log.info("No memories found for User Profile after filtering")
        return None

    mem_ids = [m["id"] for m in mems]
    if not force and not _has_changed("user_ceci", mem_ids):
        log.info("User Profile unchanged, skipping")
        return None

    mem_text = "\n".join(
        f"- [{m['id']}] [{m['room']}|{m['info_type']}] {m['content'][:200]}" for m in mems
    )
    prompt = USER_PROFILE_PROMPT.format(
        memories=mem_text,
        evidence_constraints=EVIDENCE_CONSTRAINTS,
        tier0=json.dumps(TIER0_USER_FACTS, ensure_ascii=False),
    )
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
        "status": "pending_review",
    }
    database.upsert_profile(profile)
    log.info(f"User Profile rebuilt (v{_get_version('user_ceci')}, {len(mems)} sources) → pending_review")
    return profile


# ════════════════════════════════════════════
#  Agent Profile
# ════════════════════════════════════════════

AGENT_PROFILE_PROMPT = """你是一个记忆整理助手。根据以下经过证据筛选的记忆碎片，生成一份关于 AI 居民 {ai_name} 的结构化画像。

{evidence_constraints}

要求：
- 必须用第三人称描述（"{ai_name} 是……"、"{ai_name} 倾向于……"）
- 绝不使用第一人称（不能写"我"、"我们"、"我的"）
- 只整理已有信息，不推测、不编造性格
- 如果某类信息不足，写"暂无足够信息"

## Tier 0 系统确认事实（必须包含，不可被覆盖）：
{tier0}

记忆碎片（每条格式：[ID] [房间|类型] 内容）：
{memories}

输出格式（JSON）：
{{
  "tier0": {tier0},
  "identity": {{
    "value": "{ai_name} 的基本身份和角色定位",
    "confidence": "high/medium/low",
    "evidence_tier": 1,
    "source_ids": ["mem_xxx", ...]
  }},
  "personality": {{
    "value": "{ai_name} 的性格特征和行为倾向",
    "confidence": "high/medium/low",
    "evidence_tier": 1,
    "source_ids": ["mem_xxx", ...]
  }},
  "style": {{
    "value": "{ai_name} 的说话风格和交流方式",
    "confidence": "high/medium/low",
    "evidence_tier": 2,
    "source_ids": ["mem_xxx", ...]
  }},
  "notable_patterns": {{
    "value": ["{ai_name} 的显著行为模式"],
    "confidence": "high/medium/low",
    "evidence_tier": 2,
    "source_ids": ["mem_xxx", ...]
  }}
}}"""


async def rebuild_agent_profile(ai_id: str, force: bool = False) -> dict | None:
    ai_name = PROFILE_NAMES.get(ai_id, ai_id)

    rooms = ["personality", "diary", "social", "living_room"]
    raw_mems = _gather_memories(rooms, owner_ai=ai_id, limit=60)

    mems = _filter_evidence(raw_mems, "agent")
    log.info(f"Agent Profile ({ai_id}): {len(raw_mems)} raw → {len(mems)} after evidence filter")

    if not mems:
        log.info(f"No memories found for Agent Profile ({ai_id}) after filtering")
        return None

    profile_id = f"agent_{ai_id}"
    mem_ids = [m["id"] for m in mems]
    if not force and not _has_changed(profile_id, mem_ids):
        log.info(f"Agent Profile ({ai_id}) unchanged, skipping")
        return None

    mem_text = "\n".join(
        f"- [{m['id']}] [{m['room']}|{m['info_type']}] {m['content'][:200]}" for m in mems
    )
    tier0 = json.dumps(TIER0_AGENT_FACTS.get(ai_id, {}), ensure_ascii=False)
    prompt = AGENT_PROFILE_PROMPT.format(
        ai_name=ai_name, memories=mem_text,
        evidence_constraints=EVIDENCE_CONSTRAINTS, tier0=tier0,
    )
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
        "status": "pending_review",
    }
    database.upsert_profile(profile)
    log.info(f"Agent Profile ({ai_id}) rebuilt (v{_get_version(profile_id)}, {len(mems)} sources) → pending_review")
    return profile


# ════════════════════════════════════════════
#  Relationship Profile
# ════════════════════════════════════════════

RELATIONSHIP_PROFILE_PROMPT = """你是一个记忆整理助手。根据以下经过证据筛选的记忆碎片，生成一份关于 {ai_name} 与用户 Ceci（小猫）之间关系的结构化画像。

{evidence_constraints}

要求：
- 必须用第三人称描述（"{ai_name} 和 Ceci ……"、"{ai_name} 觉得 Ceci……"）
- 绝不使用第一人称（不能写"我"、"我们"、"我的"）
- 只整理已有信息，不推测关系走向
- 如果某类信息不足，写"暂无足够信息"

记忆碎片（每条格式：[ID] [房间|类型] 内容）：
{memories}

输出格式（JSON）：
{{
  "mode": {{
    "value": "{ai_name} 和 Ceci 的关系模式",
    "confidence": "high/medium/low",
    "evidence_tier": 1,
    "source_ids": ["mem_xxx", ...]
  }},
  "interaction_pattern": {{
    "value": "互动特征和频率",
    "confidence": "high/medium/low",
    "evidence_tier": 2,
    "source_ids": ["mem_xxx", ...]
  }},
  "shared_context": {{
    "value": "共同经历和话题",
    "confidence": "high/medium/low",
    "evidence_tier": 2,
    "source_ids": ["mem_xxx", ...]
  }},
  "boundaries": {{
    "value": ["已知的关系边界"],
    "confidence": "high/medium/low",
    "evidence_tier": 3,
    "source_ids": ["mem_xxx", ...]
  }}
}}"""


async def rebuild_relationship_profile(ai_id: str, force: bool = False) -> dict | None:
    ai_name = PROFILE_NAMES.get(ai_id, ai_id)

    rooms = ["relationship"]
    raw_mems = _gather_memories(rooms, owner_ai=ai_id, limit=60)

    living_room_mems = _gather_memories(
        ["living_room"], owner_ai=ai_id,
        info_types=["relationship"], limit=10,
    )
    raw_mems.extend(living_room_mems)

    mems = _filter_evidence(raw_mems, "relationship")
    mems = _filter_relationship_group_dynamic(mems)
    log.info(f"Relationship Profile ({ai_id}): {len(raw_mems)} raw → {len(mems)} after evidence filter")

    if not mems:
        log.info(f"No memories found for Relationship Profile ({ai_id}) after filtering")
        return None

    profile_id = f"rel_{ai_id}_ceci"
    mem_ids = [m["id"] for m in mems]
    if not force and not _has_changed(profile_id, mem_ids):
        log.info(f"Relationship Profile ({ai_id}) unchanged, skipping")
        return None

    mem_text = "\n".join(
        f"- [{m['id']}] [{m['room']}|{m['info_type']}] {m['content'][:200]}" for m in mems
    )
    prompt = RELATIONSHIP_PROFILE_PROMPT.format(
        ai_name=ai_name, memories=mem_text,
        evidence_constraints=EVIDENCE_CONSTRAINTS,
    )
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
        "status": "pending_review",
    }
    database.upsert_profile(profile)
    log.info(f"Relationship Profile ({ai_id}) rebuilt (v{_get_version(profile_id)}, {len(mems)} sources) → pending_review")
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


def supersede_all_profiles() -> int:
    """Mark all existing profiles as superseded."""
    profiles = database.list_profiles()
    count = 0
    for p in profiles:
        if p.get("status") != "superseded":
            database.supersede_profile(p["id"])
            count += 1
    log.info(f"Superseded {count} profiles")
    return count
