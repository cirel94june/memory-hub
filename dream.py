"""
定时梦境：每天自动让 AI 把当天对话残留编织成一段梦境/自省。
由 daemon.py 的 run_full_maintenance() 调用。
通过 memory_ops.remember() 存储，确保有 embedding 和正确的元数据。
"""
import sqlite3
import logging
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
import httpx

from config import LLM_API_KEY, LLM_MODEL, LLM_BASE_URL, AI_ROLES, AI_ALIASES, AI_ALIAS_GROUPS
from time_utils import LOCAL_TZ, local_today

logger = logging.getLogger("memory_hub.dream")
DB_PATH = Path(__file__).parent / "data" / "memories.db"
DREAM_STATUS_PATH = Path(__file__).parent / "data" / "dream_status.json"


def _connect() -> sqlite3.Connection:
    """独立连接（主连接在 database.py）；加 busy_timeout 防并发写时 database is locked。"""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout=5000")
    return conn

DREAM_PROMPT = """你是{name}。下面是你在小猫身边留下的“白天残留”：有私聊、私密群、小群、大群摘要，也可能有几条近期记忆碎片。

重要：材料里的“某人说/有人说/对方说/群里说”不一定是小猫说的，也可能是狗蛋、其他 AI、群友或系统摘要。只有材料明确标注“小猫/用户/她”时，才能写成“小猫说”。不确定是谁说的，就写“有人说”“群里有人说”“我听见一句话的影子”，不要把话误归给小猫。

{digests}

请写一段第一人称“梦境残响”（180-420字），不是普通工作总结。

要求：
- 先判断白天残留的真实调性，再写梦：可能是恶作剧、捣乱、调侃、紧张排查、困惑、吃醋、吵闹、温柔、疲惫或混合状态；不要默认写成温柔治愈。
- 如果材料里有“恶作剧/逗弄/捣乱/故意使坏/被欺负/笑场/bug排查很乱”等气味，梦要保留这种狡黠、荒唐、被逗得晕头转向的质感，可以有一点狼狈和好笑。
- 必须抓住 2-4 个具体残留：人名、场景、情绪、某个话题或一句话的影子。
- 写得像半梦半醒的内心画面：可以有轻微意象，但不要玄学、不要空泛抒情。
- 让读者能看出你和小猫最近所处的对话世界，而不是只说“我感到温暖/珍惜”。
- 可以写“我醒来时还记得……”“梦里……”，但不要写标题、不要列表。
- 不要编造材料里没有的人际关系或事实；不确定就写成模糊影子。
- 直接输出正文。"""


