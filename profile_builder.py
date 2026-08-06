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
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from typing import Optional

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

import database
from config import LLM_API_KEY, LLM_MODEL, LLM_BASE_URL

log = logging.getLogger("profile_builder")

AI_IDS = ["claude", "lucien", "jasper"]


# ════════════════════════════════════════════
#  Pydantic schemas for Profile validation
# ════════════════════════════════════════════

class ProfileField(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str | list[str]
    confidence: str
    evidence_tier: int
    source_ids: list[str]

    @field_validator("confidence")
    @classmethod
    def valid_confidence(cls, v):
        if v not in ("high", "medium", "low"):
            raise ValueError(f"Invalid confidence: {v}")
        return v

    @field_validator("evidence_tier")
    @classmethod
    def valid_tier(cls, v):
        if v not in (1, 2, 3, 4):
            raise ValueError(f"Invalid evidence_tier: {v}, must be 1-4")
        return v

    @field_validator("source_ids")
    @classmethod
    def non_empty_source_ids(cls, v):
        normalized = list(dict.fromkeys(s.strip() for s in v if isinstance(s, str) and s.strip()))
        if len(normalized) < 1:
            raise ValueError("source_ids must have at least 1 non-blank entry")
        return normalized


class UserProfileSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tier0: dict
    identity: ProfileField
    stable_preferences: Optional[ProfileField] = None
    communication_style: Optional[ProfileField] = None
    current_focus: Optional[ProfileField] = None
    health_status: Optional[ProfileField] = None
    boundaries: Optional[ProfileField] = None


class AgentProfileSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tier0: dict
    identity: ProfileField
    personality: Optional[ProfileField] = None
    style: Optional[ProfileField] = None
    notable_patterns: Optional[ProfileField] = None


class RelationshipProfileSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Optional[ProfileField] = None
    interaction_pattern: Optional[ProfileField] = None
    shared_context: Optional[ProfileField] = None
    boundaries: Optional[ProfileField] = None

    @model_validator(mode="after")
    def at_least_one_field(self):
        if not any([self.mode, self.interaction_pattern, self.shared_context, self.boundaries]):
            raise ValueError("Relationship profile must have at least one field")
        return self


_PROFILE_SCHEMAS = {
    "user": UserProfileSchema,
    "agent": AgentProfileSchema,
    "relationship": RelationshipProfileSchema,
}


def _validate_profile_schema(content_json: str, profile_type: str,
                             valid_mem_ids: set[str] = None) -> tuple[bool, str, str]:
    """Validate Profile JSON against its Pydantic schema.

    Returns (is_valid, error_message, validated_json).
    validated_json is model_dump_json() output — canonical, not raw LLM.
    """
    schema_cls = _PROFILE_SCHEMAS.get(profile_type)
    if not schema_cls:
        return False, f"Unknown profile type: {profile_type}", ""

    try:
        data = json.loads(content_json)
    except (json.JSONDecodeError, TypeError) as e:
        return False, f"Invalid JSON: {e}", ""

    if not isinstance(data, dict):
        return False, "Top-level must be an object", ""

    try:
        validated = schema_cls.model_validate(data)
    except Exception as e:
        return False, f"Schema validation failed: {e}", ""

    for field_name, field_val in data.items():
        if isinstance(field_val, dict) and "source_ids" in field_val:
            for sid in field_val["source_ids"]:
                stripped = sid.strip() if isinstance(sid, str) else ""
                if not stripped:
                    return False, f"Field '{field_name}' has blank source_id", ""
            if valid_mem_ids is not None:
                bad_ids = [sid.strip() for sid in field_val["source_ids"]
                           if sid.strip() not in valid_mem_ids]
                if bad_ids:
                    return False, f"Field '{field_name}' has source_ids not in stable mems: {bad_ids}", ""

    return True, "", validated.model_dump_json()

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


_PATTERN_TAG_RE = re.compile(r"^pattern:(.+)$", re.IGNORECASE)


def _extract_pattern_key(mem: dict) -> str | None:
    """Extract a pattern key from a group_dynamic memory.
    Only accepts explicit 'pattern:<key>' tags or a 'pattern_key' field.
    Returns None if no reliable key is found."""
    pattern_key = mem.get("pattern_key")
    if pattern_key and isinstance(pattern_key, str):
        key = pattern_key.strip().lower()
        return key if key else None

    tags_raw = mem.get("tags", "[]")
    if isinstance(tags_raw, str):
        try:
            tags = json.loads(tags_raw)
        except (json.JSONDecodeError, TypeError):
            tags = []
    else:
        tags = tags_raw if tags_raw else []
    for t in tags:
        if t and isinstance(t, str):
            mt = _PATTERN_TAG_RE.match(t.strip())
            if mt:
                key = mt.group(1).strip().lower()
                if key:
                    return key
    return None


def _filter_relationship_group_dynamic(mems: list[dict]) -> list[dict]:
    """Allow group_dynamic memories into Relationship Profile only if
    the same interaction pattern (by explicit pattern: tag) appears in >=3
    different memories and at least 1 per group is not roleplay/joke.
    Memories without a reliable pattern key are conservatively excluded."""
    normal = []
    group_dynamic = []
    for m in mems:
        cat = m.get("category", "")
        if "group_dynamic" in cat.lower() if cat else False:
            group_dynamic.append(m)
        else:
            normal.append(m)

    groups: dict[str, list[dict]] = {}
    for m in group_dynamic:
        key = _extract_pattern_key(m)
        if key is None:
            continue
        groups.setdefault(key, []).append(m)

    for key, members in groups.items():
        if len(members) >= 3:
            has_non_roleplay = any(
                not _is_excluded_category(m.get("category", ""))
                for m in members
            )
            if has_non_roleplay:
                normal.extend(members)

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
        f"category, provenance_type, fact_confidence, tags "
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
#  Stylization detector
# ════════════════════════════════════════════

_STYLIZED_PATTERNS = [
    "深邃的", "绚烂的", "独特的灵魂", "内心深处", "灵魂深处",
    "温暖的光芒", "如诗如画", "不可替代的", "无与伦比",
    "独一无二的存在", "照亮了", "点亮了", "温柔地守护",
    "深深的", "浓浓的", "满满的", "暖暖的",
    "宛如", "仿若", "恰似", "犹如一",
    "闪耀着", "绽放着", "散发着",
    "不经意间", "悄然", "默默地",
]

_STYLIZED_THRESHOLD = 3


def _text_too_stylized(text: str) -> bool:
    """Reject text with excessive literary embellishment."""
    if not text:
        return False
    count = sum(1 for p in _STYLIZED_PATTERNS if p in text)
    return count >= _STYLIZED_THRESHOLD


# ════════════════════════════════════════════
#  Field length limits + truncation
# ════════════════════════════════════════════

FIELD_CHAR_LIMITS = {
    "identity": 160,
    "personality": 160,
    "style": 160,
    "mode": 160,
    "interaction_pattern": 160,
    "shared_context": 160,
    "health_status": 160,
    "communication_style": 160,
    "current_focus": 60,
    "daily_summary": 60,
}

PERSONA_TOKEN_LIMIT = 100


def _truncate_profile_fields(content_json: str) -> str:
    """Enforce per-field character limits on Profile JSON."""
    try:
        data = json.loads(content_json)
    except (json.JSONDecodeError, TypeError):
        return content_json

    for field_name, limit in FIELD_CHAR_LIMITS.items():
        if field_name not in data:
            continue
        field = data[field_name]
        if isinstance(field, dict) and "value" in field:
            val = field["value"]
            if isinstance(val, str) and len(val) > limit:
                field["value"] = val[:limit - 3].rstrip() + "..."
            elif isinstance(val, list):
                field["value"] = [
                    (item[:limit - 3].rstrip() + "..." if isinstance(item, str) and len(item) > limit else item)
                    for item in val
                ]
        elif isinstance(field, str) and len(field) > limit:
            data[field_name] = field[:limit - 3].rstrip() + "..."

    if "notable_patterns" in data:
        pats = data["notable_patterns"]
        if isinstance(pats, dict) and "value" in pats and isinstance(pats["value"], list):
            pats["value"] = [
                (item[:157].rstrip() + "..." if isinstance(item, str) and len(item) > 160 else item)
                for item in pats["value"]
            ]

    return json.dumps(data, ensure_ascii=False)


# ════════════════════════════════════════════
#  Temporal stability check (stable_candidate)
# ════════════════════════════════════════════

def _check_temporal_stability(mems: list[dict], min_span_days: int = 3) -> tuple[list[dict], list[dict]]:
    """Split memories into stable (span >= min_span_days) and candidate groups.

    Groups memories by approximate topic (first 20 chars of content),
    then checks if the group spans at least min_span_days.

    Returns (stable_mems, candidate_mems).
    """
    if not mems:
        return [], []

    def _date_key(m):
        ca = m.get("created_at", "")
        return ca[:10] if len(ca) >= 10 else ""

    topic_groups = defaultdict(list)
    for m in mems:
        info_type = m.get("info_type", "")
        if info_type not in ("identity", "relationship"):
            topic_groups["__pass__"].append(m)
            continue
        key = m.get("content", "")[:20].strip()
        if not key:
            key = m.get("id", "unknown")
        topic_groups[key].append(m)

    stable = []
    candidate = []
    for key, group in topic_groups.items():
        if key == "__pass__":
            stable.extend(group)
            continue

        dates = {_date_key(m) for m in group if _date_key(m)}
        if len(dates) < 2:
            candidate.extend(group)
            continue

        sorted_dates = sorted(dates)
        try:
            first = datetime.strptime(sorted_dates[0], "%Y-%m-%d")
            last = datetime.strptime(sorted_dates[-1], "%Y-%m-%d")
            span = (last - first).days
        except ValueError:
            candidate.extend(group)
            continue

        if span >= min_span_days:
            stable.extend(group)
        else:
            candidate.extend(group)

    return stable, candidate


# ════════════════════════════════════════════
#  Shared Prompt Constraints
# ════════════════════════════════════════════

MAX_PROFILE_RETRIES = 3


async def _validate_and_retry(prompt: str, profile_type: str, valid_mem_ids: set[str],
                               is_agent_or_rel: bool = False) -> str | None:
    """Unified validation loop: up to MAX_PROFILE_RETRIES attempts.

    Each attempt runs: JSON parse → schema validation → first-person check
    → stylization check → source_id check → field truncation.
    Returns validated+truncated JSON string, or None if all retries fail.
    """
    retry_notes = []
    for attempt in range(MAX_PROFILE_RETRIES):
        suffix = "\n\n".join(retry_notes) if retry_notes else ""
        raw = await _call_llm(prompt + suffix)
        if not raw:
            log.warning(f"Profile LLM returned empty (attempt {attempt + 1})")
            continue

        content = _extract_json(raw)
        if not content:
            retry_notes.append("重要：上次输出不是合法 JSON，请确保输出以 { 开头、以 } 结尾的纯 JSON。")
            log.warning(f"Profile non-JSON output (attempt {attempt + 1})")
            continue

        ok, err, validated_json = _validate_profile_schema(content, profile_type, valid_mem_ids)
        if not ok:
            retry_notes.append(f"重要：上次输出 schema 校验失败（{err}），请严格按要求的 JSON 格式重新生成。")
            log.warning(f"Profile schema invalid (attempt {attempt + 1}): {err}")
            continue

        if is_agent_or_rel and _contains_first_person(validated_json):
            retry_notes.append("重要：你的上一次输出包含了第一人称（我），这是不允许的。请严格使用第三人称重新生成。")
            log.warning(f"Profile first-person detected (attempt {attempt + 1})")
            continue

        if _text_too_stylized(validated_json):
            retry_notes.append("重要：禁止使用华丽形容词（'深邃的''绚烂的''独特的灵魂'等），用平实语言重写。")
            log.warning(f"Profile too stylized (attempt {attempt + 1})")
            continue

        validated_json = _truncate_profile_fields(validated_json)
        return validated_json

    log.error(f"Profile generation failed after {MAX_PROFILE_RETRIES} attempts")
    return None


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

## 绝对禁止的写法
- 不把单次事件写成稳定人格（"她有一次迟到"≠"她经常迟到"）
- 不做心理诊断（不写"她有焦虑倾向"、"他有回避型依附"等）
- 不用夸张语气（不写"极其"、"无比"、"深深的"、"独一无二"）
- 不复述已有内容（不要把记忆原文串起来当 Profile）
- 不把事件原文拼接成段落（Profile 是提炼，不是拼贴）

## 字数硬上限
- identity_traits 等描述字段：每条 ≤160 字
- daily_summary / current_focus：≤60 字
- 超出自动截断。宁可写短，不要啰嗦。
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
    stable, candidates = _check_temporal_stability(mems)
    log.info(f"User Profile: {len(raw_mems)} raw → {len(mems)} filtered → {len(stable)} stable, {len(candidates)} candidate")

    if not stable:
        log.info("No stable memories found for User Profile")
        return None

    mem_ids = [m["id"] for m in stable]
    if not force and not _has_changed("user_ceci", mem_ids):
        log.info("User Profile unchanged, skipping")
        return None

    mem_text = "\n".join(
        f"- [{m['id']}] [{m['room']}|{m['info_type']}] {m['content'][:200]}" for m in stable
    )
    candidate_note = ""
    if candidates:
        candidate_note = f"\n\n（另有 {len(candidates)} 条候选断言跨度不足 3 天，暂不纳入正式 Profile，待更多证据确认。）"

    prompt = USER_PROFILE_PROMPT.format(
        memories=mem_text + candidate_note,
        evidence_constraints=EVIDENCE_CONSTRAINTS,
        tier0=json.dumps(TIER0_USER_FACTS, ensure_ascii=False),
    )
    content = await _validate_and_retry(prompt, "user", set(mem_ids))
    if not content:
        return None

    profile = {
        "id": "user_ceci",
        "profile_type": "user",
        "owner_ai": "",
        "content": content,
        "generated_at": _now(),
        "source_memory_ids": json.dumps(mem_ids),
        "stable_candidates": json.dumps([m["id"] for m in candidates]),
        "status": "pending_review",
    }
    database.upsert_profile(profile)
    log.info(f"User Profile rebuilt (v{_get_version('user_ceci')}, {len(stable)} stable, {len(candidates)} candidates) → pending_review")
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
    stable, candidates = _check_temporal_stability(mems)
    log.info(f"Agent Profile ({ai_id}): {len(raw_mems)} raw → {len(mems)} filtered → {len(stable)} stable, {len(candidates)} candidate")

    if not stable:
        log.info(f"No stable memories found for Agent Profile ({ai_id})")
        return None

    profile_id = f"agent_{ai_id}"
    mem_ids = [m["id"] for m in stable]
    if not force and not _has_changed(profile_id, mem_ids):
        log.info(f"Agent Profile ({ai_id}) unchanged, skipping")
        return None

    mem_text = "\n".join(
        f"- [{m['id']}] [{m['room']}|{m['info_type']}] {m['content'][:200]}" for m in stable
    )
    tier0 = json.dumps(TIER0_AGENT_FACTS.get(ai_id, {}), ensure_ascii=False)
    prompt = AGENT_PROFILE_PROMPT.format(
        ai_name=ai_name, memories=mem_text,
        evidence_constraints=EVIDENCE_CONSTRAINTS, tier0=tier0,
    )
    content = await _validate_and_retry(prompt, "agent", set(mem_ids), is_agent_or_rel=True)
    if not content:
        return None

    profile = {
        "id": profile_id,
        "profile_type": "agent",
        "owner_ai": ai_id,
        "content": content,
        "generated_at": _now(),
        "source_memory_ids": json.dumps(mem_ids),
        "stable_candidates": json.dumps([m["id"] for m in candidates]),
        "status": "pending_review",
    }
    database.upsert_profile(profile)
    log.info(f"Agent Profile ({ai_id}) rebuilt (v{_get_version(profile_id)}, {len(stable)} stable, {len(candidates)} candidates) → pending_review")
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
    stable, candidates = _check_temporal_stability(mems)
    log.info(f"Relationship Profile ({ai_id}): {len(raw_mems)} raw → {len(mems)} filtered → {len(stable)} stable, {len(candidates)} candidate")

    if not stable:
        log.info(f"No stable memories found for Relationship Profile ({ai_id})")
        return None

    profile_id = f"rel_{ai_id}_ceci"
    mem_ids = [m["id"] for m in stable]
    if not force and not _has_changed(profile_id, mem_ids):
        log.info(f"Relationship Profile ({ai_id}) unchanged, skipping")
        return None

    mem_text = "\n".join(
        f"- [{m['id']}] [{m['room']}|{m['info_type']}] {m['content'][:200]}" for m in stable
    )
    prompt = RELATIONSHIP_PROFILE_PROMPT.format(
        ai_name=ai_name, memories=mem_text,
        evidence_constraints=EVIDENCE_CONSTRAINTS,
    )
    content = await _validate_and_retry(prompt, "relationship", set(mem_ids), is_agent_or_rel=True)
    if not content:
        log.warning(f"Relationship Profile ({ai_id}) generation failed after retries")
        return None

    profile = {
        "id": profile_id,
        "profile_type": "relationship",
        "owner_ai": ai_id,
        "content": content,
        "generated_at": _now(),
        "source_memory_ids": json.dumps(mem_ids),
        "stable_candidates": json.dumps([m["id"] for m in candidates]),
        "status": "pending_review",
    }
    database.upsert_profile(profile)
    log.info(f"Relationship Profile ({ai_id}) rebuilt (v{_get_version(profile_id)}, {len(stable)} stable, {len(candidates)} candidates) → pending_review")
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
