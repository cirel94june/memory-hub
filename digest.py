"""
定时消化：对每个 AI 近期新增的零散记忆做关联归纳。
由 daemon.py 的 run_full_maintenance() 调用（在做梦之后）。

与做梦的区别：
- 做梦 = 对当天对话的感性联想（dream.py）
- 消化 = 对近期记忆的结构化整理（本模块）

设计原则（来自 Ombre Brain）：只给素材+提示，不替 AI 下人格/情感结论。
"""
import logging
from datetime import datetime, timezone, timedelta

import httpx

from config import LLM_API_KEY, LLM_MODEL, LLM_BASE_URL, AI_ROLES, AI_ALIASES, AI_ALIAS_GROUPS
import database

logger = logging.getLogger("memory_hub.digest")

DIGEST_THRESHOLD = 30
DIGEST_COOLDOWN_HOURS = 24

DIGEST_PROMPT = """你是记忆整理助手。下面是{name}最近新增的一批记忆条目。

请做以下整理（120-200字）：
1. 这些记忆之间有什么关联？有没有共同的主题或线索？
2. 有没有互相矛盾或重复的内容？
3. 有没有值得注意的变化趋势？

规则：
- 只做客观归纳，不要替{name}下人格结论或情感判断
- 不要编造记忆里没有的信息
- 用"这些记忆显示……"而不是"你觉得……"
- 如果记忆太零散没有明显关联，就说"近期记忆较零散，暂无明显关联"

记忆条目：
{memories}
"""


async def _call_llm(prompt: str) -> tuple[str, str | None]:
    """调用 LLM。返回 (content, error)。成功时 error=None，失败时 content=""。"""
    if not LLM_API_KEY:
        return "", "LLM_API_KEY not configured"
    url = f"{LLM_BASE_URL}/chat/completions"
    headers = {"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"}
    body = {
        "model": LLM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 1024,
    }
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(url, json=body, headers=headers)
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"], None
    except Exception as e:
        logger.error(f"Digest LLM error: {e}")
        return "", str(e)


def _get_ai_ids(canonical: str) -> list[str]:
    """获取一个 AI 的所有别名 ID。"""
    return AI_ALIAS_GROUPS.get(canonical, [canonical])


def _last_digest_time(ai_id: str) -> str | None:
    """查询该 AI 最近一次 digest 的时间。"""
    conn = database._get_read_conn()
    ai_ids = _get_ai_ids(ai_id)
    placeholders = ",".join("?" for _ in ai_ids)
    row = conn.execute(
        f"SELECT MAX(created_at) FROM memories "
        f"WHERE source_ai IN ({placeholders}) AND room='dreams' "
        f"AND category='digest' AND status='active'",
        ai_ids,
    ).fetchone()
    return row[0] if row and row[0] else None


def _count_new_memories_since(ai_id: str, since: str) -> int:
    """统计某个 AI 自某时间以来的新增记忆数（排除 digest 本身）。"""
    conn = database._get_read_conn()
    ai_ids = _get_ai_ids(ai_id)
    placeholders = ",".join("?" for _ in ai_ids)
    row = conn.execute(
        f"SELECT COUNT(*) FROM memories "
        f"WHERE source_ai IN ({placeholders}) AND status='active' "
        f"AND created_at > ? AND category != 'digest'",
        (*ai_ids, since),
    ).fetchone()
    return row[0] if row else 0


def _fetch_recent_memories(ai_id: str, since: str, limit: int = 50) -> list[dict]:
    """拉取某 AI 自某时间以来的新增记忆。"""
    conn = database._get_read_conn()
    ai_ids = _get_ai_ids(ai_id)
    placeholders = ",".join("?" for _ in ai_ids)
    rows = conn.execute(
        f"SELECT id, content, room, category, created_at FROM memories "
        f"WHERE source_ai IN ({placeholders}) AND status='active' "
        f"AND created_at > ? AND category != 'digest' "
        f"ORDER BY created_at DESC LIMIT ?",
        (*ai_ids, since, limit),
    ).fetchall()
    return [{"id": r[0], "content": r[1], "room": r[2], "category": r[3], "created_at": r[4]} for r in rows]


def _should_digest(ai_id: str) -> tuple[bool, str]:
    """判断是否需要消化。返回 (should, since_time)。

    M1 fix: 先数记忆再看冷却。>=30 条无视冷却立即触发，
    <30 条才检查是否过了冷却期+至少 5 条。
    """
    last = _last_digest_time(ai_id)
    now = datetime.now(timezone.utc)
    since = last or (now - timedelta(hours=DIGEST_COOLDOWN_HOURS)).isoformat()
    count = _count_new_memories_since(ai_id, since)

    if count >= DIGEST_THRESHOLD:
        return True, since

    cooldown_cutoff = (now - timedelta(hours=DIGEST_COOLDOWN_HOURS)).isoformat()
    if (not last or last < cooldown_cutoff) and count >= 5:
        return True, since

    return False, ""


async def digest_for_ai(ai_id: str) -> dict | None:
    """对单个 AI 执行消化。返回消化结果或 None（无需消化/失败）。"""
    should, since = _should_digest(ai_id)
    if not should:
        return {"ai_id": ai_id, "status": "not_due"}

    memories = _fetch_recent_memories(ai_id, since)
    if len(memories) < 5:
        return {"ai_id": ai_id, "status": "not_due"}

    name = AI_ROLES.get(ai_id, {}).get("name", ai_id)
    mem_text = "\n".join(
        f"- [{m['room']}] {m['content'][:200]}"
        for m in memories
    )

    prompt = DIGEST_PROMPT.format(name=name, memories=mem_text)
    result, llm_err = await _call_llm(prompt)
    if llm_err or len(result.strip()) < 20:
        logger.warning(f"Digest LLM failed for {ai_id}: {llm_err or 'response too short'}")
        return {"ai_id": ai_id, "status": "failed", "error": llm_err or "response too short"}

    import memory_ops
    mem_result = await memory_ops.remember(
        content=f"[消化] {result.strip()}",
        room="dreams",
        category="digest",
        importance=0.5,
        source_ai=ai_id,
        source_platform="daemon:digest",
        layer="private",
        owner_ai=ai_id,
        auto_merge=False,
        provenance_type="digest",
        info_type="reflection",
        auto_analyze=False,
    )

    logger.info(f"Digest for {ai_id}: {len(memories)} memories → {len(result)} chars")
    return {
        "ai_id": ai_id,
        "status": "success",
        "memories_digested": len(memories),
        "digest_length": len(result),
        "memory_id": mem_result.get("id", ""),
    }


async def run_digest() -> dict:
    """对所有 AI 角色执行消化。由 daemon 调用。"""
    results = {}
    canonical_ids = set()
    for ai_id in AI_ROLES:
        canonical = AI_ALIASES.get(ai_id, ai_id)
        if canonical in canonical_ids:
            continue
        canonical_ids.add(canonical)
        try:
            result = await digest_for_ai(canonical)
            results[canonical] = result
        except Exception as e:
            logger.warning(f"Digest for {canonical} failed: {e}")
            results[canonical] = {"ai_id": canonical, "status": "failed", "error": str(e)}

    succeeded = sum(1 for r in results.values() if r.get("status") == "success")
    failed = sum(1 for r in results.values() if r.get("status") == "failed")
    return {"digested": succeeded, "failed": failed, "details": results}
