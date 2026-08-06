"""
一次性脚本：把 Phase 1.5 首批生成的 7 份有质量问题的 Profile 标记为 superseded。

运行：python scripts/supersede_old_profiles.py [--dry-run]
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ALLOW_DEFAULT_HUB_SECRET", "1")

import asyncio
import database

PROFILE_IDS = [
    "user_ceci",
    "agent_claude", "agent_lucien", "agent_jasper",
    "rel_claude_ceci", "rel_lucien_ceci", "rel_jasper_ceci",
]


async def main():
    dry_run = "--dry-run" in sys.argv
    db_path = os.environ.get("DB_PATH", "data/memory_hub.db")
    await database.init_db(db_path)

    print(f"{'[DRY-RUN] ' if dry_run else ''}Superseding old profiles...")
    count = 0
    for pid in PROFILE_IDS:
        p = database.get_profile(pid)
        if not p:
            print(f"  {pid}: not found, skip")
            continue
        status = p.get("status", "unknown")
        if status == "superseded":
            print(f"  {pid}: already superseded, skip")
            continue
        if dry_run:
            print(f"  {pid}: would supersede (current: {status})")
        else:
            database.supersede_profile(pid)
            print(f"  {pid}: superseded (was: {status})")
        count += 1

    print(f"\n{'Would supersede' if dry_run else 'Superseded'}: {count} profiles")


if __name__ == "__main__":
    asyncio.run(main())
