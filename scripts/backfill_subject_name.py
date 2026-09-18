# -*- coding: utf-8 -*-
"""
Backfill subject_name / speaker_name for pre-PR#24 memories.

Memories created before PR #24 have empty subject_name and speaker_name,
which means the subject_guardrail cannot protect them from hijacked updates.

Strategy:
  - speaker_name: set from source_ai if it maps to a known family role
  - subject_name: infer from content by scanning for family names
    * If exactly one family member is mentioned -> that member
    * If content is clearly about Ceci (first-person "我", no other names) -> ceci
    * If ambiguous or no match -> leave empty (fail-open, same as today)

Usage:
    # Dry-run (default): report what would change
    ALLOW_DEFAULT_HUB_SECRET=1 python scripts/backfill_subject_name.py --dry-run

    # Execute
    ALLOW_DEFAULT_HUB_SECRET=1 python scripts/backfill_subject_name.py --execute

    # Output report to file
    ALLOW_DEFAULT_HUB_SECRET=1 python scripts/backfill_subject_name.py --dry-run --output data/backfill_report.json
"""
import argparse
import json
import os
import sys
import sqlite3
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault("ALLOW_DEFAULT_HUB_SECRET", "1")

import identity_whitelist as iw


def _infer_subject_from_content(content: str) -> str:
    """Scan content for family member names. Returns role or empty string."""
    if not content:
        return ""

    lower = content.lower()
    found_roles: set[str] = set()

    for alias, role in iw.NAME_ALIASES.items():
        if alias in lower:
            found_roles.add(role)

    if len(found_roles) == 1:
        return found_roles.pop()

    if len(found_roles) == 0:
        return iw.ROLE_CECI

    return ""


def _infer_speaker(source_ai: str) -> str:
    """Map source_ai to a role-based speaker name."""
    if not source_ai:
        return ""
    role = iw.role_from_name(source_ai)
    if iw.is_known_family(role):
        return source_ai.strip().lower()
    return ""


def main():
    parser = argparse.ArgumentParser(description="Backfill subject_name/speaker_name")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="Report only, don't modify (default)")
    parser.add_argument("--execute", action="store_true",
                        help="Actually update the database")
    parser.add_argument("--output", type=str, default="",
                        help="Write report to JSON file")
    args = parser.parse_args()

    if args.execute:
        args.dry_run = False

    import database as db
    import asyncio
    asyncio.run(db.init_db())
    conn = sqlite3.connect(db.DB_PATH)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT id, content, source_ai, subject_name, speaker_name "
        "FROM memories WHERE status = 'active' "
        "AND (subject_name = '' OR speaker_name = '')"
    ).fetchall()

    print(f"Found {len(rows)} memories with empty subject_name or speaker_name")

    updates = []
    stats = {
        "total_scanned": len(rows),
        "subject_inferred": 0,
        "speaker_inferred": 0,
        "subject_by_role": {},
        "unchanged": 0,
    }

    for row in rows:
        mem_id = row["id"]
        content = row["content"] or ""
        source_ai = row["source_ai"] or ""
        old_subject = row["subject_name"] or ""
        old_speaker = row["speaker_name"] or ""

        new_subject = old_subject
        new_speaker = old_speaker

        if not old_subject:
            new_subject = _infer_subject_from_content(content)

        if not old_speaker:
            new_speaker = _infer_speaker(source_ai)

        if new_subject == old_subject and new_speaker == old_speaker:
            stats["unchanged"] += 1
            continue

        if new_subject != old_subject:
            stats["subject_inferred"] += 1
            stats["subject_by_role"][new_subject] = stats["subject_by_role"].get(new_subject, 0) + 1

        if new_speaker != old_speaker:
            stats["speaker_inferred"] += 1

        updates.append({
            "id": mem_id,
            "content_preview": content[:60],
            "source_ai": source_ai,
            "old_subject": old_subject,
            "new_subject": new_subject,
            "old_speaker": old_speaker,
            "new_speaker": new_speaker,
        })

    print(f"\nStats:")
    print(f"  subject_name to fill: {stats['subject_inferred']}")
    print(f"  speaker_name to fill: {stats['speaker_inferred']}")
    print(f"  unchanged: {stats['unchanged']}")
    print(f"  subject by role: {json.dumps(stats['subject_by_role'], ensure_ascii=False)}")

    if args.output:
        report = {"stats": stats, "updates": updates}
        os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"\nReport written to {args.output}")

    if not args.dry_run and updates:
        print(f"\nApplying {len(updates)} updates...")
        for u in updates:
            conn.execute(
                "UPDATE memories SET subject_name = ?, speaker_name = ? WHERE id = ?",
                (u["new_subject"], u["new_speaker"], u["id"]),
            )
        conn.commit()
        print("Done.")
    elif updates:
        print(f"\nDry-run: {len(updates)} memories would be updated. Use --execute to apply.")

    conn.close()


if __name__ == "__main__":
    main()
