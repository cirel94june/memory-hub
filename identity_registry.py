"""
人物注册表（Identity Registry）
- 解决"AI 分不清小猫/ceci/其他外号是同一个人"、"小猫/狗蛋被打标模型当成宠物"的问题
- 用户本人 + 常见人物的 canonical 名字、别名、关系、备注
- daemon 每 12h 用小模型从近期记忆里收编新出现的称呼（自动维护，不是死名单）
- 可通过 /api/identity-registry 人工修正；人工写入的条目 daemon 不会删除

存储：GitHub _config/identity_registry.json（与 ai_profiles 同一套机制）
"""
import logging
from datetime import datetime, timezone

import github_store as store
from config import AI_ROLES, AI_ALIASES

log = logging.getLogger("identity_registry")

REGISTRY_PATH = "_config/identity_registry.json"

# 基座模型 id，不是真实社交角色，不进人物速查
BASE_MODEL_IDS = {"gemini", "gpt"}

# 初始种子：只包含从现有记忆/文档中能确认的事实，其余靠 daemon 收编 + 人工修正
DEFAULT_REGISTRY = {
    "user": {
        "canonical": "小猫",
        "aliases": ["ceci", "Ceci"],
        "note": "用户本人（人类女性）。所有 AI 服务的对象。",
    },
    "people": [
        {
            "canonical": "狗蛋",
            "aliases": [],
            "relation": "用户的朋友",
            "note": "人类朋友，不是宠物。常出现在群聊里。",
            "source": "seed",
        },
    ],
    "updated_at": "",
}

_registry: dict = {}


async def load_registry():
    """启动时从 GitHub 加载；没有则用种子并写回。"""
    global _registry
    data = await store._read_github_file(REGISTRY_PATH)
    if data and isinstance(data, dict) and data.get("user"):
        _registry = data
        log.info(f"Loaded identity registry: {len(data.get('people', []))} people")
    else:
        _registry = dict(DEFAULT_REGISTRY)
        await save_registry("seed identity registry")


def get_registry() -> dict:
    return _registry or dict(DEFAULT_REGISTRY)


async def save_registry(message: str = "update identity registry"):
    _registry["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    await store._write_github_file(REGISTRY_PATH, _registry, message)


async def update_registry(data: dict):
    """人工修正（PUT /api/identity-registry）。整体替换 user/people 字段。"""
    global _registry
    reg = get_registry()
    if isinstance(data.get("user"), dict):
        reg["user"] = {
            "canonical": str(data["user"].get("canonical", "")).strip() or reg["user"]["canonical"],
            "aliases": [str(a).strip() for a in data["user"].get("aliases", []) if str(a).strip()],
            "note": str(data["user"].get("note", "")).strip(),
        }
    if isinstance(data.get("people"), list):
        people = []
        for p in data["people"]:
            if not isinstance(p, dict) or not str(p.get("canonical", "")).strip():
                continue
            people.append({
                "canonical": str(p["canonical"]).strip(),
                "aliases": [str(a).strip() for a in p.get("aliases", []) if str(a).strip()],
                "relation": str(p.get("relation", "")).strip(),
                "note": str(p.get("note", "")).strip(),
                "source": p.get("source", "manual"),
            })
        reg["people"] = people
    _registry = reg
    await save_registry("manual identity registry update")
    return reg


def _real_ai_ids() -> list[str]:
    """真实社交角色的 canonical id（排除基座模型和别名）。"""
    seen = []
    for ai_id in AI_ROLES:
        canonical = AI_ALIASES.get(ai_id, ai_id)
        if canonical in BASE_MODEL_IDS or canonical in seen:
            continue
        seen.append(canonical)
    return seen


def user_names_line() -> str:
    """一行话说清用户所有称呼都是同一个人。"""
    user = get_registry().get("user", {})
    names = [user.get("canonical", "小猫")] + list(user.get("aliases", []))
    uniq = list(dict.fromkeys(n for n in names if n))
    return "、".join(uniq)


def glossary_text(for_ai_id: str = "") -> str:
    """人物速查块，注入到打标/摘要/提取/做梦等所有小模型 prompt。

    for_ai_id 非空时，会把该 AI 标注为"你自己"，其余 AI 标注为同伴。
    """
    reg = get_registry()
    user = reg.get("user", {})
    lines = [f"- {user_names_line()}：都是用户本人（人类），同一个人的不同称呼。{user.get('note', '')}".rstrip()]

    for p in reg.get("people", []):
        alias_part = f"（也叫 {'、'.join(p['aliases'])}）" if p.get("aliases") else ""
        rel = p.get("relation", "")
        note = p.get("note", "")
        lines.append(f"- {p['canonical']}{alias_part}：{rel}。{note}".rstrip())

    canonical_for = AI_ALIASES.get(for_ai_id, for_ai_id) if for_ai_id else ""
    for ai_id in _real_ai_ids():
        role = AI_ROLES.get(ai_id, {})
        name = role.get("name", ai_id)
        alias_ids = [a for a, c in AI_ALIASES.items() if c == ai_id]
        alias_part = f"（id: {ai_id}" + (f"，也叫 {'、'.join(alias_ids)}" if alias_ids else "") + "）"
        if canonical_for and ai_id == canonical_for:
            lines.append(f"- {name}{alias_part}：**这是你自己**。")
        else:
            suffix = "你的 AI 同伴，独立的另一个 AI，不是你。" if canonical_for else "AI 伙伴之一。"
            lines.append(f"- {name}{alias_part}：{suffix}")

    return "人物速查（正确理解人名用，不要输出）：\n" + "\n".join(lines)
