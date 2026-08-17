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


# Old _classify_and_execute removed (H5): replaced by _classify_plan +
# _execute_plan below to guarantee the operator's approval matches what
# actually runs. See docstrings of those functions.


def _summarize(pairs_by_room: dict[str, list[dict]]) -> dict:
    total = sum(len(p) for p in pairs_by_room.values())
    per_room = {r: len(p) for r, p in pairs_by_room.items() if p}
    return {"total_pairs": total, "per_room": per_room}


async def _classify_plan(flat_pairs: list[dict], max_pairs: int) -> dict:
    """H5 plan phase: call LLM classifier for each pair and build an
    IMMUTABLE plan. The plan records the exact action, target snapshot
    (updated_at, status) at scan time, and the reason — so --execute can
    verify preconditions haven't drifted before touching data."""
    import analyzer
    import memory_ops

    executable: list[dict] = []
    report_only: list[dict] = []
    for idx, pair in enumerate(flat_pairs[:max_pairs]):
        b = database.get_memory(pair["b_id"])
        a = database.get_memory(pair["a_id"])
        if not a or not b:
            continue
        try:
            rel = await analyzer.classify_relation(b["content"], [a])
            relations = rel.get("relations", [])
            if not relations:
                continue
            r0 = relations[0]
            action = memory_ops._map_relation_to_action(
                r0, b.get("provenance_type", ""), a,
            )
            print(f"  [{idx+1}/{min(len(flat_pairs), max_pairs)}] "
                  f"{a['id']} × {b['id']} → {action} "
                  f"(sim={pair['similarity']})")

            entry = {
                **pair, "action": action,
                "reason": r0.get("reason", "")[:200],
                "a_updated_at_snapshot": a.get("updated_at", ""),
                "b_updated_at_snapshot": b.get("updated_at", ""),
                "a_status_snapshot": a.get("status", ""),
                "b_status_snapshot": b.get("status", ""),
                "b_provenance": b.get("provenance_type", ""),
            }
            if action in _DEDUP_ACTIONS:
                executable.append(entry)
            elif action in _REPORT_ONLY_ACTIONS:
                report_only.append(entry)
        except Exception as e:
            print(f"  ERROR on {pair['a_id']} × {pair['b_id']}: {e}")

    return {"executable": executable, "report_only": report_only}


async def _execute_plan(plan_entries: list[dict]) -> dict:
    """H5 execute phase: consume the immutable plan. For each entry, verify
    that both memories are still active AND their updated_at matches the
    snapshot — else skip that entry (someone else touched the row). NEVER
    calls the LLM classifier; the action was decided at plan time.

    Round-9 (post-first-run): counters split so operators can tell WHY an
    entry was skipped without guessing:
      - applied           → maintenance action ran
      - skipped_drift     → status/updated_at changed since plan
      - skipped_missing   → memory row deleted since plan
      - blocked_by_guard  → provenance guard rejected supersede/update
                            (e.g. ai_summary cannot supersede ai_summary
                            without user-level authorization)
      - unexpected_none   → guard passed but action returned None anyway
                            (indicates a bug or a downstream MaintenanceDrift
                            we didn't catch)
      - error             → uncaught exception
    """
    import memory_ops
    counts = {"applied": 0, "skipped_drift": 0, "skipped_missing": 0,
              "blocked_by_guard": 0, "unexpected_none": 0, "error": 0}
    for entry in plan_entries:
        action = entry["action"]
        if action not in _DEDUP_ACTIONS:
            # Plan entries with report-only actions shouldn't reach here
            # (they're filtered out at plan-write time), but guard anyway.
            counts["skipped_drift"] += 1
            continue

        a = database.get_memory(entry["a_id"])
        b = database.get_memory(entry["b_id"])
        if not a or not b:
            counts["skipped_missing"] += 1
            continue

        # Precondition: both rows still active, updated_at unchanged since scan
        if (a.get("status") != "active" or b.get("status") != "active"
                or a.get("updated_at", "") != entry.get("a_updated_at_snapshot")
                or b.get("updated_at", "") != entry.get("b_updated_at_snapshot")):
            counts["skipped_drift"] += 1
            print(f"  SKIP {entry['a_id']} × {entry['b_id']}: "
                  f"drift detected since scan")
            continue

        # Round-9: provenance guard pre-check so blocked pairs are reported
        # accurately instead of being lumped into skipped_drift. This
        # catches the common case where two ai_summary rows can't
        # supersede each other without user-level authorization.
        provenance_for_guard = entry.get("b_provenance", "")
        if not memory_ops._can_supersede(provenance_for_guard, a):
            counts["blocked_by_guard"] += 1
            print(f"  BLOCKED {entry['a_id']} × {entry['b_id']}: "
                  f"provenance guard rejected ({provenance_for_guard!r} "
                  f"cannot supersede {a.get('provenance_type')!r}). "
                  f"Route through user-level review or manual override.")
            continue

        try:
            # H3 round-6: B is load-bearing for the supersede decision even
            # though we only mutate A. Pass B's snapshot to the atomic
            # helper so the tx rolls back if B drifted since plan time.
            result = await memory_ops._execute_maintenance_action(
                action, a, b["content"],
                reason=f"legacy_dedup_script: {entry['reason']}",
                source_ai="dedup_script",
                provenance_type=provenance_for_guard,
                companion_expected_rows=[{
                    "id": entry["b_id"],
                    "status": entry.get("b_status_snapshot", ""),
                    "updated_at": entry.get("b_updated_at_snapshot", ""),
                }],
            )
            if result:
                counts["applied"] += 1
            else:
                # Guard already passed our pre-check so a None here means
                # something else (e.g. MaintenanceDrift swallowed inside
                # _execute_maintenance_action). Report distinctly for
                # observability instead of pretending it was drift.
                counts["unexpected_none"] += 1
                print(f"  UNEXPECTED_NONE {entry['a_id']} × {entry['b_id']}: "
                      f"guard passed but action returned None")
        except Exception as e:
            print(f"  ERROR on {entry['a_id']} × {entry['b_id']}: {e}")
            counts["error"] += 1
    return counts


