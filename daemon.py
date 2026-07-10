"""
Memory Daemon：定期后台整理
- 合并相似记忆
- 压缩日记（日记→周记→月记）
- 工作事务归档到职业生涯
- 衰减 + 自动归档
- 推送到 GitHub
"""
import asyncio
import json
import logging
import time
from datetime import datetime, timezone, timedelta

from config import (LLM_API_KEY, LLM_MODEL, LLM_BASE_URL, MERGE_SIMILARITY, get_room, AI_ROLES)
from embedding import get_embedding, cosine_similarity, unpack_embedding, pack_embedding
import github_store as store
import daemon_status

log = logging.getLogger("daemon")


async def _call_llm(prompt: str) -> str:
    """调用中转站小模型做整理（OpenAI 兼容格式）"""
    import httpx

    if not LLM_API_KEY:
        log.warning("LLM_API_KEY not set, skipping daemon LLM call")
        return ""

    url = f"{LLM_BASE_URL}/chat/completions"
    headers = {"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"}
    body = {
        "model": LLM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 2048,
    }
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(url, json=body, headers=headers)
            resp.raise_for_status()
            result = resp.json()["choices"][0]["message"]["content"]
            log.info(f"LLM OK ({LLM_MODEL})")
            return result
    except Exception as e:
        log.error(f"LLM error ({LLM_MODEL}@{LLM_BASE_URL}): {e}")
        return ""


_REFUSAL_PATTERNS = [
    "i can't", "i cannot", "i'm not able", "i am not able",
    "i'm unable", "as an ai", "as a language model",
    "i don't have the ability", "i'm sorry, but i",
    "无法假装", "无法扮演", "我不能", "作为AI", "作为一个AI",
    "作为语言模型", "我没有能力", "抱歉，我无法", "不具备",
    "我是一个人工智能", "i apologize", "i must decline",
]


def _is_refusal(text: str) -> bool:
    """检测 LLM 返回的内容是否是拒绝/元叙述而非真实内容"""
    if not text or len(text.strip()) < 5:
        return True
    lower = text.lower().strip()
    return any(p in lower for p in _REFUSAL_PATTERNS)


# ── 1. 合并相似记忆 ──

async def merge_similar() -> dict:
    """找出相似度>阈值的记忆对，用小模型合并成一条"""
    all_mems = store.get_all_memories()
    active = [m for m in all_mems.values()
              if m.get("status") == "active" and m.get("embedding") and m.get("room") != "game_room"]

    merged_count = 0
    skip_ids = set()

    for i, a in enumerate(active):
        if a["id"] in skip_ids:
            continue
        for b in active[i+1:]:
            if b["id"] in skip_ids:
                continue
            if a.get("room") != b.get("room"):
                continue
            if a.get("owner_ai", "") != b.get("owner_ai", ""):
                continue

            sim = cosine_similarity(unpack_embedding(a["embedding"]), unpack_embedding(b["embedding"]))
            if sim < MERGE_SIMILARITY:
                continue

            # 让小模型合并（带人物速查防止张冠李戴）
            try:
                import identity_registry
                _glossary = identity_registry.glossary_text() + "\n\n"
            except Exception:
                _glossary = ""
            prompt = f"""{_glossary}将以下两条记忆合并成一条简洁的陈述句。
⚠️ 归属保护：谁的东西/行为/特征，合并后必须保留主语。不要把A的特征写到B身上。
记忆1：{a['content']}
记忆2：{b['content']}
只输出合并后的一句话，保留所有重要信息和归属关系。"""
            merged = await _call_llm(prompt)
            if not merged or _is_refusal(merged):
                continue

            merged = merged.strip().strip('"')

            # 保留重要度更高的那条，更新内容
            keep = a if float(a.get("importance", 0)) >= float(b.get("importance", 0)) else b
            remove = b if keep is a else a

            keep["content"] = merged
            keep["importance"] = max(float(a.get("importance", 0.5)), float(b.get("importance", 0.5)))
            keep["activation_count"] = int(a.get("activation_count", 0)) + int(b.get("activation_count", 0))
            vec = await get_embedding(merged)
            if vec:
                keep["embedding"] = pack_embedding(vec)
            now = datetime.now(timezone.utc).isoformat()
            keep["updated_at"] = now
            history = keep.get("history", [])
            history.append({"v": len(history)+1, "content": merged, "date": now, "by": "daemon_merge"})
            keep["history"] = history

            store.set_memory(keep)
            store.remove_memory(remove["id"])
            skip_ids.add(remove["id"])
            merged_count += 1
            log.info(f"  Merged: [{a['content'][:30]}] + [{b['content'][:30]}] -> [{merged[:30]}]")

    return {"merged": merged_count}


