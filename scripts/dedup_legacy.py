# -*- coding: utf-8 -*-
"""
Phase 1.7 块 4 — 一次性去重历史存量记忆

对 Phase 1 之前写入的存量记忆做全量去重扫描：
  - 按 room 分组
  - 组内按 created_at 排序
  - 每条与后续 window_days 内的记忆两两比较 embedding cosine
  - 相似度 >= 阈值 → 候选合并对

Dry-run（默认）：输出 JSON 清单不改数据，给主人审。
Execute：走 MemoryMaintenanceDecision 引擎的 supersede/supplement/annotate 分流，
        写 maintenance_audit 表。**不 hard delete 原文**。

用法：
    # 本地冒烟（可以随便跑）
    ALLOW_DEFAULT_HUB_SECRET=1 python scripts/dedup_legacy.py --dry-run

    # VPS 上：先 dry-run，把 JSON 报告给主人审
    ALLOW_DEFAULT_HUB_SECRET=1 python scripts/dedup_legacy.py \\
        --dry-run --output data/dedup_dryrun_20260812.json

    # 审通过后执行
    ALLOW_DEFAULT_HUB_SECRET=1 python scripts/dedup_legacy.py \\
        --execute --max-pairs 50

    # 只处理某房间
    python scripts/dedup_legacy.py --dry-run --room living_room
"""
import argparse
import asyncio
import json
import os
import sys
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault("ALLOW_DEFAULT_HUB_SECRET", "1")

import database  # noqa: E402
from embedding import unpack_embedding, cosine_similarity  # noqa: E402


DEFAULT_ROOMS = [
    "living_room", "relationship", "relationships", "personality", "diary",
    "health", "career", "psychology", "learning", "preferences",
    "work_tasks", "infra", "infra_changelog", "social", "misc",
]


def _parse_ts(s: str) -> datetime | None:
    """Parse an ISO 8601 timestamp. Handles the 'Z' suffix for UTC (Python
    3.10 datetime.fromisoformat rejects it; 3.11+ accepts). Legacy rows
    stored with 'Z' would otherwise silently skip dedup on older interpreters.
    """
    if not s:
        return None
    try:
        # Normalize trailing 'Z' → '+00:00' for compat with 3.10.
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        t = datetime.fromisoformat(s)
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _load_room_mems(conn: sqlite3.Connection, room: str) -> list[dict]:
    rows = conn.execute(
        "SELECT id, content, room, created_at, importance, provenance_type, "
        "       fact_confidence, embedding "
        "FROM memories "
        "WHERE room = ? AND status = 'active' AND embedding IS NOT NULL "
        "ORDER BY created_at",
        (room,),
    ).fetchall()
    out = []
    for r in rows:
        out.append({
            "id": r[0], "content": r[1] or "", "room": r[2],
            "created_at": r[3] or "",
            "importance": float(r[4]) if r[4] is not None else 0.5,
            "provenance_type": r[5] or "",
            "fact_confidence": r[6],
            "_ts": _parse_ts(r[3] or ""),
            "_vec": unpack_embedding(r[7]) if r[7] else None,
        })
    return out


def _scan_room(
    mems: list[dict], sim_threshold: float, window_days: int,
) -> list[dict]:
    """Sliding-window pairwise cosine within window_days of each pivot."""
    pairs = []
    window = timedelta(days=window_days)
    n = len(mems)
    for i in range(n):
        m_i = mems[i]
        if m_i["_ts"] is None or m_i["_vec"] is None:
            continue
        for j in range(i + 1, n):
            m_j = mems[j]
            if m_j["_ts"] is None or m_j["_vec"] is None:
                continue
            if m_j["_ts"] - m_i["_ts"] > window:
                break  # sorted by created_at, further j's all outside window
            sim = cosine_similarity(m_i["_vec"], m_j["_vec"])
            if sim >= sim_threshold:
                pairs.append({
                    "room": m_i["room"],
                    "a_id": m_i["id"], "b_id": m_j["id"],
                    "similarity": round(sim, 4),
                    "a_content": m_i["content"][:200],
                    "b_content": m_j["content"][:200],
                    "a_created_at": m_i["created_at"],
                    "b_created_at": m_j["created_at"],
                    "a_importance": m_i["importance"],
                    "b_importance": m_j["importance"],
                    "a_provenance": m_i["provenance_type"],
                    "b_provenance": m_j["provenance_type"],
                })
    return pairs


# Only actions that actually REDUCE the number of active memories count as
# dedup. annotate/supplement just append comments — they don't dedup, and
# running them here would mis-attribute "dedup" work while leaving duplicates
# in place. Ceci should decide manually whether to merge those.
_DEDUP_ACTIONS = {"supersede", "update"}
_REPORT_ONLY_ACTIONS = {"supplement", "annotate", "correct", "no_change"}


