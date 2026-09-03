"""S1 (v5.1) — proposal promotion-column migration.

Boundary: this batch only adds 4 columns and tags new inserts with v=2.
No worker, no promotion path, no runtime hooks.

Coverage (verifies Ceci's S1 gating):
  1. Old-schema DB (no promotion columns) → init_db adds them, data intact
  2. Repeated init_db → no error, no dup columns, still idempotent
  3. Old rows keep promotion_protocol_version = 0
  4. New inserts through insert_proposal() are stamped v = 2
  5. Migration failure inside init_db leaves the original DB unchanged
     (verified via a snapshot / restore around a forced failure)
  6. _PROPOSAL_COLUMNS drift check — schema and column list match exactly
"""
import asyncio
import os
import sqlite3
import shutil
import sys
from datetime import datetime, timezone

os.environ.setdefault("ALLOW_DEFAULT_HUB_SECRET", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import database


NEW_COLS = {
    "promotion_claim_id",
    "promotion_claim_at",
    "promotion_protocol_version",
    "target_snapshot_json",
}


def _table_cols(db_path) -> dict:
    """Return {col_name: (type, dflt, notnull)}."""
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("PRAGMA table_info(proposals)").fetchall()
        # row: (cid, name, type, notnull, dflt_value, pk)
        return {r[1]: (r[2], r[4], r[3]) for r in rows}
    finally:
        conn.close()


def _seed_pre_v51_proposals_db(db_path) -> list[str]:
    """Create a DB that has the *current pre-S1* proposals shape (i.e. all
    columns EXCEPT the four new promotion ones), insert two rows, close.
    Returns the inserted proposal ids.

    We rebuild the pre-S1 schema by hand so the test does not depend on
    running an older revision of `init_db`.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript("""
            CREATE TABLE proposals (
                id                    TEXT PRIMARY KEY,
                content               TEXT NOT NULL,
                claim_type            TEXT NOT NULL DEFAULT 'observation',
                speech_mode           TEXT NOT NULL DEFAULT 'uncertain',
                conversation_kind     TEXT NOT NULL DEFAULT 'house_chat',
                proposed_room         TEXT NOT NULL DEFAULT 'living_room',
                source_message_ids    TEXT NOT NULL DEFAULT '[]',
                evidence_excerpt      TEXT NOT NULL DEFAULT '',
                proposer_ai_id        TEXT NOT NULL DEFAULT '',
                confidence            REAL NOT NULL DEFAULT 0.5,
                conflicts_with        TEXT NOT NULL DEFAULT '[]',
                status                TEXT NOT NULL DEFAULT 'pending',
                layer                 TEXT NOT NULL DEFAULT 'shared',
                owner_ai              TEXT NOT NULL DEFAULT '',
                importance            REAL NOT NULL DEFAULT 0.5,
                emotion_arousal       REAL NOT NULL DEFAULT 0.3,
                category              TEXT NOT NULL DEFAULT '',
                tags                  TEXT NOT NULL DEFAULT '[]',
                event_date            TEXT NOT NULL DEFAULT '',
                source_context        TEXT NOT NULL DEFAULT '',
                source_platform       TEXT NOT NULL DEFAULT '',
                provenance_type       TEXT NOT NULL DEFAULT '',
                created_at            TEXT NOT NULL,
                reviewed_at           TEXT NOT NULL DEFAULT '',
                reviewed_by           TEXT NOT NULL DEFAULT '',
                reject_reason         TEXT NOT NULL DEFAULT '',
                triage_reason         TEXT NOT NULL DEFAULT '',
                applied_memory_id     TEXT NOT NULL DEFAULT '',
                failure_reason        TEXT NOT NULL DEFAULT '',
                subject_id            TEXT NOT NULL DEFAULT '',
                source_actor_id       TEXT NOT NULL DEFAULT '',
                info_type             TEXT NOT NULL DEFAULT 'fact',
                maintenance_action    TEXT NOT NULL DEFAULT '',
                maintenance_target_id TEXT NOT NULL DEFAULT ''
            );
        """)
        now = datetime.now(timezone.utc).isoformat()
        conn.executemany(
            "INSERT INTO proposals (id, content, created_at) VALUES (?, ?, ?)",
            [("old_p1", "老的自动通过卡池 1", now),
             ("old_p2", "老的自动通过卡池 2", now)],
        )
        conn.commit()
    finally:
        conn.close()
    return ["old_p1", "old_p2"]


@pytest.fixture
def isolated_db(monkeypatch, tmp_path):
    db_path = tmp_path / "hub.db"
    monkeypatch.setattr(database, "DB_PATH", db_path)
    return db_path


# ── Test 1: 旧库迁移 —— 4 列被幂等加入，旧数据完整保留 ──────
def test_s1_old_schema_migrates_and_preserves_data(isolated_db):
    pre_ids = _seed_pre_v51_proposals_db(isolated_db)
    cols_before = set(_table_cols(isolated_db).keys())
    assert NEW_COLS.isdisjoint(cols_before), "sanity: pre-S1 schema really lacks 4 columns"

    asyncio.run(database.init_db(str(isolated_db)))

    cols_after = _table_cols(isolated_db)
    assert NEW_COLS.issubset(cols_after.keys()), "S1 migration must add all 4 columns"
    # Types & defaults
    assert cols_after["promotion_claim_id"][0].upper().startswith("TEXT")
    assert cols_after["promotion_claim_at"][0].upper().startswith("TEXT")
    assert cols_after["promotion_protocol_version"][0].upper().startswith("INT")
    assert cols_after["target_snapshot_json"][0].upper().startswith("TEXT")
    # Old data intact
    conn = sqlite3.connect(str(isolated_db))
    try:
        rows = conn.execute(
            "SELECT id, content, promotion_protocol_version, promotion_claim_id, "
            "promotion_claim_at, target_snapshot_json "
            "FROM proposals ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    assert [r[0] for r in rows] == pre_ids
    for r in rows:
        assert r[2] == 0, "legacy rows MUST retain promotion_protocol_version=0"
        assert r[3] == "" and r[4] == "" and r[5] == ""


# ── Test 2: 重复 init_db 幂等 ──────
def test_s1_repeated_init_is_idempotent(isolated_db):
    _seed_pre_v51_proposals_db(isolated_db)
    asyncio.run(database.init_db(str(isolated_db)))
    cols_1 = _table_cols(isolated_db)
    # Second call must not raise and must produce identical schema.
    asyncio.run(database.init_db(str(isolated_db)))
    cols_2 = _table_cols(isolated_db)
    assert cols_1 == cols_2
    # No dup rows for a given column name (SQLite would have raised anyway).
    conn = sqlite3.connect(str(isolated_db))
    try:
        names = [r[1] for r in conn.execute("PRAGMA table_info(proposals)").fetchall()]
    finally:
        conn.close()
    assert len(names) == len(set(names)), "no duplicate columns after 2× init_db"


# ── Test 3: 旧行始终 v=0 ──────
def test_s1_legacy_rows_stay_v0_after_migration(isolated_db):
    _seed_pre_v51_proposals_db(isolated_db)
    asyncio.run(database.init_db(str(isolated_db)))
    conn = sqlite3.connect(str(isolated_db))
    try:
        versions = [r[0] for r in conn.execute(
            "SELECT promotion_protocol_version FROM proposals"
        ).fetchall()]
    finally:
        conn.close()
    assert versions == [0, 0], "migration must NEVER touch existing rows' version"


# ── Test 4: 新 insert_proposal 显式写 v=2 ──────
def test_s1_new_insert_is_stamped_v2(isolated_db):
    # Start with a fresh DB so init_db creates the current schema wholesale.
    asyncio.run(database.init_db(str(isolated_db)))
    now = datetime.now(timezone.utc).isoformat()
    database.insert_proposal({
        "id": "new_p1",
        "content": "新 proposal",
        "created_at": now,
        # deliberately do NOT pass promotion_protocol_version — the DB layer
        # is responsible for stamping v=2 on every new insert.
    })
    conn = sqlite3.connect(str(isolated_db))
    try:
        row = conn.execute(
            "SELECT promotion_protocol_version, promotion_claim_id, "
            "promotion_claim_at, target_snapshot_json "
            "FROM proposals WHERE id='new_p1'"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] == 2, "new proposals MUST be stamped promotion_protocol_version=2"
    assert row[1] == ""
    assert row[2] == ""
    assert row[3] == ""


# ── Test 5: 新旧共存 —— 迁移后 legacy=v0，新增=v2 ──────
def test_s1_new_and_legacy_coexist_with_correct_versions(isolated_db):
    _seed_pre_v51_proposals_db(isolated_db)
    asyncio.run(database.init_db(str(isolated_db)))
    now = datetime.now(timezone.utc).isoformat()
    database.insert_proposal({"id": "new_after_migration", "content": "x", "created_at": now})
    conn = sqlite3.connect(str(isolated_db))
    try:
        by_id = dict(conn.execute(
            "SELECT id, promotion_protocol_version FROM proposals"
        ).fetchall())
    finally:
        conn.close()
    assert by_id == {"old_p1": 0, "old_p2": 0, "new_after_migration": 2}


# ── Test 6: 迁移失败保留原状态 ──────
def test_s1_migration_failure_leaves_db_untouched(isolated_db, monkeypatch):
    """Simulate init_db raising mid-way; the snapshot pre-init must equal the
    on-disk file after the failed init. This proves we did not silently write
    partial migrations to the caller's DB."""
    _seed_pre_v51_proposals_db(isolated_db)
    snapshot = isolated_db.with_suffix(".snap")
    shutil.copy2(isolated_db, snapshot)

    # Force a failure DEEP inside init_db by wrapping sqlite3.connect so any
    # ALTER TABLE that would add the first new v5.1 column blows up.
    # sqlite3.Connection is a C-level immutable type; we wrap the whole
    # connection with a delegating proxy instead of patching the class.
    real_connect = database.sqlite3.connect

    # Fail on the THIRD v5.1 column (promotion_protocol_version). The first
    # two ALTERs succeed inside the same explicit transaction; the failure
    # must trigger ROLLBACK and leave zero of the four columns behind.
    class SabotageConn:
        def __init__(self, real):
            self._r = real
        def execute(self, sql, *a, **kw):
            if "ALTER TABLE PROPOSALS ADD COLUMN PROMOTION_PROTOCOL_VERSION" in sql.strip().upper():
                raise sqlite3.OperationalError("simulated failure on 3rd v5.1 ALTER")
            return self._r.execute(sql, *a, **kw)
        def __getattr__(self, name):
            return getattr(self._r, name)
        def __setattr__(self, name, value):
            if name == "_r":
                object.__setattr__(self, name, value)
            else:
                setattr(self._r, name, value)
        def __enter__(self):
            self._r.__enter__()
            return self
        def __exit__(self, *a):
            return self._r.__exit__(*a)

    def sabotage_connect(*a, **kw):
        return SabotageConn(real_connect(*a, **kw))

    monkeypatch.setattr(database.sqlite3, "connect", sabotage_connect)
    with pytest.raises(sqlite3.OperationalError):
        asyncio.run(database.init_db(str(isolated_db)))
    monkeypatch.setattr(database.sqlite3, "connect", real_connect)

    # The failure happened at the very first new column; earlier columns
    # (triage_reason etc.) may have been added, but the 4 v5.1 columns
    # MUST NOT exist and legacy rows MUST remain unchanged.
    cols_now = set(_table_cols(isolated_db).keys())
    assert not (NEW_COLS & cols_now), "no v5.1 promotion columns after failed init"

    # Legacy row data must equal snapshot's legacy row data.
    def read_ids(p):
        c = sqlite3.connect(str(p))
        try:
            return sorted(r[0] for r in c.execute("SELECT id FROM proposals"))
        finally:
            c.close()
    assert read_ids(isolated_db) == read_ids(snapshot)


# ── Test 7: _PROPOSAL_COLUMNS 与实际表 schema 对齐 ──────
def test_s1_proposal_columns_list_matches_schema(isolated_db):
    asyncio.run(database.init_db(str(isolated_db)))
    schema_cols = set(_table_cols(isolated_db).keys())
    list_cols = set(database._PROPOSAL_COLUMNS)
    missing_in_list = schema_cols - list_cols
    extra_in_list = list_cols - schema_cols
    assert not missing_in_list, f"_PROPOSAL_COLUMNS missing: {missing_in_list}"
    assert not extra_in_list, f"_PROPOSAL_COLUMNS has phantom cols: {extra_in_list}"
    for c in NEW_COLS:
        assert c in list_cols, f"{c!r} must be in _PROPOSAL_COLUMNS for INSERT to hit it"


# ── Test 8: insert_proposal 永远写 v=2 —— caller 无法伪造 v=0 新行 ─────
def test_s1_insert_proposal_forces_v2_even_when_caller_passes_zero(isolated_db):
    """The public insert path must be fail-closed: any caller that hands in
    version=0 (mistakenly or maliciously) still gets a v=2 row, so recovery
    can never miss a fresh insert. S6 adopt-legacy will UPDATE existing v=0
    rows in place — it must never insert a new v=0 row through this path."""
    asyncio.run(database.init_db(str(isolated_db)))
    now = datetime.now(timezone.utc).isoformat()
    database.insert_proposal({
        "id": "attempted_v0",
        "content": "caller tries to inject v=0 here",
        "created_at": now,
        "promotion_protocol_version": 0,   # ← ignored
    })
    conn = sqlite3.connect(str(isolated_db))
    try:
        v = conn.execute(
            "SELECT promotion_protocol_version FROM proposals WHERE id='attempted_v0'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert v == 2, "insert_proposal MUST clobber caller-supplied v=0; no escape hatch"


# ── Test 9: 第二列 ALTER 失败也全 rollback（补 Codex Medium 反例）─────
def test_s1_second_column_failure_rolls_back_all_four(isolated_db, monkeypatch):
    """Migration must be atomic across the four v5.1 columns; if the second
    ALTER fails, the first ALTER must be rolled back too — so the DB looks
    like the migration never ran and the next init_db can try again cleanly."""
    _seed_pre_v51_proposals_db(isolated_db)
    real_connect = database.sqlite3.connect

    class Sabotage2:
        def __init__(self, real): self._r = real
        def execute(self, sql, *a, **kw):
            if "ALTER TABLE PROPOSALS ADD COLUMN PROMOTION_CLAIM_AT" in sql.strip().upper():
                raise sqlite3.OperationalError("simulated failure on 2nd v5.1 ALTER")
            return self._r.execute(sql, *a, **kw)
        def __getattr__(self, name): return getattr(self._r, name)
        def __setattr__(self, name, value):
            if name == "_r": object.__setattr__(self, name, value)
            else: setattr(self._r, name, value)
        def __enter__(self): self._r.__enter__(); return self
        def __exit__(self, *a): return self._r.__exit__(*a)

    monkeypatch.setattr(database.sqlite3, "connect",
                        lambda *a, **kw: Sabotage2(real_connect(*a, **kw)))
    with pytest.raises(sqlite3.OperationalError):
        asyncio.run(database.init_db(str(isolated_db)))
    monkeypatch.setattr(database.sqlite3, "connect", real_connect)

    cols_now = set(_table_cols(isolated_db).keys())
    leaked = NEW_COLS & cols_now
    assert not leaked, (
        f"atomic v5.1 migration leaked partial columns: {leaked}. "
        "Second-column failure must ROLLBACK first-column ALTER too."
    )