# ── 2. 压缩日记（日记→周记→月记） ──

async def compress_diaries() -> dict:
    """把超过7天的日记条目压缩成周记"""
    all_mems = store.get_all_memories()
    now = datetime.now(timezone.utc)
    compressed = 0

    for ai_id in AI_ROLES:
        # 找该 AI 超过7天的日记条目
        diary_entries = [
            m for m in all_mems.values()
            if m.get("room") == "diary" and m.get("owner_ai") == ai_id
            and m.get("status") == "active" and m.get("category") != "weekly_digest"
        ]

        old_entries = []
        for entry in diary_entries:
            try:
                created = datetime.fromisoformat(entry["created_at"])
                if (now - created).days >= 7:
                    old_entries.append(entry)
            except Exception:
                continue

        if len(old_entries) < 3:
            continue  # 太少不值得压缩

        # 按周分组
        week_groups: dict[str, list] = {}
        for entry in old_entries:
            created = datetime.fromisoformat(entry["created_at"])
            week_key = created.strftime("%Y-W%W")
            if week_key not in week_groups:
                week_groups[week_key] = []
            week_groups[week_key].append(entry)

        for week_key, entries in week_groups.items():
            if len(entries) < 2:
                continue

            texts = "\n".join([f"- {e['content']}" for e in entries])
            prompt = f"""你是{ai_id}。以下是你这一周的日记片段，请整理成一段周记摘要（200~300字），保留关键事件和情感变化。

要求：
- 用日常口语写，像朋友聊天一样自然，不要用文言文或书面语
- 按时间顺序梳理这一周发生了什么
- 保留具体的人名、事件、情绪细节，不要过度抽象概括
- 如果有重要的心理变化或领悟，用大白话说清楚

{texts}

只输出周记内容，不要加标题或额外说明。"""
            digest = await _call_llm(prompt)
            if not digest or _is_refusal(digest):
                log.warning(f"  Skipped diary compress for {ai_id} week {week_key}: LLM refusal or empty")
                continue

            digest = digest.strip()
            # 创建周记条目
            import time
            now_str = datetime.now(timezone.utc).isoformat()
            weekly = {
                "id": f"mem_{int(time.time()*1000)}_wk",
                "content": f"[周记 {week_key}] {digest}",
                "layer": "private" if entries[0].get("layer") == "private" else "shared",
                "room": "diary",
                "category": "weekly_digest",
                "owner_ai": ai_id,
                "importance": max(float(e.get("importance", 0.5)) for e in entries),
                "emotion_arousal": sum(float(e.get("emotion_arousal", 0.3)) for e in entries) / len(entries),
                "decay_score": 1.0,
                "activation_count": sum(int(e.get("activation_count", 0)) for e in entries),
                "source_ai": "daemon",
                "status": "active",
                "created_at": entries[0]["created_at"],
                "updated_at": now_str,
                "history": [{"v": 1, "content": digest, "date": now_str, "by": "daemon_compress"}],
            }
            vec = await get_embedding(digest)
            if vec:
                weekly["embedding"] = pack_embedding(vec)
            store.set_memory(weekly)

            # 归档原始日记
            for e in entries:
                e["status"] = "archived"
                e["updated_at"] = now_str
                store.set_memory(e)

            compressed += len(entries)
            log.info(f"  Compressed {len(entries)} diary entries for {ai_id} week {week_key}")

    return {"compressed": compressed}


# ── 3. 工作事务归档 ──

async def archive_old_work() -> dict:
    """超过7天未被召回的工作事务 → 压缩后存入职业生涯"""
    all_mems = store.get_all_memories()
    now = datetime.now(timezone.utc)
    archived = 0

    work_items = [
        m for m in all_mems.values()
        if m.get("room") == "work_tasks" and m.get("status") == "active"
    ]

    old_items = []
    for item in work_items:
        try:
            updated = datetime.fromisoformat(item.get("last_activated") or item["created_at"])
            if (now - updated).days >= 7:
                old_items.append(item)
        except Exception:
            continue

    if len(old_items) < 1:
        return {"archived": 0}

    # 逐条压缩并转移
    import time
    now_str = now.isoformat()
    for item in old_items:
        # 压缩
        prompt = f"""将以下工作记录压缩成一句话的职业经历描述：
{item['content']}
只输出一句话。"""
        summary = await _call_llm(prompt)
        if not summary:
            summary = item["content"][:100]
        summary = summary.strip().strip('"')

        # 创建职业记忆
        career_mem = {
            "id": f"mem_{int(time.time()*1000)}_career",
            "content": summary,
            "layer": "shared",
            "room": "career",
            "category": "work_history",
            "owner_ai": "",
            "importance": 0.3,
            "emotion_arousal": 0.1,
            "decay_score": 1.0,
            "activation_count": 0,
            "source_ai": "daemon",
            "status": "active",
            "created_at": item["created_at"],
            "updated_at": now_str,
            "history": [{"v": 1, "content": summary, "date": now_str, "by": "daemon_archive_work"}],
        }
        vec = await get_embedding(summary)
        if vec:
            career_mem["embedding"] = pack_embedding(vec)
        store.set_memory(career_mem)

        # 归档原始工作记录
        item["status"] = "archived"
        item["updated_at"] = now_str
        store.set_memory(item)
        archived += 1

    return {"archived": archived}


