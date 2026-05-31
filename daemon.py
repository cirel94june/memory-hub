"""
Memory Daemon：定期后台整理
- 合并相似记忆
- 压缩日记（日记→周记→月记）
- 工作事务归档到职业生涯
- 衰减 + 自动归档
- 推送到 GitHub
"""
import json
import logging
from datetime import datetime, timezone, timedelta

from config import GEMINI_API_KEY, MERGE_SIMILARITY, get_room
from embedding import get_embedding, cosine_similarity, unpack_embedding, pack_embedding
import github_store as store

log = logging.getLogger("daemon")


async def _call_gemini_flash(prompt: str) -> str:
    """调用小模型做整理"""
    if not GEMINI_API_KEY:
        return ""
    import httpx
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 512},
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(url, json=body)
            resp.raise_for_status()
            return resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        log.error(f"Gemini Flash error: {e}")
        return ""


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

            # 让小模型合并
            prompt = f"""将以下两条记忆合并成一条简洁的陈述句，保留所有重要信息：
记忆1：{a['content']}
记忆2：{b['content']}
只输出合并后的一句话。"""
            merged = await _call_gemini_flash(prompt)
            if not merged:
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

    for ai_id in ["claude", "gemini", "gpt"]:
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
            prompt = f"""你是{ai_id}。以下是你这一周的日记片段，请压缩成一段简洁的周记（100字以内），保留关键事件和情感变化：

{texts}

只输出周记内容。"""
            digest = await _call_gemini_flash(prompt)
            if not digest:
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
        summary = await _call_gemini_flash(prompt)
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

    result = await _call_gemini_flash(prompt)
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
                merged = await _call_gemini_flash(f"合并以下信息为一句简洁的陈述：\n{texts}\n只输出一句话。")
                if not merged:
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

        chapter = await _call_gemini_flash(prompt)
        if not chapter:
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


# ── 主入口：一键执行所有整理 ──

async def run_full_maintenance() -> dict:
    """执行完整的记忆整理流程"""
    log.info("Starting full memory maintenance...")

    from memory_ops import run_decay

    results = {}

    # 1. 合并相似记忆
    results["merge"] = await merge_similar()
    log.info(f"  Merge: {results['merge']}")

    # 2. 压缩日记
    results["compress"] = await compress_diaries()
    log.info(f"  Compress: {results['compress']}")

    # 3. 工作事务归档
    results["work_archive"] = await archive_old_work()
    log.info(f"  Work archive: {results['work_archive']}")

    # 4. 客厅整理
    results["living_room"] = await tidy_living_room()
    log.info(f"  Living room: {results['living_room']}")

    # 5. 心理感悟蒸馏
    results["psychology"] = await distill_psychology()
    log.info(f"  Psychology: {results['psychology']}")

    # 6. 衰减
    results["decay"] = await run_decay()
    log.info(f"  Decay: {results['decay']}")

    # 7. 推送到 GitHub
    await store.push_dirty()

    # 8. 重建所有 AI 的走廊
    from corridor import rebuild_all_corridors
    await rebuild_all_corridors()
    log.info("Maintenance complete, corridors rebuilt")

    return results
