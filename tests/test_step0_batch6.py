"""Step 0-A batch #6 — closure tests: 归档 Low + 完整 _prepare_memory_value
参数化回归 + _set_memory_in_tx golden 覆盖 + 跨文件枚举升级 rglob。

覆盖 Codex 前几批归档的 Low:
- batch #1 round-2 L2: _prepare_memory_value 参数化回归
- batch #3 L: _set_memory_in_tx golden (created_at / crq/link/finalize_claim
  preserve / embedding COALESCE / vec 三分支 / vec 写失败 best-effort)
- batch #3 fixup L: 跨文件枚举升级 rglob + 排除范围 + reversal 测试

批次 #5 已完成 memory_ops._WRITE_LOCK 断言更新，不在本文件覆盖。
"""
import ast
import asyncio
import os
import sqlite3
import sys
from pathlib import Path
from unittest import mock

import pytest

os.environ.setdefault("ALLOW_DEFAULT_HUB_SECRET", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
from database import _prepare_memory_value, EMBEDDING_DIM


# ── shared fixture ──

@pytest.fixture
def initdb(tmp_path):
    db_path = tmp_path / "test.db"
    database.DB_PATH = db_path
    database._conn = None
    asyncio.run(database.init_db(str(db_path)))
    yield db_path
    try:
        database.close_thread_read_conn()
    except Exception:
        pass


# ════════════════════════════════════════════
# Section A: _prepare_memory_value 参数化回归 (Codex batch #1 L2)
# ════════════════════════════════════════════

# Format: (key, input_value, expected_output, description)
_PREP_CASES = [
    # resolved / anchored — via _resolved_to_int
    ("resolved", None, None, "resolved None → None"),
    ("resolved", True, 1, "resolved True → 1"),
    ("resolved", False, 0, "resolved False → 0"),
    ("resolved", 1, 1, "resolved int 1 → 1"),
    ("resolved", 0, 0, "resolved int 0 → 0"),
    ("resolved", "weird", None, "resolved unknown → None (per _resolved_to_int)"),
    ("anchored", None, None, "anchored None → None"),
    ("anchored", True, 1, "anchored True → 1"),
    ("anchored", False, 0, "anchored False → 0"),

    # comments / history — JSON dumps for list/dict, "[]" for None
    ("comments", None, "[]", "comments None → '[]'"),
    ("comments", [], '[]', "comments empty list → '[]'"),
    ("comments", [{"x": 1}], '[{"x": 1}]', "comments list of dicts → JSON string"),
    ("comments", {"a": "b"}, '{"a": "b"}', "comments dict → JSON string"),
    ("comments", "already str", "already str", "comments already str → passthrough"),
    ("history", None, "[]", "history None → '[]'"),
    ("history", [{"h": 1}], '[{"h": 1}]', "history list → JSON string"),

    # embedding — bytes / None passthrough (no coercion)
    ("embedding", None, None, "embedding None → None (COALESCE handles at SQL)"),
    ("embedding", b"\x00" * (EMBEDDING_DIM * 4), b"\x00" * (EMBEDDING_DIM * 4),
     "embedding bytes → bytes passthrough"),

    # fact_confidence — REAL or None passthrough (Codex v2.9 H1: MUST preserve None)
    ("fact_confidence", None, None, "fact_confidence None → None (SQL NULL, 未知置信度)"),
    ("fact_confidence", 0.85, 0.85, "fact_confidence 0.85 → 0.85"),
    ("fact_confidence", 0.0, 0.0, "fact_confidence 0.0 → 0.0 (not treated as None)"),
    ("fact_confidence", 1.0, 1.0, "fact_confidence 1.0 → 1.0"),

    # other fields — None → "" (string default)
    ("id", None, "", "id None → ''"),
    ("id", "mem_123", "mem_123", "id str → passthrough"),
    ("content", None, "", "content None → ''"),
    ("content", "hello", "hello", "content str → passthrough"),
    ("room", None, "", "room None → ''"),
    ("owner_ai", None, "", "owner_ai None → ''"),
    ("client_request_id", None, "", "crq None → ''"),
    ("client_request_id", "req_abc", "req_abc", "crq str → passthrough"),

    # numeric fields where SQLite may see a float
    ("importance", None, "", "importance None → '' (not typed by _prepare)"),
    ("importance", 0.7, 0.7, "importance float → passthrough"),
    ("activation_count", None, "", "activation_count None → ''"),
    ("activation_count", 5, 5, "activation_count int → passthrough"),
]


@pytest.mark.parametrize("key,inp,expected,desc", _PREP_CASES,
                         ids=[c[3] for c in _PREP_CASES])
def test_prepare_memory_value_parametrized(key, inp, expected, desc):
    """参数化回归：11+ 边界值全通过 _prepare_memory_value 规范化契约。"""
    got = _prepare_memory_value(key, inp)
    assert got == expected, f"{desc}: expected {expected!r}, got {got!r}"


def test_prepare_memory_value_fact_confidence_none_stays_none():
    """v2.9 H1 契约核心断言：fact_confidence 的 None 必须保留（不改成 0.0）。"""
    assert _prepare_memory_value("fact_confidence", None) is None
    # SQL NULL 语义验证 —— 用生产 set_memory 写入后读回也是 None
    # (需要 initdb fixture — 单独一条测试)


def test_prepare_memory_value_fact_confidence_persisted_as_null(initdb):
    """端到端：fact_confidence=None 通过 set_memory → 读回 None（SQL NULL）。"""
    database.set_memory({
        "id": "prep_fc_none",
        "content": "no confidence known",
        "status": "active",
        "fact_confidence": None,
    })
    got = database.get_memory("prep_fc_none")
    assert got["fact_confidence"] is None, (
        f"fact_confidence=None must persist as SQL NULL, got {got['fact_confidence']!r}"
    )


# ════════════════════════════════════════════
# Section B: _set_memory_in_tx golden 覆盖 (Codex batch #3 L)
# ════════════════════════════════════════════

def test_set_memory_created_at_preserved_across_upsert(initdb):
    """created_at: 首次插入用 excluded；后续 UPSERT 保留原值。"""
    database.set_memory({
        "id": "gold_ca", "content": "first",
        "status": "active",
        "created_at": "2020-01-01T00:00:00+00:00",
    })
    first = database.get_memory("gold_ca")
    assert first["created_at"] == "2020-01-01T00:00:00+00:00"

    # second UPSERT passes a *different* created_at — must NOT overwrite
    database.set_memory({
        "id": "gold_ca", "content": "updated",
        "status": "active",
        "created_at": "2030-06-06T00:00:00+00:00",
    })
    second = database.get_memory("gold_ca")
    assert second["created_at"] == "2020-01-01T00:00:00+00:00", (
        f"created_at must be preserved on re-upsert, got {second['created_at']!r}"
    )
    assert second["content"] == "updated"


def test_set_memory_client_request_id_preserved_when_empty(initdb):
    """client_request_id: 已有值时空 UPSERT 不清空。"""
    database.set_memory({
        "id": "gold_crq", "content": "first", "status": "active",
        "client_request_id": "req_abc",
    })
    assert database.get_memory("gold_crq")["client_request_id"] == "req_abc"

    # activation touch / comment append style: doesn't pass crq
    database.set_memory({
        "id": "gold_crq", "content": "no crq passed", "status": "active",
        "client_request_id": "",
    })
    assert database.get_memory("gold_crq")["client_request_id"] == "req_abc", (
        "client_request_id must be preserved when new value is empty"
    )


def test_set_memory_finalize_claim_preserved_when_empty(initdb):
    """finalize_claim_id / finalize_claim_at: preserve on empty (round-6 fix)。"""
    database.set_memory({
        "id": "gold_fc", "content": "claimed", "status": "pending",
        "finalize_claim_id": "claim_token_xyz",
        "finalize_claim_at": "2026-01-01T00:00:00+00:00",
    })
    first = database.get_memory("gold_fc")
    assert first["finalize_claim_id"] == "claim_token_xyz"
    assert first["finalize_claim_at"] == "2026-01-01T00:00:00+00:00"

    # generic UPSERT (no claim fields) must NOT clobber
    database.set_memory({
        "id": "gold_fc", "content": "unrelated update", "status": "pending",
    })
    second = database.get_memory("gold_fc")
    assert second["finalize_claim_id"] == "claim_token_xyz"
    assert second["finalize_claim_at"] == "2026-01-01T00:00:00+00:00"


def test_set_memory_link_to_real_id_preserved_when_empty(initdb):
    """link_to_real_id: 骨架 replaced 后不能被空 UPSERT 清掉。"""
    database.set_memory({
        "id": "gold_link", "content": "skeleton", "status": "replaced",
        "link_to_real_id": "real_mem_xxx",
    })
    assert database.get_memory("gold_link")["link_to_real_id"] == "real_mem_xxx"

    database.set_memory({
        "id": "gold_link", "content": "again", "status": "replaced",
        "link_to_real_id": "",
    })
    assert database.get_memory("gold_link")["link_to_real_id"] == "real_mem_xxx"


def test_set_memory_embedding_coalesce_preserves_stored_bytes(initdb):
    """embedding: 已有向量时新 UPSERT 传 None 不能清掉。"""
    emb = b"\x01" * (EMBEDDING_DIM * 4)
    database.set_memory({
        "id": "gold_emb", "content": "vec test", "status": "active",
        "embedding": emb,
    })
    got = database.get_memory("gold_emb")
    assert got["embedding"] == emb

    # generic UPSERT without embedding
    database.set_memory({
        "id": "gold_emb", "content": "touched", "status": "active",
    })
    got2 = database.get_memory("gold_emb")
    assert got2["embedding"] == emb, "embedding must not be clobbered by NULL UPSERT"
    assert got2["content"] == "touched"


def test_set_memory_vec_index_new_entry(initdb):
    """vec 首次插入：vec_id_map + memories_vec 都建立映射。"""
    emb = b"\x02" * (EMBEDDING_DIM * 4)
    database.set_memory({
        "id": "gold_vec_new", "content": "vec new", "status": "active",
        "embedding": emb,
    })
    conn = database._get_read_conn()
    map_row = conn.execute(
        "SELECT vec_rowid FROM vec_id_map WHERE memory_id = ?", ("gold_vec_new",)
    ).fetchone()
    assert map_row is not None
    vec_rowid = map_row[0]
    vec_row = conn.execute(
        "SELECT rowid FROM memories_vec WHERE rowid = ?", (vec_rowid,)
    ).fetchone()
    assert vec_row is not None


def test_set_memory_vec_index_update_existing(initdb):
    """vec 已存在：第二次 UPSERT 用新 embedding 更新 memories_vec 同 rowid。"""
    emb1 = b"\x03" * (EMBEDDING_DIM * 4)
    emb2 = b"\x04" * (EMBEDDING_DIM * 4)
    database.set_memory({
        "id": "gold_vec_upd", "content": "v1", "status": "active",
        "embedding": emb1,
    })
    conn = database._get_read_conn()
    rowid1 = conn.execute(
        "SELECT vec_rowid FROM vec_id_map WHERE memory_id = ?", ("gold_vec_upd",)
    ).fetchone()[0]

    database.set_memory({
        "id": "gold_vec_upd", "content": "v2", "status": "active",
        "embedding": emb2,
    })
    conn = database._get_read_conn()
    rowid2 = conn.execute(
        "SELECT vec_rowid FROM vec_id_map WHERE memory_id = ?", ("gold_vec_upd",)
    ).fetchone()[0]
    assert rowid1 == rowid2, "vec_rowid must remain stable across UPSERT"


def test_set_memory_vec_cleanup_on_no_embedding(initdb):
    """vec 存在时 UPSERT 传 valid embedding=None (dict 无 embedding 键实际走 COALESCE 保留)。
    真正的清理场景：mem dict 显式带 embedding=<invalid size or None> 且旧 mem 有映射.
    实际当前实现: dict 无 embedding 键 → COALESCE 保留旧；dict 有 embedding=None 且旧存在 → 也 COALESCE 保留。
    唯一走清理路径：dict 有 embedding=<size 不对的 bytes>（视为 invalid），触发 else 分支。
    """
    emb = b"\x05" * (EMBEDDING_DIM * 4)
    database.set_memory({
        "id": "gold_vec_clean", "content": "with vec", "status": "active",
        "embedding": emb,
    })
    conn = database._get_read_conn()
    assert conn.execute(
        "SELECT vec_rowid FROM vec_id_map WHERE memory_id = ?",
        ("gold_vec_clean",)
    ).fetchone() is not None

    # 用无效 embedding (size 不对)：走 else 清理分支
    database.set_memory({
        "id": "gold_vec_clean", "content": "invalid emb", "status": "active",
        "embedding": b"\x00\x00\x00",  # wrong size
    })
    conn = database._get_read_conn()
    map_row = conn.execute(
        "SELECT vec_rowid FROM vec_id_map WHERE memory_id = ?",
        ("gold_vec_clean",)
    ).fetchone()
    assert map_row is None, "vec_id_map entry should be cleaned when embedding invalid"


def test_set_memory_vec_write_failure_swallowed(initdb, monkeypatch):
    """vec 写失败时应仅 warning 不 raise (best-effort)。主 memory UPSERT 必须成功。"""
    emb = b"\x06" * (EMBEDDING_DIM * 4)
    original_execute = database._get_conn().execute
    call_count = {"n": 0}

    def fake_execute(sql, *args, **kwargs):
        # 让主 memories 表 UPSERT 通过；vec 相关写入模拟失败
        if isinstance(sql, str) and "memories_vec" in sql and sql.strip().upper().startswith(("INSERT", "UPDATE")):
            call_count["n"] += 1
            raise sqlite3.OperationalError("simulated vec write failure")
        return original_execute(sql, *args, **kwargs)

    # 用 ProxyConn 模式（sqlite3.Connection 属性只读，不能直接 monkeypatch）
    real_conn = database._get_conn()

    class ProxyConn:
        def execute(self, sql, *args, **kwargs):
            return fake_execute(sql, *args, **kwargs)
        def commit(self):
            return real_conn.commit()
        def executescript(self, sql):
            return real_conn.executescript(sql)

    monkeypatch.setattr(database, "_get_conn", lambda: ProxyConn())

    # 走 set_memory — 主 UPSERT 应成功，vec 写失败被 warning 吞掉
    database.set_memory({
        "id": "gold_vec_fail", "content": "vec fail", "status": "active",
        "embedding": emb,
    })
    monkeypatch.undo()

    # 主 memory 应已写入
    got = database.get_memory("gold_vec_fail")
    assert got is not None and got["content"] == "vec fail"
    # 至少触发了一次 vec 写失败
    assert call_count["n"] >= 1


# ════════════════════════════════════════════
# Section C: 跨文件枚举升级 rglob (Codex batch #3 fixup L)
# ════════════════════════════════════════════

_IN_TX_HELPERS = frozenset({'_set_memory_in_tx'})
_EXCLUDED_DIRS = {'tests', 'scripts', '__pycache__', '.venv', 'venv',
                  '.git', 'frontend', 'static-app', 'static', 'data', 'docs'}


def _enumerate_production_py_files_recursive():
    """升级版：rglob 递归找所有生产 .py，显式排除测试/脚本/缓存/前端/文档等。
    覆盖了未来任何嵌套生产子包的情况（原 iterdir 只扫单层）。
    """
    project_root = Path(__file__).parent.parent
    py_files = []
    for p in project_root.rglob("*.py"):
        # 跳过排除目录（任一父路径匹配即跳过）
        if any(part in _EXCLUDED_DIRS for part in p.parts):
            continue
        if p.name == 'database.py':
            continue
        py_files.append(p)
    return py_files


def test_batch6_rglob_enumeration_includes_root_files():
    """升级后仍能覆盖项目根目录下所有生产 py。"""
    files = _enumerate_production_py_files_recursive()
    names = {p.name for p in files}
    # 抽查几个关键生产模块必须在扫描范围内
    for expected in ('memory_ops.py', 'activity_log.py', 'main.py', 'daemon.py'):
        assert expected in names, f"{expected} missing from rglob enumeration"


def test_batch6_rglob_enumeration_excludes_scan_dirs():
    """tests/ / scripts/ / __pycache__ / frontend / docs 必须被排除。"""
    files = _enumerate_production_py_files_recursive()
    for p in files:
        for excluded in _EXCLUDED_DIRS:
            assert excluded not in p.parts, (
                f"{p} should have been excluded ({excluded} in parts)"
            )


def test_batch6_rglob_gate_catches_in_tx_helper_import_reversal():
    """反例：假 from database import _set_memory_in_tx 必被抓。"""
    fake_src = 'from database import _set_memory_in_tx\ndef bad(): _set_memory_in_tx(None, {})\n'
    tree = ast.parse(fake_src)
    found_import = any(
        isinstance(n, ast.ImportFrom)
        and any(a.name in _IN_TX_HELPERS for a in n.names)
        for n in ast.walk(tree)
    )
    assert found_import, "ImportFrom reversal not caught"


def test_batch6_rglob_gate_catches_in_tx_helper_bare_name_reversal():
    """反例：裸 Name 引用 (from database import *; _set_memory_in_tx(...)) 必被抓。"""
    fake_src = 'from database import *\ndef bad(): _set_memory_in_tx(None, {})\n'
    tree = ast.parse(fake_src)
    found_name = any(
        isinstance(n, ast.Name) and n.id in _IN_TX_HELPERS
        for n in ast.walk(tree)
    )
    assert found_name, "bare-name reversal not caught"


def test_batch6_rglob_gate_current_codebase_clean():
    """当前所有生产模块（含未来嵌套子包）均无 _IN_TX_HELPERS 引用。"""
    violations = []
    for path in _enumerate_production_py_files_recursive():
        try:
            src = path.read_text(encoding='utf-8')
        except UnicodeDecodeError:
            continue
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for a in node.names:
                    if a.name in _IN_TX_HELPERS:
                        violations.append((path.name, node.lineno, f"import {a.name!r}"))
            elif isinstance(node, ast.Attribute):
                if node.attr in _IN_TX_HELPERS:
                    violations.append((path.name, node.lineno, f".{node.attr}"))
            elif isinstance(node, ast.Name):
                if node.id in _IN_TX_HELPERS:
                    violations.append((path.name, node.lineno, f"bare {node.id}"))
    assert not violations, (
        f"in-tx helpers referenced in production code: {violations}"
    )