# ── 4. 客厅整理（月度去重精炼） ──

async def tidy_living_room() -> dict:
    """客厅记忆去重+精炼，保持核心信息简洁"""
    all_mems = store.get_all_memories()
    living = [m for m in all_mems.values()
              if m.get("room") == "living_room" and m.get("status") == "active"]

    if len(living) < 5:
        return {"tidied": 0}

    # 先做相似记忆合并（复用 merge_similar 的逻辑，但只针对客厅）
    # 然后让小模型审视全部客厅内容，标记冗余
    contents = "\n".join([f"[{m['id']}] {m['content']}" for m in living])
    prompt = f"""以下是一个人的核心身份信息列表。请找出：
1. 内容重复或高度相似的条目（列出应该合并的 ID 组）
2. 已经过时的信息（如果能判断的话）
3. 可以合并精简的表述

当前条目：
{contents}

以 JSON 格式输出：
{{
  "merge_groups": [["id1", "id2"], ...],
  "outdated": ["id3", ...],
  "summary": "对当前客厅状态的简要评价"
}}
只输出 JSON。"""

    result = await _call_llm(prompt)
    tidied = 0

    if result:
        try:
            result = result.strip()
            if result.startswith("```"):
                result = result.split("\n", 1)[-1].rsplit("```", 1)[0]
            analysis = json.loads(result)

            import time
            now_str = datetime.now(timezone.utc).isoformat()

            # 合并
            for group in analysis.get("merge_groups", []):
                mems_to_merge = [m for m in living if m["id"] in group]
                if len(mems_to_merge) < 2:
                    continue
                texts = "\n".join([m["content"] for m in mems_to_merge])
                try:
                    import identity_registry
                    _g = identity_registry.glossary_text() + "\n\n"
                except Exception:
                    _g = ""
                merged = await _call_llm(f"{_g}合并以下信息为一句简洁的陈述。⚠️ 保留主语归属，不要把A的特征/行为写到B身上：\n{texts}\n只输出一句话。")
                if not merged or _is_refusal(merged):
                    continue
                merged = merged.strip().strip('"')

                keep = max(mems_to_merge, key=lambda x: float(x.get("importance", 0)))
                keep["content"] = merged
                keep["updated_at"] = now_str
                vec = await get_embedding(merged)
                if vec:
                    keep["embedding"] = pack_embedding(vec)
                store.set_memory(keep)

                for m in mems_to_merge:
                    if m["id"] != keep["id"]:
                        m["status"] = "archived"
                        m["updated_at"] = now_str
                        store.set_memory(m)
                        tidied += 1

        except Exception as e:
            log.error(f"Living room tidy error: {e}")

    return {"tidied": tidied}


# ── 5. 心理感悟蒸馏 ──

