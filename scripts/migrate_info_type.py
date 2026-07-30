"""
历史数据迁移：给所有 info_type='fact'（默认值）的记忆按规则分类。

规则优先，安全可重跑。只更新 info_type='fact' 的记忆。

运行：cd memory-hub && python scripts/migrate_info_type.py
"""
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "memories.db"

RULES = [
    # (条件SQL片段, info_type, 描述)
    ("room = 'personality'", "identity", "personality room → identity"),
    ("room = 'preferences'", "identity", "preferences room → identity"),
    ("room IN ('relationship', 'relationships')", "relationship", "relationship room → relationship"),
    ("room IN ('diary', 'dreams') OR category = 'night_dream'", "reflection", "diary/dreams → reflection"),
    ("room = 'work_tasks'", "task", "work_tasks room → task"),
    ("resolved IS NOT NULL", "task", "has resolved field → task"),
    ("room IN ('health', 'psychology') AND importance >= 0.7", "identity", "high-importance health/psychology → identity"),
    ("room = 'social' AND category IN ('group_dynamic', 'social_event')", "event", "social events → event"),
]


def migrate():
    if not DB_PATH.exists():
        print(f"Database not found: {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # Check if info_type column exists
    cols = {row[1] for row in conn.execute("PRAGMA table_info(memories)").fetchall()}
    if "info_type" not in cols:
        print("info_type column doesn't exist yet. Run the app first to trigger migration.")
        sys.exit(1)

    total = conn.execute("SELECT COUNT(*) FROM memories WHERE status = 'active'").fetchone()[0]
    eligible = conn.execute(
        "SELECT COUNT(*) FROM memories WHERE status = 'active' AND info_type = 'fact'"
    ).fetchone()[0]
    print(f"Total active memories: {total}")
    print(f"Eligible for classification (info_type='fact'): {eligible}")
    print()

    updated_total = 0
    for sql_cond, info_type, desc in RULES:
        query = f"""
            UPDATE memories SET info_type = ?
            WHERE status = 'active' AND info_type = 'fact' AND ({sql_cond})
        """
        cur = conn.execute(query, (info_type,))
        count = cur.rowcount
        if count > 0:
            print(f"  {desc}: {count} records → {info_type}")
            updated_total += count

    conn.commit()

    # Print distribution
    print(f"\nTotal updated: {updated_total}")
    print("\nFinal distribution:")
    for row in conn.execute(
        "SELECT info_type, COUNT(*) as cnt FROM memories WHERE status = 'active' "
        "GROUP BY info_type ORDER BY cnt DESC"
    ).fetchall():
        print(f"  {row[0] or '(empty)'}: {row[1]}")

    conn.close()
    print("\nDone.")


if __name__ == "__main__":
    migrate()
