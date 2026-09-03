"""Proposal-pending fix — Step S0: report-only audit of stuck pending proposals.

    python tools/audit_stuck_proposals.py --report [--db-path PATH] [--out DIR]

Read-only. Never touches proposals or memories.

Emits three files into --out (default: ./audit-out/YYYY-MM-DDTHH-MM-SSZ/):
  1. report.md          — human-readable, sorted by category and age
  2. report.json        — machine-readable, one row per proposal
  3. plan-template.json — operator-plan skeleton (v5.1 §A4 shape),
                          with an `expected_fingerprint` field per proposal
                          that Ceci can later approve / reject / annotate

Categories (v5.1):
  A: maintenance_action in ('', 'create') — normal create, safest to adopt
  B: maintenance_action in ('update', 'supersede') — needs target_snapshot
  C: any other maintenance_action or malformed — NOT adoptable via CLI

Later phases:
  * S1 adds promotion columns (claim / snapshot / protocol version)
  * S6 adds `--adopt-plan <plan.json>` (currently stubbed with clear error)

Nothing in this script writes to the DB. `--adopt-plan` refuses with a
NotImplementedError pointing to the施工 step.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure repo root is on sys.path so `memory_payload` imports cleanly whether
# the script is invoked from the repo root or /opt/memory-hub.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from memory_payload import canonical_proposal_fingerprint  # noqa: E402


CATEGORY_A = "A"  # normal create — safest
CATEGORY_B = "B"  # maintenance update/supersede — needs snapshot
CATEGORY_C = "C"  # unsupported maintenance_action or malformed


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _fetch_pending(conn: sqlite3.Connection) -> list[dict]:
    cols = _table_columns(conn, "proposals")
    col_list = sorted(cols)
    rows = conn.execute(
        f"SELECT {', '.join(col_list)} FROM proposals "
        "WHERE status='pending' ORDER BY created_at ASC"
    ).fetchall()
    return [dict(zip(col_list, r)) for r in rows]


def _classify(prop: dict) -> tuple[str, str]:
    """Return (category, note)."""
    ma = (prop.get("maintenance_action") or "").strip()
    if ma in ("", "create"):
        return CATEGORY_A, "normal create — CLI safe path"
    if ma in ("update", "supersede"):
        # In v5.1 these require a target_snapshot column; that column ships in
        # S1. Legacy rows have no snapshot at all → CLI must refuse and ask
        # Ceci to recreate a fresh v=2 maintenance proposal.
        has_snap = bool((prop.get("target_snapshot_json") or "").strip())
        target = prop.get("maintenance_target_id") or ""
        if not has_snap:
            return (
                CATEGORY_C,
                f"legacy maintenance ({ma}) without target_snapshot — "
                f"target_id={target!r}; CLI cannot adopt, recreate as fresh v2 proposal",
            )
        return CATEGORY_B, f"maintenance ({ma}) with snapshot"
    return CATEGORY_C, f"unsupported maintenance_action={ma!r}"


def _fingerprint_or_reason(prop: dict) -> tuple[str | None, str]:
    """Compute fingerprint; return (fp, note). fp is None if we cannot."""
    try:
        snap_raw = (prop.get("target_snapshot_json") or "").strip()
        snap = json.loads(snap_raw) if snap_raw else None
        return canonical_proposal_fingerprint(prop, target_snapshot=snap), ""
    except ValueError as e:
        return None, f"fingerprint_skipped: {e}"
    except Exception as e:  # noqa: BLE001 — audit must not crash on bad rows
        return None, f"fingerprint_error: {type(e).__name__}: {e}"


def _age_days(created_at: str, now: datetime) -> float:
    try:
        # created_at is ISO8601 UTC in this codebase; be forgiving of 'Z'
        ts = created_at.rstrip("Z")
        if "+" not in ts and "-" not in ts[10:]:
            ts += "+00:00"
        created = datetime.fromisoformat(ts)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return round((now - created).total_seconds() / 86400.0, 2)
    except Exception:
        return -1.0


def _summarize(prop: dict) -> str:
    c = (prop.get("content") or "").replace("\n", " ").strip()
    return (c[:120] + "…") if len(c) > 120 else c


def build_report(db_path: Path) -> dict:
    if not db_path.exists():
        raise FileNotFoundError(db_path)
    now = datetime.now(timezone.utc)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        conn.row_factory = None
        pending = _fetch_pending(conn)
    finally:
        conn.close()

    rows_out: list[dict] = []
    counts = {CATEGORY_A: 0, CATEGORY_B: 0, CATEGORY_C: 0}
    for p in pending:
        cat, cat_note = _classify(p)
        fp, fp_note = _fingerprint_or_reason(p)
        counts[cat] += 1
        rows_out.append({
            "proposal_id": p.get("id"),
            "category": cat,
            "category_note": cat_note,
            "age_days": _age_days(p.get("created_at") or "", now),
            "created_at": p.get("created_at"),
            "content_preview": _summarize(p),
            "layer": p.get("layer"),
            "proposed_room": p.get("proposed_room"),
            "proposer_ai_id": p.get("proposer_ai_id"),
            "triage_reason": p.get("triage_reason"),
            "maintenance_action": p.get("maintenance_action") or "",
            "maintenance_target_id": p.get("maintenance_target_id") or "",
            "has_target_snapshot": bool((p.get("target_snapshot_json") or "").strip()),
            "confidence": p.get("confidence"),
            "expected_fingerprint": fp,
            "fingerprint_note": fp_note,
        })

    return {
        "generated_at": now.isoformat(),
        "generated_by": f"{getpass.getuser()}@{os.uname().nodename}"
                        if hasattr(os, "uname") else getpass.getuser(),
        "db_path": str(db_path),
        "pending_total": len(rows_out),
        "counts_by_category": counts,
        "items": rows_out,
    }


def _plan_template(report: dict) -> dict:
    """Skeleton compatible with v5.1 §A4 operator plan.

    `plan_sha256` is left as an empty string; the eventual --adopt-plan CLI
    will compute and require the caller-signed checksum before executing.
    """
    return {
        "plan_id": f"op-plan-{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H-%M-%SZ')}",
        "created_at": report["generated_at"],
        "created_by": report["generated_by"],
        "report_sha256": hashlib.sha256(
            json.dumps(report, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest(),
        "plan_sha256": "",
        "notes": (
            "Review each item, set operator_decision to one of: "
            "'adopt_as_active' | 'reject' | 'defer' | 'recreate_fresh'. "
            "Only category A/B items with expected_fingerprint are adoptable. "
            "Category C items must be rejected or recreated."
        ),
        "items": [
            {
                "proposal_id": r["proposal_id"],
                "category": r["category"],
                "expected_fingerprint": r["expected_fingerprint"] or "",
                "operator_decision": "",
                "operator_note": "",
            }
            for r in report["items"]
        ],
    }


def _write_markdown(report: dict) -> str:
    lines: list[str] = []
    lines.append(f"# Stuck-proposal audit — {report['generated_at']}")
    lines.append("")
    lines.append(f"- DB: `{report['db_path']}`")
    lines.append(f"- Generated by: `{report['generated_by']}`")
    lines.append(f"- Pending total: **{report['pending_total']}**")
    lines.append("")
    lines.append("| Category | Meaning | Count |")
    lines.append("|---|---|---|")
    lines.append(f"| A | normal create — CLI safe | {report['counts_by_category']['A']} |")
    lines.append(f"| B | maintenance with snapshot — CLI safe | {report['counts_by_category']['B']} |")
    lines.append(f"| C | not adoptable — reject or recreate | {report['counts_by_category']['C']} |")
    lines.append("")

    for cat in ("A", "B", "C"):
        cat_items = [it for it in report["items"] if it["category"] == cat]
        if not cat_items:
            continue
        lines.append(f"## Category {cat} ({len(cat_items)})")
        lines.append("")
        for it in sorted(cat_items, key=lambda x: x["age_days"], reverse=True):
            lines.append(f"### `{it['proposal_id']}`  ·  {it['age_days']} d  ·  {it['proposed_room']}")
            lines.append("")
            lines.append(f"> {it['content_preview']}")
            lines.append("")
            fp = it["expected_fingerprint"] or "(none)"
            lines.append(f"- proposer: `{it['proposer_ai_id']}`  ·  triage: `{it['triage_reason']}`  ·  confidence: `{it['confidence']}`")
            lines.append(f"- created_at: `{it['created_at']}`")
            lines.append(f"- maintenance_action: `{it['maintenance_action'] or '(none)'}` "
                         f" ·  target_id: `{it['maintenance_target_id'] or '(none)'}`"
                         f"  ·  has_snapshot: `{it['has_target_snapshot']}`")
            lines.append(f"- expected_fingerprint: `{fp[:32]}…`" if fp != "(none)"
                         else f"- expected_fingerprint: `{fp}`  ·  reason: {it['fingerprint_note']}")
            lines.append(f"- category_note: {it['category_note']}")
            lines.append("")
    return "\n".join(lines) + "\n"


def cmd_report(args: argparse.Namespace) -> int:
    db_path = Path(args.db_path).expanduser().resolve()
    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    session_dir = out_dir / ts
    session_dir.mkdir()

    report = build_report(db_path)
    (session_dir / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (session_dir / "report.md").write_text(_write_markdown(report), encoding="utf-8")
    (session_dir / "plan-template.json").write_text(
        json.dumps(_plan_template(report), indent=2, ensure_ascii=False), encoding="utf-8"
    )

    print(f"Wrote: {session_dir}")
    for name in ("report.md", "report.json", "plan-template.json"):
        print(f"  - {name}")
    print(f"Pending total: {report['pending_total']}  "
          f"(A={report['counts_by_category']['A']} "
          f"B={report['counts_by_category']['B']} "
          f"C={report['counts_by_category']['C']})")
    return 0


class OperatorGateError(RuntimeError):
    """Any operator-mode precondition failed. Never advance a plan when
    raised — the caller must fix the environment / plan file first."""


ALLOWED_PLAN_DIRS_ENV = "HUB_OPERATOR_PLAN_DIRS"


def _default_allowed_plan_dirs() -> list[Path]:
    dirs = os.environ.get(ALLOWED_PLAN_DIRS_ENV, "").strip()
    if dirs:
        return [Path(p).expanduser().resolve() for p in dirs.split(os.pathsep) if p]
    return [
        Path("/etc/memhub/operator-plans").resolve() if os.name != "nt" else Path.home() / ".memhub/operator-plans",
        (Path.home() / ".memhub/operator-plans").resolve(),
    ]


def _check_operator_env(i_am_operator: bool = False) -> None:
    """Runtime gate: refuses to run outside operator mode.

    Layers:
      1. HUB_OPERATOR_MODE=1 must be set explicitly.
      2. stdin must be a TTY (interactive shell), unless the caller passes
         --i-am-operator to opt into non-TTY execution (wrapper script,
         cron, etc.). Non-TTY without the flag is refused.
    """
    if os.environ.get("HUB_OPERATOR_MODE", "").strip() != "1":
        raise OperatorGateError(
            "HUB_OPERATOR_MODE=1 not set. Run this only on the VPS shell as "
            "operator: `export HUB_OPERATOR_MODE=1` before invoking."
        )
    if not i_am_operator:
        try:
            is_tty = sys.stdin.isatty()
        except (AttributeError, ValueError):
            is_tty = False
        if not is_tty:
            raise OperatorGateError(
                "stdin is not a TTY; refusing to run adopt-plan in a "
                "non-interactive shell. If this is intentional (wrapper "
                "script, cron job) pass --i-am-operator to confirm."
            )


def _check_plan_file(plan_path: Path) -> Path:
    """Filesystem gate for the plan file. Returns the resolved path.

    Refuses:
      * symlinks
      * paths outside allowed dirs (default: /etc/memhub/operator-plans/
        or ~/.memhub/operator-plans/)
      * non-regular files
      * files not owned by the current caller uid
      * files with group- or world-write bits set
    """
    if not plan_path.exists():
        raise OperatorGateError(f"plan file not found: {plan_path}")
    if plan_path.is_symlink():
        raise OperatorGateError(
            f"plan path must not be a symlink: {plan_path}"
        )
    resolved = plan_path.resolve(strict=True)
    if resolved != plan_path.absolute():
        raise OperatorGateError(
            f"plan path resolves differently: {plan_path} -> {resolved}"
        )
    if not resolved.is_file():
        raise OperatorGateError(f"plan path is not a regular file: {resolved}")

    allowed = _default_allowed_plan_dirs()
    if not any(_is_under(resolved, d) for d in allowed):
        raise OperatorGateError(
            f"plan {resolved} is outside allowed dirs {allowed}"
        )

    if hasattr(os, "getuid"):
        st = resolved.stat()
        if st.st_uid != os.getuid():
            raise OperatorGateError(
                f"plan {resolved} owned by uid={st.st_uid}, "
                f"not caller uid={os.getuid()}"
            )
        if st.st_mode & 0o022:
            raise OperatorGateError(
                f"plan {resolved} has group/world write bits: "
                f"{oct(st.st_mode & 0o777)}"
            )
    return resolved


def _is_under(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _load_plan(plan_path: Path) -> dict:
    """Load and integrity-check the plan file. `plan_sha256` is treated
    as a corruption checksum, NOT an authenticity signature — the real
    authority boundary is filesystem permissions (checked above).

    v5.1 H4: `plan_sha256` is REQUIRED (Codex saw plans missing the field
    slip through). Must be exactly 64 lowercase hex chars AND match the
    computed sha256 over the rest of the plan.
    """
    resolved = _check_plan_file(plan_path)
    raw = resolved.read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise OperatorGateError(f"plan {resolved} is not a JSON object")
    if "items" not in data or not isinstance(data["items"], list):
        raise OperatorGateError(f"plan {resolved} missing items list")
    stated = data.get("plan_sha256", "")
    if not stated:
        raise OperatorGateError(
            f"plan {resolved} missing required field plan_sha256; run the "
            "audit report first and re-sign the plan."
        )
    if (len(stated) != 64
            or any(c not in "0123456789abcdef" for c in stated.lower())):
        raise OperatorGateError(
            f"plan_sha256 must be 64-char lowercase hex; got {stated!r}"
        )
    stripped = {k: v for k, v in data.items() if k != "plan_sha256"}
    computed = hashlib.sha256(
        json.dumps(stripped, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    if stated != computed:
        raise OperatorGateError(
            f"plan checksum mismatch: stated {stated[:12]}..., computed {computed[:12]}..."
        )
    return data


def cmd_adopt_plan(args: argparse.Namespace) -> int:
    """Operator CLI: apply a reviewed adoption plan.

    Refuses to run unless every operator gate passes. See v5.1 A4 in
    docs/proposal-pending-fix-plan.md for the security rationale.
    """
    _check_operator_env(i_am_operator=getattr(args, "i_am_operator", False))
    # Import database only AFTER the env gate — a caller that skipped the
    # gate should never reach the DB layer.
    import database
    from datetime import datetime, timezone

    # v5.1 H3: independent CLI processes have no globally-init'd database
    # connection. Explicitly init it against --db-path (or HUB_DB_PATH) so
    # commit_promotion_atomic / adopt_legacy_proposal_atomic can write.
    db_path = getattr(args, "db_path", None) or os.environ.get("HUB_DB_PATH")
    if not db_path:
        raise OperatorGateError(
            "no DB path — pass --db-path or set HUB_DB_PATH before running"
        )
    import asyncio
    asyncio.run(database.init_db(str(Path(db_path).expanduser())))

    plan_path = Path(args.plan_path).expanduser()
    plan = _load_plan(plan_path)

    print(f"[{datetime.now(timezone.utc).isoformat()}] adopt-plan start")
    print(f"  plan_id: {plan.get('plan_id')}")
    print(f"  items:   {len(plan['items'])}")

    adopted, refused, errors = 0, 0, 0
    results = []
    for item in plan["items"]:
        pid = item.get("proposal_id")
        decision = (item.get("operator_decision") or "").strip()
        fp = item.get("expected_fingerprint") or ""
        note = item.get("operator_note", "")
        if not pid:
            errors += 1
            results.append({"proposal_id": None, "outcome": "missing_id"})
            continue
        if decision != "adopt_as_active":
            # 'reject', 'defer', 'recreate_fresh', or empty → skip this
            # entry; operator must handle those out-of-band.
            refused += 1
            results.append({"proposal_id": pid, "outcome": f"skipped ({decision or 'no_decision'})"})
            continue
        if not fp or len(fp) != 64:
            errors += 1
            results.append({"proposal_id": pid, "outcome": "invalid_fingerprint"})
            continue
        try:
            r = database.adopt_legacy_proposal_atomic(
                pid, fp, reviewed_by=f"operator_cli:{plan.get('plan_id', '')}",
                plan_id=plan.get("plan_id", ""),
            )
            if r.get("error"):
                errors += 1
                results.append({"proposal_id": pid, "outcome": f"error: {r['error']}"})
            else:
                adopted += 1
                results.append({
                    "proposal_id": pid,
                    "outcome": "adopted",
                    "memory_id": r.get("memory_id"),
                    "note": note,
                })
        except database.LegacyContentDrift as e:
            errors += 1
            results.append({"proposal_id": pid, "outcome": "fingerprint_drift", "detail": str(e)})
        except Exception as e:  # noqa: BLE001
            errors += 1
            results.append({
                "proposal_id": pid,
                "outcome": f"{type(e).__name__}: {e}",
            })

    print(f"[{datetime.now(timezone.utc).isoformat()}] adopt-plan done")
    print(f"  adopted: {adopted}")
    print(f"  refused: {refused}  (skipped decisions)")
    print(f"  errors:  {errors}")
    for r in results:
        print(f"    - {r}")
    return 0 if errors == 0 else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_report = sub.add_parser("report", help="Read-only audit of pending proposals")
    p_report.add_argument("--db-path", default=os.environ.get("HUB_DB_PATH", "memory.db"))
    p_report.add_argument("--out", default="./audit-out")
    p_report.set_defaults(func=cmd_report)

    p_adopt = sub.add_parser("adopt-plan", help="Apply a reviewed operator plan")
    p_adopt.add_argument("plan_path")
    p_adopt.add_argument("--db-path", default=os.environ.get("HUB_DB_PATH"),
                         help="Path to memory-hub SQLite DB (or set HUB_DB_PATH).")
    p_adopt.add_argument("--i-am-operator", action="store_true",
                         help="Confirm non-interactive execution (wrapper "
                              "script, cron). Otherwise stdin must be a TTY.")
    p_adopt.set_defaults(func=cmd_adopt_plan)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