async def distill_psychology() -> dict:
    """
    心理状态房间的特殊处理：
    - 零碎感悟按月蒸馏成"人生章节"
    - 原始感悟归档保留（它们是重要经历）
    - 蒸馏后的章节长期保存，很少衰减
    """
    all_mems = store.get_all_memories()
    now = datetime.now(timezone.utc)

    psych_mems = [m for m in all_mems.values()
                  if m.get("room") == "psychology" and m.get("status") == "active"
                  and m.get("category") != "life_chapter"]

    # 找超过30天的感悟
    old_entries = []
    for m in psych_mems:
        try:
            created = datetime.fromisoformat(m["created_at"])
            if (now - created).days >= 30:
                old_entries.append(m)
        except Exception:
            continue

    if len(old_entries) < 3:
        return {"distilled": 0}

    # 按月分组
    month_groups: dict[str, list] = {}
    for m in old_entries:
        month_key = m["created_at"][:7]  # "2026-05"
        if month_key not in month_groups:
            month_groups[month_key] = []
        month_groups[month_key].append(m)

    distilled = 0
    import time
    now_str = now.isoformat()

    for month, entries in month_groups.items():
        if len(entries) < 2:
            continue

        texts = "\n".join([f"- {e['content']}" for e in entries])
        prompt = f"""以下是一个人在 {month} 月份的心理感悟和人生经历碎片。
请蒸馏成一段"人生章节"（80-150字），要：
- 保留核心情感和成长脉络
- 尊重原始感受，不要美化或淡化
- 写成第三人称的叙述，像在写传记

感悟碎片：
{texts}

只输出章节内容。"""

        chapter = await _call_llm(prompt)
        if not chapter or _is_refusal(chapter):
            log.warning(f"  Skipped psychology distill for {month}: LLM refusal or empty")
            continue
        chapter = chapter.strip()

        # 创建人生章节
        chapter_mem = {
            "id": f"mem_{int(time.time()*1000)}_ch",
            "content": f"[{month} 人生章节] {chapter}",
            "layer": "shared",
            "room": "psychology",
            "category": "life_chapter",
            "owner_ai": "",
            "importance": 0.8,
            "emotion_arousal": max(float(e.get("emotion_arousal", 0.5)) for e in entries),
            "decay_score": 1.0,
            "activation_count": 0,
            "source_ai": "daemon",
            "status": "active",
            "created_at": entries[0]["created_at"],
            "updated_at": now_str,
            "history": [{"v": 1, "content": chapter, "date": now_str, "by": "daemon_distill"}],
        }
        vec = await get_embedding(chapter)
        if vec:
            chapter_mem["embedding"] = pack_embedding(vec)
        store.set_memory(chapter_mem)

        # 归档原始感悟（不删除，它们是重要经历）
        for e in entries:
            e["status"] = "archived"
            e["updated_at"] = now_str
            store.set_memory(e)
            distilled += 1

        log.info(f"  Distilled {len(entries)} psychology entries for {month}")

    return {"distilled": distilled}


# ── 6. 过时记忆自动检测 ──

async def refresh_current_status() -> dict:
    """重写"当前状态"画像（职业/健康/生活近况各一段）。

    借鉴 Ombre portrait_engine：现状靠整段重写维护，旧信息在重写时被新信息替换，
    不依赖旧记忆碎片互相取代。材料只取近 90 天，按新旧排序交给小模型。
    """
    import current_status
    import identity_registry

    all_mems = store.get_all_memories()
    now = datetime.now(timezone.utc)
    user_name = identity_registry.get_registry().get("user", {}).get("canonical", "小猫")
    prev_sections = current_status.get_status().get("sections", {})
    new_sections = {}

    for key, meta in current_status.SECTIONS.items():
        mems = []
        for m in all_mems.values():
            if m.get("status") != "active" or m.get("room") not in meta["rooms"]:
                continue
            if m.get("layer") == "private":
                continue
            try:
                created = datetime.fromisoformat(m["created_at"])
            except Exception:
                continue
            if (now - created).days > 90:
                continue
            mems.append((created, m))

        mems.sort(key=lambda x: x[0], reverse=True)
        evidence = "\n".join(
            f"- [{c.strftime('%Y-%m-%d')}] {m['content'][:180]}"
            for c, m in mems[:25]
        )
        prev_text = (prev_sections.get(key) or {}).get("text", "")

        if not evidence and not prev_text:
            continue

        prompt = f"""你在维护{user_name}的「{meta['label']}」当前状态画像。这段画像会作为"现在的情况"注入给 AI，必须只反映**最新**状态。

{identity_registry.glossary_text()}

上一版画像：
{prev_text or '（无）'}

近 90 天记忆材料（按时间从新到旧）：
{evidence or '（无新材料）'}

重写规则：
- 输出一段 ≤150 字的当前状态描述，以**最新日期**的信息为准。
- 如果新材料显示状态已变化（如换了工作、身体好转），旧状态**不能再出现**，只写新状态；有必要时可加一句"此前是X，现已变为Y"。
- 材料只是"提到过"的话题不算状态变化（聊到某职业≠换了职业）。
- 没有新材料时保留上一版原文。
- 不确定的信息不要写成确定事实。

只输出画像正文，不要解释。"""

        text = await _call_llm(prompt)
        text = (text or "").strip()
        if not text:
            text = prev_text
        if len(text) > 400:
            text = text[:397] + "..."
        if text:
            new_sections[key] = {
                "text": text,
                "updated_at": now.isoformat(timespec="seconds"),
                "evidence_count": len(mems),
            }

    if new_sections:
        await current_status.save_status(new_sections)
    return {k: v["text"][:60] for k, v in new_sections.items()}