async def main() -> int:
    ap = argparse.ArgumentParser(description="Legacy memory dedup scan")
    ap.add_argument("--dry-run", action="store_true", default=True,
                    help="default: scan + LLM classify + write plan; no DB changes")
    ap.add_argument("--execute", action="store_true",
                    help="apply a pre-generated plan (requires --plan-file)")
    ap.add_argument("--plan-file", default="",
                    help="path to a plan JSON produced by --dry-run "
                         "(required for --execute)")
    ap.add_argument("--room", default="",
                    help="only process this room (empty = all default rooms)")
    ap.add_argument("--sim-threshold", type=float, default=0.85,
                    help="cosine similarity cutoff (default 0.85)")
    ap.add_argument("--window-days", type=int, default=3,
                    help="only compare pairs within this many days apart "
                         "(default 3)")
    ap.add_argument("--max-pairs", type=int, default=200,
                    help="cap on pairs classified per run (default 200)")
    ap.add_argument("--output", default="",
                    help="write scan-phase plan to this path (JSON)")
    ap.add_argument("--db-path", default="",
                    help="M2: override DB_PATH (for tests / dry-run on a copy)")
    args = ap.parse_args()

    if args.db_path:
        # M2: honor override BEFORE init_db so migrations touch the right file
        database.DB_PATH = Path(args.db_path)

    is_execute = args.execute
    mode = "EXECUTE" if is_execute else "PLAN"

    print(f"[dedup_legacy] mode={mode} db={database.DB_PATH} "
          f"sim>={args.sim_threshold} window={args.window_days}d")

    # Ensure database is initialized so migrations & connection exist.
    await database.init_db(str(database.DB_PATH))
    conn = database._get_conn()

    if is_execute:
        # H5: execute consumes a pre-generated plan, does NOT call LLM.
        if not args.plan_file:
            print("[dedup_legacy] --execute requires --plan-file (produced by "
                  "an earlier plan run)")
            return 2
        with open(args.plan_file, encoding="utf-8") as f:
            plan = json.load(f)
        executable = plan.get("executable", [])
        print(f"[dedup_legacy] applying {len(executable)} planned actions "
              f"(pre-classified, no LLM call)...")
        counts = await _execute_plan(executable)
        print(f"[dedup_legacy] execute complete: {counts}")
        return 0

    # PLAN path: scan → classify → write immutable plan
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

    flat_pairs = [p for room_pairs in pairs_by_room.values() for p in room_pairs]
    plan_result = await _classify_plan(flat_pairs, args.max_pairs) \
        if flat_pairs else {"executable": [], "report_only": []}

    plan_doc = {
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "db_path": str(database.DB_PATH),
        "sim_threshold": args.sim_threshold,
        "window_days": args.window_days,
        "summary": summary,
        "executable": plan_result["executable"],
        "report_only": plan_result["report_only"],
    }

    out_path = args.output or (
        f"data/dedup_plan_"
        f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    )
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(plan_doc, f, ensure_ascii=False, indent=2)
    print(f"[dedup_legacy] plan written to {out_path}")
    print("[dedup_legacy] plan-only run — no changes made. "
          f"Review then: python scripts/dedup_legacy.py --execute --plan-file {out_path}")

    # Kept-for-backcompat: emit a separate report_only listing too
    if plan_result["report_only"]:
        report_path = (out_path.replace(".json", "_report_only.json")
                       if out_path.endswith(".json") else out_path + ".report_only.json")
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump({
                "note": ("These pairs matched semantically but the classifier "
                         "suggested annotate/supplement/correct — actions that "
                         "do NOT reduce duplicate count. Review manually and "
                         "decide whether to merge, supersede, or leave alone."),
                "count": len(plan_result["report_only"]),
                "pairs": plan_result["report_only"],
            }, f, ensure_ascii=False, indent=2)
        print(f"[dedup_legacy] {len(plan_result['report_only'])} pairs need "
              f"manual review → {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
