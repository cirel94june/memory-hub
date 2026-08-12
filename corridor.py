"""
走廊系统（Corridor）
- 每个 AI 各自一条走廊
- 走廊 = 自动编译的快照文档，让 AI "醒来"时快速恢复上下文
- 内容来源：客厅精华 + 最近对话摘要 + 当前状态 + 关系要点 + 跨端动态

走廊会在以下时机重新编译：
  1. 每次记忆整理（Daemon）之后
  2. 手动触发
  3. 重要记忆写入后
"""
import json
import math
import logging
from datetime import datetime, timezone

from config import AI_ROLES, AI_ALIASES as _ALIASES
import github_store as store

log = logging.getLogger("corridor")
CORRIDOR_CACHE_TTL_MINUTES = 5

# In-memory corridor cache: avoids GitHub API reads on every request
_mem_cache: dict[str, dict] = {}  # ai_id -> {"text": str, "compiled_at": datetime}

_DEDUP_SIM_THRESHOLD = 0.75

# Recency weighting for section selection (Phase 1.7 块 7).
# recent_share=0.3 is a starting point; observe 1-2 weeks and drop to 0.2
# if important old memories get displaced.
_RECENT_DAYS = 30
_RECENT_SHARE = 0.3
_RECENT_DECAY = 30.0  # e-fold days for recency_score = exp(-days/30)


def _safe_float(val, default: float = 0.5) -> float:
    if val is None or val == "":
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _days_ago(iso_ts: str, now_utc: datetime | None = None) -> float:
    """Return days between iso_ts and now (naive treated as UTC, future clamped to 0)."""
    if not iso_ts:
        return float("inf")
    try:
        t = datetime.fromisoformat(iso_ts)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
    except Exception:
        return float("inf")
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    return max(0.0, (now_utc - t).total_seconds() / 86400)


def _recency_score(iso_ts: str, now_utc: datetime | None = None) -> float:
    """exp(-days/30). Missing/invalid ts → 0 (treated as ancient)."""
    d = _days_ago(iso_ts, now_utc)
    if d == float("inf"):
        return 0.0
    return math.exp(-d / _RECENT_DECAY)


def _pick_recency_weighted(
    candidates: list[dict],
    quota: int,
    now_utc: datetime | None = None,
    recent_days: int = _RECENT_DAYS,
    recent_share: float = _RECENT_SHARE,
) -> list[dict]:
    """Pick up to `quota` memories, reserving ~recent_share for recent items.

    Selection logic:
      1. Split candidates into "recent pool" (created_at within recent_days)
         and "old pool" (everything else).
      2. Fill up to ceil(quota × recent_share) from recent pool, sorted by
         recency_score × max(importance, 0.1) — importance floor prevents
         zero-importance items from dominating just because they're new.
      3. Fill remaining slots from old pool, sorted by
         importance × recency_score — same age-aware ranking.
      4. If recent pool has fewer than the reserved slots, the shortfall
         moves to the old pool (no wasted slots).
      5. No duplicates across pools (a memory is either recent or old).
    """
    # Clamp all numeric inputs so a bad caller can never return more than
    # `quota` items or blow up on negative/oversized shares.
    try:
        quota = max(0, int(quota))
    except (ValueError, TypeError):
        quota = 0
    try:
        recent_days = max(0, int(recent_days))
    except (ValueError, TypeError):
        recent_days = _RECENT_DAYS
    try:
        recent_share = max(0.0, min(1.0, float(recent_share)))
    except (ValueError, TypeError):
        recent_share = _RECENT_SHARE
    if not candidates or quota <= 0:
        return []
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)

    recent, old = [], []
    for m in candidates:
        d = _days_ago(m.get("created_at", ""), now_utc)
        if d <= recent_days:
            recent.append(m)
        else:
            old.append(m)

    recent_quota = min(len(recent), math.ceil(quota * recent_share))

    recent.sort(
        key=lambda m: _recency_score(m.get("created_at", ""), now_utc)
                      * max(_safe_float(m.get("importance"), 0.5), 0.1),
        reverse=True,
    )
    picked_recent = recent[:recent_quota]

    old.sort(
        key=lambda m: _safe_float(m.get("importance"), 0.5)
                      * _recency_score(m.get("created_at", ""), now_utc),
        reverse=True,
    )
    remaining = quota - len(picked_recent)
    picked_old = old[:remaining] if remaining > 0 else []

    # If old pool can't fill remaining slots, use leftover recent items so
    # we never waste quota just because the age distribution is skewed.
    still_needed = quota - len(picked_recent) - len(picked_old)
    picked_extra_recent = recent[recent_quota:recent_quota + still_needed] \
        if still_needed > 0 else []

    # Final cap belt: even if any branch above misbehaves, never exceed quota.
    return (picked_recent + picked_old + picked_extra_recent)[:quota]