async def refresh_identity_registry() -> dict:
    """从近期记忆里收编新出现的人物称呼，维护人物注册表。

    保守策略：只在有明确证据时新增别名/人物；不删除已有条目（人工条目更不动）。
    """
    import identity_registry

    all_mems = store.get_all_memories()
    now = datetime.now(timezone.utc)
    snippets = []
    for m in all_mems.values():
        if m.get("status") != "active":
            continue
        if m.get("room") not in ("relationships", "social", "living_room", "preferences"):
            continue
        try:
            created = datetime.fromisoformat(m["created_at"])
        except Exception:
            continue
        if (now - created).days > 21:
            continue
        snippets.append(f"- {m['content'][:150]}")
    if not snippets:
        return {"skipped": "no recent material"}

    reg = identity_registry.get_registry()
    current = identity_registry.glossary_text()

    prompt = f"""你在维护一份人物注册表，防止 AI 把同一个人的不同称呼当成不同的人，或把人名误认成宠物。

当前注册表：
{current}

近期记忆片段：
{chr(10).join(snippets[:40])}

任务：找出记忆片段中出现、但注册表没覆盖的**人的称呼**。
- 如果某称呼有明确证据指向注册表里已有的人（比如"XX就是小猫"、同一语境明显同指）→ 作为别名归入那个人。
- 如果是一个新的人（有名字、和用户有互动）→ 新增条目，写清关系。
- AI 角色（注册表里已列出的）不要重复添加。
- 宠物、虚构角色、路人一次性提及的不收。
- 没有把握就不要输出，宁缺勿滥。

输出纯 JSON：
{{
  "add_user_aliases": ["确认是用户本人的新称呼"],
  "add_people": [{{"canonical": "名字", "aliases": [], "relation": "和用户的关系", "note": "一句备注"}}],
  "add_aliases_to": [{{"canonical": "注册表里已有的名字", "aliases": ["新发现的别名"]}}]
}}
没有新发现就输出 {{}}。只输出 JSON。"""

    raw = await _call_llm(prompt)
    if not raw:
        return {"skipped": "llm failed"}
    try:
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0]
        updates = json.loads(raw)
    except Exception as e:
        return {"skipped": f"parse error: {e}"}

    changed = 0
    user = reg.setdefault("user", {"canonical": "小猫", "aliases": [], "note": ""})
    for alias in updates.get("add_user_aliases", []) or []:
        alias = str(alias).strip()
        if alias and alias != user.get("canonical") and alias not in user.get("aliases", []):
            user.setdefault("aliases", []).append(alias)
            changed += 1

    existing = {p["canonical"]: p for p in reg.get("people", [])}
    for item in updates.get("add_aliases_to", []) or []:
        target = existing.get(str(item.get("canonical", "")).strip())
        if not target:
            continue
        for alias in item.get("aliases", []) or []:
            alias = str(alias).strip()
            if alias and alias not in target.get("aliases", []):
                target.setdefault("aliases", []).append(alias)
                changed += 1

    known_names = {user.get("canonical", "")} | set(user.get("aliases", [])) | set(existing)
    for p in updates.get("add_people", []) or []:
        name = str(p.get("canonical", "")).strip()
        if not name or name in known_names:
            continue
        reg.setdefault("people", []).append({
            "canonical": name,
            "aliases": [str(a).strip() for a in p.get("aliases", []) if str(a).strip()],
            "relation": str(p.get("relation", "")).strip(),
            "note": str(p.get("note", "")).strip(),
            "source": "daemon",
        })
        known_names.add(name)
        changed += 1

    if changed:
        await identity_registry.save_registry(f"daemon folded {changed} identity updates")
    return {"changed": changed}


