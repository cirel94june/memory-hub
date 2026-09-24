"""Shared todo rule used by corridor, context(incremental) and context(full)."""
import os
import sys
from datetime import datetime, timezone, timedelta

os.environ.setdefault("ALLOW_DEFAULT_HUB_SECRET", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory_ops import is_open_todo, sort_todos

NOW = datetime(2026, 9, 24, tzinfo=timezone.utc)


def _m(info_type, days_ago=1, **kw):
    base = {"status": "active", "resolved": False, "info_type": info_type,
            "created_at": (NOW - timedelta(days=days_ago)).isoformat()}
    base.update(kw)
    return base


def test_task_is_todo_even_when_old():
    assert is_open_todo(_m("task", days_ago=60), NOW)


def test_recent_event_is_todo():
    assert is_open_todo(_m("event", days_ago=3), NOW)


def test_old_event_drops_out():
    assert not is_open_todo(_m("event", days_ago=60), NOW)


def test_facts_states_identity_never_todo():
    for t in ("fact", "state", "identity", "relationship", "reflection", ""):
        assert not is_open_todo(_m(t), NOW), t


def test_correction_never_todo():
    assert not is_open_todo(_m("task", provenance_type="user_correction"), NOW)


def test_resolved_or_unmarked_not_todo():
    assert not is_open_todo(_m("task", resolved=True), NOW)
    assert not is_open_todo(_m("task", resolved=None), NOW)


def test_social_autocapture_excluded():
    assert not is_open_todo(_m("task", room="social", source_platform="auto_capture:group"), NOW)


def test_sort_newest_first():
    a = {"updated_at": "2026-09-01"}
    b = {"updated_at": "2026-09-20"}
    assert sort_todos([a, b]) == [b, a]
