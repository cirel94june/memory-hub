"""Small persistent status report for background maintenance."""
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATUS_PATH = Path(__file__).parent / "data" / "daemon_status.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_status(payload: dict[str, Any]) -> None:
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = {"updated_at": _now(), **payload}
    STATUS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def read_status() -> dict[str, Any]:
    if not STATUS_PATH.exists():
        return {"status": "never_run", "updated_at": ""}
    try:
        data = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"status": "invalid", "updated_at": ""}
    except Exception as exc:
        return {"status": "invalid", "updated_at": "", "error": str(exc)}