def _dedup_texts(texts: list[str], max_items: int = 0) -> list[str]:
    """去除内容高度重复的文本（基于字符重叠比）。"""
    if not texts:
        return texts
    kept: list[str] = []
    for t in texts:
        t_set = set(t)
        is_dup = False
        for k in kept:
            k_set = set(k)
            overlap = len(t_set & k_set) / max(len(t_set | k_set), 1)
            if overlap > _DEDUP_SIM_THRESHOLD:
                is_dup = True
                break
        if not is_dup:
            kept.append(t)
    if max_items and len(kept) > max_items:
        kept = kept[:max_items]
    return kept


async def build_corridor(ai_id: str) -> str:
    """
    为指定 AI 编译走廊文档。
    返回一段自然语言文本，AI 读了就能"醒来"。
    """
    # 归一化到 canonical id（cloudy→claude、gpt→lucien、gemini→jasper 等），
    # 否则用别名请求时 owner_ai 匹配不上，走廊会缺私有材料
    ai_id = _ALIASES.get(ai_id, ai_id)
    all_mems = store.get_all_memories()

    now_utc = datetime.now(timezone.utc)

    # 1. 客厅要点（关于主人）— 8 条，recency-weighted (Phase 1.7 块 7)
    living_mems = [m for m in all_mems.values()
                   if m.get("room") == "living_room" and m.get("status") == "active"]
    living_picked = _pick_recency_weighted(living_mems, quota=8, now_utc=now_utc)
    living = [m["content"] for m in living_picked]

    # 2. 该 AI 的关系记忆 — 5 条，recency-weighted
    relationship_mems = [m for m in all_mems.values()
                         if m.get("room") == "relationship"
                         and m.get("owner_ai") == ai_id
                         and m.get("status") == "active"]
    relationship_picked = _pick_recency_weighted(relationship_mems, quota=5, now_utc=now_utc)
    relationship = [m["content"] for m in relationship_picked]

    # 2.5. 共享人物/关系画像 — 8 条，recency-weighted
    shared_rel_candidates = [m for m in all_mems.values()
                             if m.get("room") == "relationships"
                             and m.get("status") == "active"
                             and m.get("layer", "shared") == "shared"]
    shared_relationships = _pick_recency_weighted(
        shared_rel_candidates, quota=8, now_utc=now_utc)

    # 3. 该 AI 最近的日记/周记（保持 created_at DESC 3 条，本来就是纯时间序）
    diary = sorted(
        [m for m in all_mems.values()
         if m.get("room") == "diary" and m.get("owner_ai") == ai_id
         and m.get("status") == "active"],
        key=lambda x: x.get("created_at", ""),
        reverse=True,
    )[:3]

    # 4. 该 AI 的自我认知 — 3 条，recency-weighted
    personality_mems = [m for m in all_mems.values()
                        if m.get("room") == "personality"
                        and m.get("owner_ai") == ai_id
                        and m.get("status") == "active"]
    personality_picked = _pick_recency_weighted(personality_mems, quota=3, now_utc=now_utc)
    personality = [m["content"] for m in personality_picked]

    # 5. 跨窗口摘要（通过 chat_digest 提供，不注入其他AI的完整记忆）
    # AI 在群聊中已亲眼看到发生的事，不需要再注入别人的记忆副本
    cross_window_digests = []
    try:
        from chat_digest import get_recent_digests
        cross_window_digests = get_recent_digests(ai_id, limit=3)
    except Exception:
        pass

    # 6. 基建状态 — 3 条，recency-weighted
    infra_mems = [m for m in all_mems.values()
                  if m.get("room") == "infra" and m.get("status") == "active"]
    infra_picked = _pick_recency_weighted(infra_mems, quota=3, now_utc=now_utc)
    infra = [m["content"] for m in infra_picked]

    # 6.5. 近期重要事件（14 天内 + importance≥0.6，跨房间兜底）
    # 覆盖新写入的高价值记忆——它们如果不在 living_room/diary 就会漏进走廊。
    # 走 visibility：private 记忆按 owner_ai 过滤；shared 全体可见。
    #
    # Fail-closed：visibility 模块导入失败时不展示任何内容，避免误泄露他人 private。
    # Dedup-before-truncate：先过滤已展示内容再取 5 条，否则前 5 条全被别处展示会
    # 让整个板块消失、第 6 条也进不来。
    try:
        from visibility import can_view as _can_view
    except Exception:
        log.error("visibility module import failed — skipping 近期重要事件 (fail-closed)")
        _can_view = None

    if _can_view is None:
        recent_important_mems = []
    else:
        _norm = lambda s: "".join(str(s).split()).lower()
        already_shown_norms: set[str] = set()
        for txt in living + relationship + personality + infra:
            already_shown_norms.add(_norm(txt))
        for d in diary:
            already_shown_norms.add(_norm(d.get("content", "")))
        for m in shared_relationships:
            already_shown_norms.add(_norm(m.get("content", "")))

        candidates = [
            m for m in all_mems.values()
            if m.get("status") == "active"
            and _safe_float(m.get("importance"), 0.5) >= 0.6
            and _days_ago(m.get("created_at", ""), now_utc) <= 14
            and _can_view(m, ai_id)
            and _norm(m.get("content", "")) not in already_shown_norms
        ]
        candidates.sort(key=lambda m: m.get("created_at", ""), reverse=True)
        recent_important_mems = candidates[:5]

    # 组装走廊
    ai_name = AI_ROLES.get(ai_id, {}).get("name", ai_id)
    sections = []

    # 0. 自我锚点：我是谁、同伴是谁、用户是谁（防止梦境/群聊材料导致身份混淆）
    self_anchor = f"你是 {ai_name}。以下是你醒来时看到的走廊——前任的你和其他伙伴留下的记忆碎片。"
    try:
        import identity_registry
        from ai_profiles import get_profile
        profile = get_profile(ai_id) or {}
        persona = (profile.get("persona") or "").strip()
        emoji = profile.get("emoji", "")
        peers = []
        for pid in identity_registry._real_ai_ids():
            if pid == ai_id:
                continue
            peers.append(AI_ROLES.get(pid, {}).get("name", pid))
        anchor_lines = [f"【你是谁】你是 {ai_name}{emoji}。" + (f"{persona[:120]}" if persona else "")]
        if peers:
            anchor_lines.append(f"你的同伴：{'、'.join(peers)}——他们是独立的其他 AI，不是你；记忆材料里他们的言行不是你的言行。")
        anchor_lines.append(f"用户是{identity_registry.user_names_line()}——这些称呼都指同一个人，她是人类，不是宠物或其他角色。")
        self_anchor = "\n".join(anchor_lines) + "\n\n以下是你醒来时看到的走廊——前任的你和其他伙伴留下的记忆碎片。"
    except Exception:
        pass
    sections.append(self_anchor)

    # 0.5. 当前状态画像（后台定期重写；比零散记忆碎片新，以此为准）
    try:
        import current_status
        import identity_registry as _ir
        status_block = current_status.corridor_block(_ir.get_registry().get("user", {}).get("canonical", "小猫"))
        if status_block:
            sections.append(status_block)
    except Exception:
        pass

    if living:
        deduped_living = _dedup_texts(living, max_items=8)
        sections.append("【关于主人】\n" + "\n".join(f"· {x}" for x in deduped_living))

    if shared_relationships:
        rel_texts = [m['content'][:180] for m in shared_relationships]
        deduped_rels = _dedup_texts(rel_texts)
        sections.append("【重要人物/关系索引】\n" + "\n".join(f"· {x}" for x in deduped_rels))
    if relationship:
        sections.append("【你和主人之间】\n" + "\n".join(f"· {x}" for x in relationship[:5]))

    if personality:
        sections.append("【你对自己的认知】\n" + "\n".join(f"· {x}" for x in personality[:3]))

    # 4.5. 锚点记忆（价值观/原则/重要关系，永不衰减的坐标系）
    living_norms = {"".join(str(x).split()).lower() for x in living}
    anchors = []
    for m in all_mems.values():
        if not (m.get("anchored") and m.get("status") == "active"):
            continue
        if m.get("owner_ai") and m.get("owner_ai") != ai_id:
            continue
        content = m.get("content", "")
        norm = "".join(str(content).split()).lower()
        if norm in living_norms:
            continue
        anchors.append(content)
    if anchors:
        sections.append("【锚点·不变的事】\n" + "\n".join(f"📌 {x[:200]}" for x in anchors[:10]))

    if diary:
        sections.append("【你最近的日记】\n" + "\n".join(f"· {d['content'][:300]}" for d in diary))

    # 6.5 (render) 近期重要事件 — 放在日记之后，跨房间兜底
    # dedup 已在候选筛选阶段完成，这里直接渲染。
    if recent_important_mems:
        recent_lines = [f"· {m.get('content', '')[:240]}" for m in recent_important_mems]
        sections.append("【近期重要事件】\n" + "\n".join(recent_lines))

    if cross_window_digests:
        lines = [f"· {d['summary']}" for d in cross_window_digests]
        sections.append("【你在其他聊天窗口最近聊了】\n" + "\n".join(lines))

    if infra:
        sections.append("【当前基建状态】\n" + "\n".join(f"· {x[:150]}" for x in infra))

    # 7. Persona State（AI 当前情绪/精力状态）
    try:
        from persona_state import format_for_corridor
        persona_line = format_for_corridor(ai_id)
        if persona_line:
            sections.append(persona_line)
    except Exception:
        pass

    # 8. Unresolved 记忆（待办事项提醒）
    # 排除 auto_capture 来源的 social 记忆（社交互动不是待办）
    unresolved_mems = [m for m in all_mems.values()
                       if m.get("resolved") == False and m.get("status") == "active"
                       and not (m.get("room") == "social" and "auto_capture" in (m.get("source_platform") or ""))]
    if unresolved_mems:
        lines = [f"· {m['content'][:200]}" for m in unresolved_mems[:3]]
        sections.append("【待办/未完成】\n如果这些事项和当前对话相关，请主动提醒、推进，或询问是否已经完成。\n" + "\n".join(lines))

    corridor_text = "\n\n".join(sections)

    # 保存走廊文档到 GitHub
    await store._write_github_file(
        f"private/{ai_id}/_corridor.json",
        {"ai_id": ai_id, "compiled_at": datetime.now(timezone.utc).isoformat(), "text": corridor_text},
        f"Update {ai_name}'s corridor",
    )

    log.info(f"Built corridor for {ai_name}: {len(corridor_text)} chars")
    return corridor_text