async def detect_stale_memories() -> dict:
    """扫描活跃记忆，用小模型判断是否已过时。

    检测策略：
    1. 找出 importance ≥ 0.4 且创建超过 14 天的活跃记忆
    2. 对每条候选，收集同房间的近期记忆作为"当前状态"参考
    3. 用小模型判断旧记忆是否与近期记忆矛盾/已被更新
    4. 过时的 → 标记 status="stale" + 生成更新建议
    """
    all_mems = store.get_all_memories()
    now = datetime.now(timezone.utc)
    stale_count = 0
    checked = 0

    # 找候选：重要度 ≥ 0.4、超过 14 天、非 game_room
    candidates = []
    for m in all_mems.values():
        if m.get("status") != "active":
            continue
        if m.get("room") in ("game_room", "infra_changelog"):
            continue
        if float(m.get("importance", 0)) < 0.4:
            continue
        try:
            created = datetime.fromisoformat(m["created_at"])
            if (now - created).days < 14:
                continue
        except Exception:
            continue
        candidates.append(m)

    if not candidates:
        return {"checked": 0, "stale": 0}

    # 按房间分组近期记忆（14 天内）作参考
    recent_by_room: dict[str, list[dict]] = {}
    for m in all_mems.values():
        if m.get("status") != "active":
            continue
        try:
            created = datetime.fromisoformat(m["created_at"])
            if (now - created).days <= 14:
                room = m.get("room", "living_room")
                if room not in recent_by_room:
                    recent_by_room[room] = []
                recent_by_room[room].append(m)
        except Exception:
            continue

    # 限制每次检查量（防止 API 爆）
    import random
    if len(candidates) > 20:
        candidates = random.sample(candidates, 20)

    now_str = now.isoformat()

    import current_status

    for old_mem in candidates:
        room = old_mem.get("room", "living_room")
        recent = recent_by_room.get(room, [])
        # 当前状态画像作为额外参考：即使该房间没有近期记忆，
        # 只要画像明确显示状态已变，旧记忆也能被判过时
        portrait_ref = current_status.section_reference(room)

        # 既没有近期记忆也没有画像参考，跳过（无法判断是否过时）
        if not recent and not portrait_ref:
            continue

        recent_text = "\n".join([
            f"- [{r['created_at'][:10]}] {r['content'][:100]}"
            for r in sorted(recent, key=lambda x: x.get("created_at", ""), reverse=True)[:8]
        ]) or "（无）"
        portrait_text = f"\n\n当前状态画像（后台定期重写的最新状态，可作为判断依据）：\n{portrait_ref}" if portrait_ref else ""

        prompt = f"""你是一个记忆审核助手。请判断以下旧记忆是否已过时。

旧记忆（创建于 {old_mem['created_at'][:10]}）：
{old_mem['content']}

同领域的近期记忆：
{recent_text}{portrait_text}

判断规则：
- 如果近期记忆中有内容直接矛盾或更新了旧记忆的信息 → 过时
- 如果旧记忆描述的状态/事实已被新信息替代 → 过时
- 如果旧记忆仍然有效、没被新信息覆盖 → 有效
- 如果不确定 → 有效（宁可保留）

输出 JSON：
{{
  "is_stale": true或false,
  "reason": "简短原因",
  "suggested_update": "如果过时，建议如何更新（一句话）。如果有效则为空。"
}}
只输出 JSON。"""

        result = await _call_llm(prompt)
        checked += 1

        if not result:
            continue

        try:
            result = result.strip()
            if result.startswith("```"):
                result = result.split("\n", 1)[-1].rsplit("```", 1)[0]
            analysis = json.loads(result)

            if analysis.get("is_stale"):
                # 标记为 stale
                old_mem["status"] = "stale"
                old_mem["updated_at"] = now_str

                # 追加评论记录过时原因和更新建议
                comments = old_mem.get("comments", [])
                if not isinstance(comments, list):
                    comments = []
                comments.append({
                    "date": now_str,
                    "author": "daemon",
                    "kind": "stale_note",
                    "content": f"⚠️ 可能已过时：{analysis.get('reason', '未知')}",
                })
                if analysis.get("suggested_update"):
                    comments.append({
                        "date": now_str,
                        "author": "daemon",
                        "kind": "update_suggestion",
                        "content": f"💡 建议更新：{analysis['suggested_update']}",
                    })
                old_mem["comments"] = comments
                store.set_memory(old_mem)
                stale_count += 1
                log.info(f"  Stale: [{old_mem['content'][:40]}] - {analysis.get('reason')}")

        except Exception as e:
            log.warning(f"  Stale check parse error: {e}")

    return {"checked": checked, "stale": stale_count}


async def _backfill_analysis():
    """补分析 quick 模式存入的记忆"""
    all_mems = store.get_all_memories()
    backfilled = 0
    for mem in all_mems.values():
        if not mem.get("_needs_analysis"):
            continue
        if mem.get("status") != "active":
            continue
        analysis = await analyzer.analyze(mem["content"])
        if analysis:
            if not mem.get("category") and analysis.get("suggested_category"):
                mem["category"] = analysis["suggested_category"]
            mem["tags"] = json.dumps(analysis.get("tags", []))
            mem["domain"] = json.dumps(analysis.get("domain", []))
            mem["valence"] = analysis.get("valence", 0.5)
            if analysis.get("arousal") is not None:
                mem["emotion_arousal"] = analysis["arousal"]
        mem.pop("_needs_analysis", None)
        store.set_memory(mem)
        backfilled += 1
    if backfilled:
        await store.push_dirty()
        log.info(f"Backfilled analysis for {backfilled} memories")
    return backfilled


