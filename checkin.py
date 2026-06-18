"""打卡日历：习惯追踪系统"""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "memories.db"


def _init():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS habits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            emoji TEXT NOT NULL DEFAULT '✅',
            color TEXT NOT NULL DEFAULT '#7c5cbf',
            sort_order INTEGER NOT NULL DEFAULT 0,
            archived INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS checkins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            habit_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (habit_id) REFERENCES habits(id),
            UNIQUE(habit_id, date)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_checkins_date ON checkins(date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_checkins_habit ON checkins(habit_id, date)")
    conn.commit()
    conn.close()


_init()


def list_habits(include_archived=False):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    if include_archived:
        rows = conn.execute("SELECT * FROM habits ORDER BY sort_order, id").fetchall()
    else:
        rows = conn.execute("SELECT * FROM habits WHERE archived=0 ORDER BY sort_order, id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def create_habit(name, emoji="✅", color="#7c5cbf"):
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "INSERT INTO habits (name, emoji, color, created_at) VALUES (?, ?, ?, ?)",
        (name, emoji, color, now),
    )
    habit_id = cur.lastrowid
    conn.commit()
    conn.close()
    return {"id": habit_id, "name": name, "emoji": emoji, "color": color}


def delete_habit(habit_id):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM checkins WHERE habit_id=?", (habit_id,))
    conn.execute("DELETE FROM habits WHERE id=?", (habit_id,))
    conn.commit()
    conn.close()


def toggle_checkin(habit_id, date):
    """Toggle checkin for a habit on a date. Returns True if checked in, False if removed."""
    conn = sqlite3.connect(DB_PATH)
    existing = conn.execute(
        "SELECT id FROM checkins WHERE habit_id=? AND date=?", (habit_id, date)
    ).fetchone()
    if existing:
        conn.execute("DELETE FROM checkins WHERE id=?", (existing[0],))
        conn.commit()
        conn.close()
        return False
    else:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO checkins (habit_id, date, created_at) VALUES (?, ?, ?)",
            (habit_id, date, now),
        )
        conn.commit()
        conn.close()
        return True


def get_checkins(start_date, end_date):
    """Get all checkins in a date range, grouped by date."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT habit_id, date FROM checkins WHERE date >= ? AND date <= ? ORDER BY date",
        (start_date, end_date),
    ).fetchall()
    conn.close()
    result = {}
    for r in rows:
        d = r["date"]
        if d not in result:
            result[d] = []
        result[d].append(r["habit_id"])
    return result


def get_streak(habit_id):
    """Get current streak for a habit."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT date FROM checkins WHERE habit_id=? ORDER BY date DESC LIMIT 60",
        (habit_id,),
    ).fetchall()
    conn.close()
    if not rows:
        return 0
    from datetime import timedelta
    today = datetime.now(timezone.utc).date()
    streak = 0
    expected = today
    for (d,) in rows:
        check_date = datetime.fromisoformat(d).date() if "T" in d else datetime.strptime(d, "%Y-%m-%d").date()
        if check_date == expected:
            streak += 1
            expected -= timedelta(days=1)
        elif check_date == expected - timedelta(days=1):
            expected = check_date
            streak += 1
            expected -= timedelta(days=1)
        else:
            break
    return streak