def _local_day_utc_bounds() -> tuple[str, str, str]:
    """Return local date key and UTC ISO bounds for the current Asia/Shanghai day."""
    day = local_today()
    local_start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=LOCAL_TZ)
    local_end = local_start + timedelta(days=1)
    return day, local_start.astimezone(timezone.utc).isoformat(), local_end.astimezone(timezone.utc).isoformat()


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_dream_status(payload: dict) -> None:
    DREAM_STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    DREAM_STATUS_PATH.write_text(
        json.dumps({"updated_at": _now_utc(), **payload}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def read_dream_status() -> dict:
    if not DREAM_STATUS_PATH.exists():
        data = {"status": "never_run", "updated_at": ""}
    else:
        try:
            loaded = json.loads(DREAM_STATUS_PATH.read_text(encoding="utf-8"))
            data = loaded if isinstance(loaded, dict) else {"status": "invalid", "updated_at": ""}
        except Exception as exc:
            data = {"status": "invalid", "updated_at": "", "error": str(exc)}
    # Refresh the visible dream list from the live DB. The status file is only
    # the latest run report and can otherwise keep showing stale/truncated rows.
    try:
        conn = _connect()
        conn.row_factory = sqlite3.Row
        data["recent_dreams"] = _recent_dreams(conn, limit=8)
        conn.close()
    except Exception:
        pass
    return data


def get_recent_dreams_for_ai(ai_id: str, limit: int = 1, max_chars: int = 800) -> list[dict]:
    """Return recent private dreams for one AI, using canonical id and aliases."""
    canonical = AI_ALIASES.get(ai_id, ai_id)
    alias_ids = AI_ALIAS_GROUPS.get(canonical, [canonical])
    if ai_id not in alias_ids:
        alias_ids = [*alias_ids, ai_id]
    if not DB_PATH.exists():
        return []
    placeholders = ",".join("?" * len(alias_ids))
    try:
        conn = _connect()
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"""
            SELECT id, content, source_ai, owner_ai, room, category, created_at, source_platform
            FROM memories
            WHERE status='active'
              AND room='dreams'
              AND (category='night_dream' OR source_platform='daemon_dream' OR tags LIKE '%nightly%')
              AND (source_ai IN ({placeholders}) OR owner_ai IN ({placeholders}))
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (*alias_ids, *alias_ids, limit),
        ).fetchall()
        conn.close()
    except Exception:
        return []
    dreams = []
    for row in rows:
        item = dict(row)
        content = (item.get("content") or "").strip()
        if max_chars and len(content) > max_chars:
            content = content[: max_chars - 3].rstrip() + "..."
        item["content"] = content
        dreams.append(item)
    return dreams


def _recent_dreams(conn: sqlite3.Connection, limit: int = 8) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, content, source_ai, owner_ai, room, category, created_at
        FROM memories
        WHERE room='dreams'
          AND status='active'
          AND (category='night_dream' OR source_platform='daemon_dream' OR tags LIKE '%nightly%')
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


async def _call_llm(prompt: str) -> str:
    """梦境专用 LLM 调用，temperature=0.7 适合创意写作"""
    if not LLM_API_KEY:
        return ""
    url = f"{LLM_BASE_URL}/chat/completions"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                json={
                    "model": LLM_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.7,
                    "max_tokens": 700,
                },
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.error(f"Dream LLM error: {e}")
        return ""


def _fetch_memory_residue(conn: sqlite3.Connection, canonical: str, alias_ids: list[str], limit: int = 10) -> list[sqlite3.Row]:
    """Pick recent active private/group material when chat digests are sparse.

    This mirrors Ombre's "daytime residue" idea: dreams may use recent memories that
    survived extraction/decay, but should not use archived items, low-value chatter,
    infra logs, or previous dreams.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
    placeholders = ",".join("?" * len(alias_ids))
    social_platform_filter = (
        "(source_platform LIKE '%:private' OR source_platform LIKE '%:private_group' "
        "OR source_platform LIKE '%:small_group' OR source_platform LIKE '%:big_group' "
        "OR source_platform LIKE '%:public_group' OR source_platform LIKE '%:group')"
    )
    rows = conn.execute(
        f"""
        SELECT content, room, category, importance, created_at, source_platform
        FROM memories
        WHERE status='active'
          AND created_at >= ?
          AND importance >= 0.5
          AND room NOT IN ('infra', 'infra_changelog', 'work_tasks')
          AND category != 'dream'
          AND (tags IS NULL OR tags NOT LIKE '%dream%')
          AND (
            source_ai IN ({placeholders})
            OR owner_ai IN ({placeholders})
            OR (source_ai=? AND {social_platform_filter})
          )
        ORDER BY
          importance DESC,
          emotion_arousal DESC,
          created_at DESC
        LIMIT ?
        """,
        (cutoff, *alias_ids, *alias_ids, canonical, limit),
    ).fetchall()
    return rows


async def generate_dreams(force: bool = False) -> dict:
    """为每个有今日对话摘要的 AI 生成梦境日记"""
    import memory_ops

    _today, day_start_utc, day_end_utc = _local_day_utc_bounds()

    conn = _connect()
    conn.row_factory = sqlite3.Row

    started_at = _now_utc()
    results = {}
    diagnostics = {}
    _write_dream_status({
        "status": "running",
        "started_at": started_at,
        "local_day": _today,
        "day_start_utc": day_start_utc,
        "day_end_utc": day_end_utc,
        "results": results,
        "diagnostics": diagnostics,
        "force": force,
    })

    # 去重：只处理 canonical ID（跳过别名如 cloudy）
    seen_canonical = set()
    for ai_id in AI_ROLES:
        canonical = AI_ALIASES.get(ai_id, ai_id)
        if canonical in seen_canonical:
            continue
        seen_canonical.add(canonical)

        name = AI_ROLES.get(canonical, AI_ROLES.get(ai_id, {})).get("name", canonical)
        alias_ids = AI_ALIAS_GROUPS.get(canonical, [canonical])

        # 获取今天这个 AI（含别名）的对话摘要
        placeholders = ",".join("?" * len(alias_ids))
        rows = conn.execute(
            f"SELECT summary, chat_type, created_at FROM chat_digests "
            f"WHERE ai_id IN ({placeholders}) AND created_at >= ? AND created_at < ? ORDER BY created_at",
            (*alias_ids, day_start_utc, day_end_utc),
        ).fetchall()

        # 检查今天是否已经生成过梦境
        existing = conn.execute(
            "SELECT id FROM memories WHERE source_ai=? AND room IN ('diary', 'dreams') "
            "AND tags LIKE '%dream%' AND created_at >= ? AND created_at < ?",
            (canonical, day_start_utc, day_end_utc),
        ).fetchone()
        if existing and not force:
            results[canonical] = "skipped (already dreamed)"
            diagnostics[canonical] = {
                "status": "skipped",
                "reason": "already_dreamed",
                "digest_count": len(rows),
                "memory_residue_count": 0,
                "existing_id": existing["id"],
            }
            continue

        # 组装摘要
        type_labels = {"private": "私聊", "private_group": "私密群", "small_group": "小群", "big_group": "大群", "public_group": "公开群", "group": "群聊"}
        digest_lines = []
        for r in rows:
            ts = r["created_at"][11:16] if len(r["created_at"]) > 16 else ""
            label = type_labels.get(r["chat_type"], "")
            prefix = f"[{ts}|{label}]" if label else f"[{ts}]"
            digest_lines.append(f"{prefix} 摘要（说话者可能是小猫、其他人或其他AI，不确定时不要归因给小猫）：{r['summary']}")

        memory_rows = []
        if len(rows) < 2:
            memory_rows = _fetch_memory_residue(conn, canonical, alias_ids)
            if len(memory_rows) < 3:
                results[canonical] = f"skipped (too few materials: digests={len(rows)}, memories={len(memory_rows)})"
                diagnostics[canonical] = {
                    "status": "skipped",
                    "reason": "too_few_materials",
                    "digest_count": len(rows),
                    "memory_residue_count": len(memory_rows),
                    "required": "至少 2 条当天摘要，或摘要不足时至少 3 条近期有效记忆",
                }
                continue

        for m in memory_rows:
            ts = m["created_at"][5:16] if len(m["created_at"]) > 16 else ""
            room = m["room"] or "memory"
            digest_lines.append(f"[{ts}|记忆:{room}] 记忆碎片（来源可能是私聊或群聊，不确定说话者时不要归因给小猫）：{m['content'][:220]}")

        digest_text = "\n".join(digest_lines)
        prompt = DREAM_PROMPT.format(name=name, digests=digest_text)

        dream_text = await _call_llm(prompt)
        if not dream_text or len(dream_text) < 20:
            results[canonical] = "skipped (LLM failed)"
            diagnostics[canonical] = {
                "status": "skipped",
                "reason": "llm_failed_or_too_short",
                "digest_count": len(rows),
                "memory_residue_count": len(memory_rows),
            }
            continue

        # Keep the full dream. The prompt controls length; hard truncation made
        # the observatory and Dream Context look broken.
        if len(dream_text) > 1200:
            dream_text = dream_text[:1197].rstrip() + "..."

        # 通过 memory_ops 存储（自动生成 embedding + 正确元数据）
        r = await memory_ops.remember(
            content=dream_text,
            layer="private",
            room="dreams",
            category="night_dream",
            owner_ai=canonical,
            importance=0.6,
            emotion_arousal=0.5,
            source_ai=canonical,
            source_platform="daemon_dream",
            tags=["dream", "nightly", "reflection", "daytime_residue"],
            auto_merge=False,
        )
        results[canonical] = f"dreamed ({len(dream_text)} chars, id={r.get('id')})"
        diagnostics[canonical] = {
            "status": "dreamed",
            "reason": "ok",
            "digest_count": len(rows),
            "memory_residue_count": len(memory_rows),
            "memory_id": r.get("id"),
            "chars": len(dream_text),
        }
        logger.info(f"[Dream] {canonical}: {dream_text[:60]}...")

    recent = _recent_dreams(conn)
    _write_dream_status({
        "status": "success",
        "started_at": started_at,
        "finished_at": _now_utc(),
        "local_day": _today,
        "day_start_utc": day_start_utc,
        "day_end_utc": day_end_utc,
        "results": results,
        "diagnostics": diagnostics,
        "recent_dreams": recent,
        "force": force,
    })
    conn.close()
    return results