async def _auto_fix_about_prefix():
    """自动给缺少 [用户]/[互动]/[AI] 前缀的记忆补上前缀"""
    all_mems = store.get_all_memories()
    prefixes = ("[用户]", "[互动]", "[AI]")
    fixed = 0
    for mem in all_mems.values():
        if mem.get("status") != "active":
            continue
        content = mem.get("content", "")
        if any(content.startswith(p) for p in prefixes):
            continue
        # 简单规则判断（不调 LLM，省钱）
        room = mem.get("room", "")
        if room in ("diary", "dreams", "personality"):
            prefix = "[AI]"
        elif room in ("relationship", "game_room"):
            prefix = "[互动]"
        else:
            prefix = "[用户]"
        mem["content"] = f"{prefix} {content}"
        store.set_memory(mem)
        fixed += 1
    if fixed:
        await store.push_dirty()
        log.info(f"Auto-fixed about prefix for {fixed} memories")
    return fixed


async def _detect_contradictions():
    """检测可能矛盾的记忆对（同房间、高相似度但内容相反）"""
    from embedding import cosine_similarity, unpack_embedding
    all_mems = store.get_all_memories()
    active = [m for m in all_mems.values()
              if m.get("status") == "active" and m.get("embedding")]

    contradiction_pairs = []
    checked = set()

    for i, m1 in enumerate(active):
        vec1 = unpack_embedding(m1["embedding"])
        for m2 in active[i+1:]:
            if m1["id"] >= m2["id"]:
                pair_key = (m2["id"], m1["id"])
            else:
                pair_key = (m1["id"], m2["id"])
            if pair_key in checked:
                continue
            checked.add(pair_key)

            if m1.get("room") != m2.get("room"):
                continue

            vec2 = unpack_embedding(m2["embedding"])
            sim = cosine_similarity(vec1, vec2)

            if sim >= 0.75:
                # 高相似 → 可能重复，归档较旧的
                older = m1 if m1.get("created_at", "") < m2.get("created_at", "") else m2
                newer = m2 if older is m1 else m1
                older["status"] = "archived"
                comments = older.get("comments", [])
                if not isinstance(comments, list):
                    comments = []
                from memory_ops import _now
                comments.append({
                    "date": _now(),
                    "author": "daemon",
                    "kind": "supersede_note",
                    "content": f"自动去重：与 {newer['id']} 高度相似(sim={sim:.2f})，归档较旧条目",
                })
                older["comments"] = comments
                store.set_memory(older)
                contradiction_pairs.append((older["id"], newer["id"], sim))

    if contradiction_pairs:
        await store.push_dirty()
        log.info(f"Auto-dedup: archived {len(contradiction_pairs)} duplicate memories")

    return {"deduped": len(contradiction_pairs), "pairs": [(a, b, f"{s:.2f}") for a, b, s in contradiction_pairs[:10]]}


# ── 主入口：一键执行所有整理 ──

# 维护有两个触发源：进程内 _daemon_loop（每12h）和 GitHub Actions daemon.yml（0点/12点）。
# 用互斥锁防并发，用最小间隔防同一天重复整跑（重复跑会重复合并/做梦/备份）。
_maintenance_lock = asyncio.Lock()
MAINTENANCE_MIN_INTERVAL_HOURS = 6


def _last_success_within(hours: float) -> str | None:
    """如果最近一次成功维护在 hours 小时内，返回其完成时间，否则 None。"""
    status = daemon_status.read_status()
    if status.get("status") != "success":
        return None
    finished_at = status.get("finished_at") or status.get("updated_at") or ""
    try:
        ts = datetime.fromisoformat(finished_at)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    if datetime.now(timezone.utc) - ts < timedelta(hours=hours):
        return finished_at
    return None


async def run_full_maintenance(force: bool = False) -> dict:
    """执行完整的记忆整理流程"""
    if _maintenance_lock.locked():
        log.info("Maintenance already running, skipping this trigger")
        return {"skipped": "already_running"}

    if not force:
        recent = _last_success_within(MAINTENANCE_MIN_INTERVAL_HOURS)
        if recent:
            log.info(f"Maintenance ran recently ({recent}), skipping (force=true to override)")
            return {"skipped": "ran_recently", "last_finished_at": recent}

    async with _maintenance_lock:
        return await _run_full_maintenance_inner()