async def _classify_and_execute(pairs: list[dict], max_pairs: int,
                                 report_only_out: list[dict]) -> dict:
    """For each pair, call analyzer.classify_relation and route through
    MemoryMaintenanceDecision. Only supersede/update actually run in DB;
    annotate/supplement/correct are collected into report_only_out for Ceci
    to review manually. Writes maintenance_audit for every executed action.
    """
    import analyzer
    import memory_ops

    counts = {"supersede": 0, "update": 0,
              "report_only": 0, "no_change": 0, "skip": 0, "error": 0}
    for idx, pair in enumerate(pairs[:max_pairs]):
        b = database.get_memory(pair["b_id"])
        a = database.get_memory(pair["a_id"])
        if not a or not b:
            counts["skip"] += 1
            continue
        try:
            rel = await analyzer.classify_relation(b["content"], [a])
            relations = rel.get("relations", [])
            if not relations:
                counts["no_change"] += 1
                continue
            r0 = relations[0]
            action = memory_ops._map_relation_to_action(
                r0, b.get("provenance_type", ""), a,
            )
            print(f"  [{idx+1}/{min(len(pairs), max_pairs)}] "
                  f"{pair['a_id']} × {pair['b_id']} → {action} "
                  f"(sim={pair['similarity']})")

            if action in _DEDUP_ACTIONS:
                # These actually dedup — safe to execute automatically.
                result = await memory_ops._execute_maintenance_action(
                    action, a, b["content"],
                    reason=f"legacy_dedup_script sim={pair['similarity']}",
                    source_ai="dedup_script",
                    provenance_type=b.get("provenance_type", ""),
                )
                if result:
                    counts[action] += 1
                else:
                    counts["skip"] += 1
            elif action in _REPORT_ONLY_ACTIONS:
                # Not real dedup — accumulate for the report, don't touch DB.
                report_only_out.append({
                    **pair, "proposed_action": action,
                    "reason": r0.get("reason", ""),
                })
                counts["report_only"] += 1
            elif action == "no_change":
                counts["no_change"] += 1
            else:
                counts["skip"] += 1
        except Exception as e:
            print(f"  ERROR on {pair['a_id']} × {pair['b_id']}: {e}")
            counts["error"] += 1
    return counts


def _summarize(pairs_by_room: dict[str, list[dict]]) -> dict:
    total = sum(len(p) for p in pairs_by_room.values())
    per_room = {r: len(p) for r, p in pairs_by_room.items() if p}
    return {"total_pairs": total, "per_room": per_room}


async def main() -> int:
    ap = argparse.ArgumentParser(description="Legacy memory dedup scan")
    ap.add_argument("--dry-run", action="store_true", default=True,
                    help="default: scan + report only, no data change")
    ap.add_argument("--execute", action="store_true",
                    help="actually run the merge/supersede via "
                         "MemoryMaintenanceDecision. Overrides --dry-run.")
    ap.add_argument("--room", default="",
                    help="only process this room (empty = all default rooms)")
    ap.add_argument("--sim-threshold", type=float, default=0.85,
                    help="cosine similarity cutoff (default 0.85)")
    ap.add_argument("--window-days", type=int, default=3,
                    help="only compare pairs within this many days apart "
                         "(default 3)")
    ap.add_argument("--max-pairs", type=int, default=200,
                    help="cap on pairs processed in --execute mode "
                         "(default 200, resume by rerunning)")
    ap.add_argument("--output", default="",
                    help="write dry-run report to this path (JSON)")
    args = ap.parse_args()

    is_execute = args.execute
    mode = "EXECUTE" if is_execute else "DRY-RUN"

    print(f"[dedup_legacy] mode={mode} sim>={args.sim_threshold} "
          f"window={args.window_days}d")
    if is_execute:
        print("[dedup_legacy] WARNING: will run maintenance actions "
              "(no hard deletes; audit logged)")

    # Ensure database is initialized so migrations & connection exist.
    await database.init_db(str(database.DB_PATH))

    conn = database._get_conn()

    rooms = [args.room] if args.room else DEFAULT_ROOMS
    pairs_by_room: dict[str, list[dict]] = {}
    for room in rooms:
        mems = _load_room_mems(conn, room)
        if not mems:
            continue
        print(f"  {room}: {len(mems)} active memories")
        pairs = _scan_room(mems, args.sim_threshold, args.window_days)
        if pairs:
            pairs_by_room[room] = pairs
            print(f"    → {len(pairs)} similar pairs")

    summary = _summarize(pairs_by_room)
    print(f"\n[dedup_legacy] scan complete: {summary}")

    report = {
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "sim_threshold": args.sim_threshold,
        "window_days": args.window_days,
        "summary": summary,
        "pairs_by_room": pairs_by_room,
    }

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"[dedup_legacy] report written to {args.output}")

    if not is_execute:
        print("[dedup_legacy] dry-run only — no changes made. "
              "Review the report and run with --execute to apply.")
        return 0

    # EXECUTE path
    flat_pairs = [p for room_pairs in pairs_by_room.values() for p in room_pairs]
    if not flat_pairs:
        print("[dedup_legacy] nothing to do")
        return 0

    print(f"\n[dedup_legacy] executing on {min(len(flat_pairs), args.max_pairs)} "
          f"pairs (of {len(flat_pairs)} total)...")
    report_only: list[dict] = []
    action_counts = await _classify_and_execute(
        flat_pairs, args.max_pairs, report_only)
    print(f"\n[dedup_legacy] execute complete: {action_counts}")

    # P0-4: pairs the classifier called annotate/supplement/correct are NOT
    # dedup — surface them to the operator so a human can decide what to do.
    if report_only:
        report_path = args.output or (
            f"data/dedup_report_only_"
            f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
        )
        Path(report_path).parent.mkdir(parents=True, exist_ok=True)
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump({
                "note": ("These pairs matched semantically but the classifier "
                         "suggested annotate/supplement/correct — actions that "
                         "do NOT reduce duplicate count. Review manually and "
                         "decide whether to merge, supersede, or leave alone."),
                "count": len(report_only),
                "pairs": report_only,
            }, f, ensure_ascii=False, indent=2)
        print(f"[dedup_legacy] {len(report_only)} pairs need manual review → "
              f"{report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