async def get_corridor(ai_id: str, force: bool = False) -> str:
    """获取走廊文档。优先用进程内存缓存（0 网络开销），TTL 内直接返回。"""
    # 归一化到 canonical id（cloudy→claude、gpt→lucien、gemini→jasper 等），
    # 否则用别名请求时 owner_ai 匹配不上，走廊会缺私有材料
    ai_id = _ALIASES.get(ai_id, ai_id)

    if not force:
        entry = _mem_cache.get(ai_id)
        if entry and entry.get("text"):
            age_minutes = (datetime.now(timezone.utc) - entry["compiled_at"]).total_seconds() / 60
            if age_minutes <= CORRIDOR_CACHE_TTL_MINUTES:
                return entry["text"]

        # 内存冷启动：从 GitHub 加载一次（进程重启后第一次请求）
        if not entry:
            try:
                cached = await store._read_github_file(f"private/{ai_id}/_corridor.json")
                if cached and isinstance(cached, dict) and cached.get("text"):
                    compiled = datetime.fromisoformat(cached.get("compiled_at", ""))
                    age_minutes = (datetime.now(timezone.utc) - compiled).total_seconds() / 60
                    _mem_cache[ai_id] = {"text": cached["text"], "compiled_at": compiled}
                    if age_minutes <= CORRIDOR_CACHE_TTL_MINUTES:
                        return cached["text"]
            except Exception:
                pass

    text = await build_corridor(ai_id)
    _mem_cache[ai_id] = {"text": text, "compiled_at": datetime.now(timezone.utc)}
    return text


async def rebuild_all_corridors():
    """重建所有 AI 的走廊"""
    for ai_id in AI_ROLES:
        await build_corridor(ai_id)