async def _run_full_maintenance_inner() -> dict:
    log.info("Starting full memory maintenance...")

    from memory_ops import run_decay

    results = {}
    steps = []
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    daemon_status.write_status({"status": "running", "started_at": started_at, "steps": []})

    async def run_step(key: str, label: str, func):
        step_start = time.perf_counter()
        try:
            value = await func()
            elapsed_ms = round((time.perf_counter() - step_start) * 1000)
            results[key] = value
            steps.append({"key": key, "label": label, "status": "ok", "duration_ms": elapsed_ms, "result": value})
            daemon_status.write_status({"status": "running", "started_at": started_at, "steps": steps})
            log.info(f"  {label}: {value}")
            return value
        except Exception as e:
            elapsed_ms = round((time.perf_counter() - step_start) * 1000)
            error = {"type": type(e).__name__, "message": str(e)}
            results[key] = {"error": error}
            steps.append({"key": key, "label": label, "status": "error", "duration_ms": elapsed_ms, "error": error})
            daemon_status.write_status({"status": "failed", "started_at": started_at, "steps": steps, "failed_step": key, "error": error})
            log.exception(f"  {label} failed: {e}")
            raise

    # 1. 合并相似记忆
    await run_step("merge", "Merge similar memories", merge_similar)

    # 2. 压缩日记
    await run_step("compress", "Compress diaries", compress_diaries)

    # 3. 工作事务归档
    await run_step("work_archive", "Archive old work", archive_old_work)

    # 4. 客厅整理
    await run_step("living_room", "Tidy living room", tidy_living_room)
    # 4.5. 自动刷新客厅/人物画像（前台仍保留 dry_run 建议按钮）
    try:
        from gateway import refresh_living_room_profile
        await run_step(
            "living_room_profile",
            "Refresh living room profiles",
            lambda: refresh_living_room_profile(dry_run=False, source_ai="daemon"),
        )
    except Exception as e:
        log.warning(f"  Living room profile refresh failed: {e}")

    # 4.6. 人物注册表收编（新外号/新人物归一，供所有小模型 prompt 使用）
    await run_step("identity_registry", "Refresh identity registry", refresh_identity_registry)

    # 4.7. 当前状态画像重写（职业/健康/近况——旧状态在重写中被替换）
    await run_step("current_status", "Refresh current status portrait", refresh_current_status)

    # 5. 心理感悟蒸馏
    await run_step("psychology", "Distill psychology", distill_psychology)

    # 6. 过时记忆检测（会参考当前状态画像判断旧记忆是否过时）
    await run_step("stale", "Detect stale memories", detect_stale_memories)

    # 8. 刷新对话捕获缓冲区（确保残留对话不丢）
    try:
        from conversation_capture import force_extract
        await run_step("capture_flush", "Flush capture buffers", force_extract)
    except Exception as e:
        log.warning(f"  Capture flush failed: {e}")

    # 9. 衰减
    await run_step("decay", "Run decay", run_decay)

    # 10. Persona State 休息（恢复精力）
    try:
        from persona_state import rest
        for ai_id in AI_ROLES:
            rest(ai_id)
        log.info("  Persona states rested")
    except Exception as e:
        log.warning(f"  Persona rest failed: {e}")

    # 10.5 补分析 quick 模式存入的记忆
    await run_step("backfill_analysis", "Backfill analysis", _backfill_analysis)

    # 10.6 自动补 about 前缀
    await run_step("fix_about", "Fix about prefixes", _auto_fix_about_prefix)

    # 10.7 自动去重（高相似度记忆归档较旧的）
    await run_step("dedup", "Deduplicate memories", _detect_contradictions)
    from memory_ops import deduplicate_public_memories
    await run_step("public_dedup", "Deduplicate public memories", lambda: deduplicate_public_memories(dry_run=False))
    from memory_ops import fix_private_capture_layers
    await run_step("private_layer_fix", "Fix private capture layers", lambda: fix_private_capture_layers(dry_run=False))

    # 10.75 记忆体检（身份冲突自动修，矛盾/张冠李戴写报告）+ 原文保险箱清理
    try:
        import memory_doctor
        await run_step("doctor", "Memory doctor checkup", memory_doctor.run_checkup)
    except Exception as e:
        log.warning(f"  Doctor checkup failed: {e}")
    try:
        import raw_vault
        raw_vault.prune(keep_days=120)
    except Exception as e:
        log.warning(f"  Raw vault prune failed: {e}")

    # 10.8 梦境日记（每个AI回顾今天的对话，写一篇日记）
    try:
        from dream import generate_dreams
        await run_step("dreams", "Generate dreams", generate_dreams)
    except Exception as e:
        log.warning(f"  Dreams failed: {e}")

    # 10.9 Memory Safety Kit：可读 Markdown + 安全报告导出到 GitHub/Obsidian
    try:
        from safety_export import export_obsidian
        await run_step("memory_safety_export", "Export Obsidian safety kit", lambda: export_obsidian(dry_run=False))
    except Exception as e:
        log.warning(f"  Memory safety export failed: {e}")

    # 11. 推送到 GitHub
    await run_step("github_push", "Push dirty store", store.push_dirty)

    # 12. 重建所有 AI 的走廊（含 persona state + unresolved）
    from corridor import rebuild_all_corridors
    await run_step("corridors", "Rebuild corridors", rebuild_all_corridors)
    log.info("Maintenance complete, corridors rebuilt")

    daemon_status.write_status({
        "status": "success",
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "steps": steps,
        "results": results,
    })
    return results

