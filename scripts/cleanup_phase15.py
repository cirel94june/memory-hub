"""
Phase 1.5 数据清理：provenance 回填 + info_type 补漏 + 空 room 修复。

安全可重跑。支持 --dry-run（执行所有 UPDATE 后 ROLLBACK）。

运行：cd memory-hub && python scripts/cleanup_phase15.py [--dry-run]
"""
import sqlite3
import sys
import json
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "memories.db"

VALID_INFO_TYPES = {"identity", "state", "event", "task", "reflection", "relationship", "fact"}

PROVENANCE_RULES = [
    ("source_platform LIKE 'auto_capture:%'", "ai_summary", "auto_capture → ai_summary"),
    ("source_platform = 'user_correction'", "user_correction", "user_correction platform"),
    ("source_platform = 'telegram' AND source_platform NOT LIKE 'auto_capture:%'", "user_statement", "telegram manual"),
    ("room IN ('diary', 'dreams') AND category = 'night_dream'", "dream", "night dream"),
    ("room IN ('diary', 'dreams') AND (category != 'night_dream' OR category IS NULL)", "ai_summary", "diary/dream entries"),
]

INFO_TYPE_FIXES = [
    ("room = 'dreams' AND info_type = 'fact'", "reflection", "dreams room fact → reflection"),
    ("info_type = '' OR info_type IS NULL", "fact", "empty info_type → fact"),
    ("info_type = 'preferences'", "identity", "preferences (invalid type) → identity"),
]


def cleanup(dry_run: bool = False):
    if not DB_PATH.exists():
        print(f"Database not found: {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("BEGIN")

    try:
        total = conn.execute("SELECT COUNT(*) FROM memories WHERE status = 'active'").fetchone()[0]
        print(f"Active memories: {total}")
        if dry_run:
            print("** DRY RUN — all UPDATEs executed, then ROLLBACK **")
        print()

        # ── 0. Room name unification ──
        print("=== Room name unification ===")
        count = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE status='active' AND room='relationships'"
        ).fetchone()[0]
        if count > 0:
            print(f"  relationships → relationship: {count} records")
            conn.execute("UPDATE memories SET room='relationship' WHERE room='relationships'")
        else:
            print("  (no 'relationships' room records to unify)")
        print()

        # ── 1. Provenance backfill ──
        empty_prov = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE status = 'active' AND (provenance_type = '' OR provenance_type IS NULL)"
        ).fetchone()[0]
        print(f"=== Provenance backfill ({empty_prov} empty) ===")

        backfill_total = 0
        for sql_cond, prov, desc in PROVENANCE_RULES:
            full_cond = f"status = 'active' AND (provenance_type = '' OR provenance_type IS NULL) AND ({sql_cond})"
            count = conn.execute(f"SELECT COUNT(*) FROM memories WHERE {full_cond}").fetchone()[0]
            if count > 0:
                print(f"  {desc}: {count} records → {prov}")
                backfill_total += count
                conn.execute(f"""
                    UPDATE memories SET provenance_type = ?,
                        tags = CASE
                            WHEN tags IS NULL OR tags = '[]' THEN '["_backfilled_provenance"]'
                            WHEN tags LIKE '%_backfilled_provenance%' THEN tags
                            ELSE REPLACE(tags, ']', ', "_backfilled_provenance"]')
                        END
                    WHERE {full_cond}
                """, (prov,))

        remaining = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE status = 'active' AND (provenance_type = '' OR provenance_type IS NULL)"
        ).fetchone()[0]
        if remaining > 0:
            print(f"  fallback (remaining unmatched): {remaining} records → ai_summary")
            conn.execute("""
                UPDATE memories SET provenance_type = 'ai_summary',
                    tags = CASE
                        WHEN tags IS NULL OR tags = '[]' THEN '["_backfilled_provenance"]'
                        WHEN tags LIKE '%_backfilled_provenance%' THEN tags
                        ELSE REPLACE(tags, ']', ', "_backfilled_provenance"]')
                    END
                WHERE status = 'active' AND (provenance_type = '' OR provenance_type IS NULL)
            """)
            backfill_total += remaining

        print(f"  Total provenance backfill: {backfill_total}")

        # ── 2. info_type fixes ──
        print(f"\n=== info_type fixes ===")
        info_total = 0
        for sql_cond, info_type, desc in INFO_TYPE_FIXES:
            full_cond = f"status = 'active' AND ({sql_cond})"
            count = conn.execute(f"SELECT COUNT(*) FROM memories WHERE {full_cond}").fetchone()[0]
            if count > 0:
                print(f"  {desc}: {count} records → {info_type}")
                info_total += count
                conn.execute(f"UPDATE memories SET info_type = ? WHERE {full_cond}", (info_type,))

        invalid = conn.execute(
            f"SELECT info_type, COUNT(*) FROM memories WHERE status = 'active' AND info_type NOT IN ({','.join('?' * len(VALID_INFO_TYPES))}) GROUP BY info_type",
            list(VALID_INFO_TYPES),
        ).fetchall()
        if invalid:
            print(f"  WARNING: remaining invalid info_types: {[(r[0], r[1]) for r in invalid]}")
        print(f"  Total info_type fixes: {info_total}")

        # ── 3. Empty room fix ──
        print(f"\n=== Empty room fix ===")
        empty_rooms = conn.execute(
            "SELECT id, content, source_ai FROM memories WHERE status = 'active' AND (room = '' OR room IS NULL)"
        ).fetchall()
        if empty_rooms:
            for r in empty_rooms:
                print(f"  {r['id']}: \"{r['content'][:60]}\" → living_room")
                conn.execute("UPDATE memories SET room = 'living_room' WHERE id = ?", (r['id'],))
        else:
            print("  No empty rooms found")

        # ── Final stats ──
        print(f"\n=== Post-cleanup stats ===")
        print("Provenance distribution:")
        for r in conn.execute(
            "SELECT provenance_type, COUNT(*) as c FROM memories WHERE status = 'active' GROUP BY provenance_type ORDER BY c DESC"
        ):
            print(f"  {r[0] or '(empty)'}: {r[1]}")

        print("\ninfo_type distribution:")
        for r in conn.execute(
            "SELECT info_type, COUNT(*) as c FROM memories WHERE status = 'active' GROUP BY info_type ORDER BY c DESC"
        ):
            print(f"  {r[0] or '(empty)'}: {r[1]}")

        if dry_run:
            conn.rollback()
            print("\n** ROLLBACK — no changes persisted (dry-run) **")
        else:
            conn.commit()
            print("\nCOMMIT — all changes persisted.")

    except Exception as e:
        conn.rollback()
        print(f"\nERROR: {e} — ROLLBACK performed, no changes persisted.")
        raise
    finally:
        conn.close()

    print("Done.")


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    cleanup(dry_run=dry)
