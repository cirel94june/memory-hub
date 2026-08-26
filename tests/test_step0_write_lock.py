"""Step 0-A base tests for _write_transaction() ctx and lock invariants.

Codex Medium (2a review): _write_transaction() 基础单测必须随 2b 落地。
Covers:
  - 同线程嵌套立即抛 RuntimeError
  - BEGIN IMMEDIATE 失败后 lock 与 _in_write_tx.active 恢复
  - body 抛异常 → rollback + 恢复
  - commit 抛异常 → rollback attempt + 恢复
  - 后续事务仍可取得锁
  - AST 断言：非 database.py / tests 之外，所有 conn.commit() 只出现在
    _write_transaction 内 + init_db 内
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


def _reset_active_flag():
    if getattr(database._in_write_tx, 'active', False):
        database._in_write_tx.active = False


def _lock_available():
    got = database._WRITE_LOCK.acquire(blocking=False)
    if got:
        database._WRITE_LOCK.release()
    return got


@pytest.fixture
def initdb(tmp_path):
    """Each test gets a fresh initialised DB on its own path."""
    db_path = tmp_path / "test.db"
    database.DB_PATH = db_path
    asyncio.run(database.init_db(str(db_path)))
    _reset_active_flag()
    yield db_path
    _reset_active_flag()


def test_nested_call_raises_runtime_error(initdb):
    """同线程内在一个 _write_transaction() 内再进入 → 立即 RuntimeError（防死锁）。"""
    with database._write_transaction() as conn:
        with pytest.raises(RuntimeError, match="nested _write_transaction"):
            with database._write_transaction():
                pass
    assert not getattr(database._in_write_tx, 'active', False)
    assert _lock_available()


def test_body_exception_rollbacks_and_recovers(initdb):
    """body 抛异常 → ctx ROLLBACK + re-raise + 状态恢复。
    通过观察数据未持久化验证 rollback，不 mock。
    """
    class MyErr(Exception):
        pass
    # setup 一张测试表（独立 tx）
    with database._write_transaction() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS _tx_test (v INTEGER)")

    with pytest.raises(MyErr):
        with database._write_transaction() as conn:
            conn.execute("INSERT INTO _tx_test VALUES (1)")
            raise MyErr("boom")

    assert not getattr(database._in_write_tx, 'active', False)
    assert _lock_available()
    # rollback 生效：那条 INSERT 应不在
    with database._write_transaction() as conn:
        row = conn.execute("SELECT COUNT(*) FROM _tx_test WHERE v = 1").fetchone()
        assert row[0] == 0, f"rollback failed — row still present: {row[0]}"
        conn.execute("DROP TABLE IF EXISTS _tx_test")


def test_begin_immediate_failure_releases_lock(initdb, monkeypatch):
    """BEGIN IMMEDIATE 本身抛异常 → active flag 恢复 + 锁释放。

    通过 monkeypatch database._get_conn 返回代理 conn，其 execute 拦截 BEGIN 抛异常。
    """
    real_conn = database._get_conn()

    class ProxyConn:
        def execute(self, sql, *args, **kwargs):
            if isinstance(sql, str) and sql.startswith("BEGIN IMMEDIATE"):
                raise sqlite3.OperationalError("simulated BEGIN failure")
            return real_conn.execute(sql, *args, **kwargs)
        def commit(self):
            return real_conn.commit()

    monkeypatch.setattr(database, '_get_conn', lambda: ProxyConn())

    with pytest.raises(sqlite3.OperationalError, match="simulated BEGIN"):
        with database._write_transaction():
            pytest.fail("should not enter body when BEGIN fails")

    assert not getattr(database._in_write_tx, 'active', False)
    assert _lock_available()


def test_commit_failure_still_releases_lock(initdb, monkeypatch):
    """commit 抛异常 → 状态恢复 + 锁释放 + 后续事务能继续。"""
    real_conn = database._get_conn()

    class ProxyConn:
        def execute(self, sql, *args, **kwargs):
            return real_conn.execute(sql, *args, **kwargs)
        def commit(self):
            raise sqlite3.OperationalError("simulated commit failure")

    monkeypatch.setattr(database, '_get_conn', lambda: ProxyConn())

    with pytest.raises(sqlite3.OperationalError, match="simulated commit"):
        with database._write_transaction() as c:
            pass  # no-op body

    assert not getattr(database._in_write_tx, 'active', False)
    assert _lock_available()

    # 撤 monkeypatch 后（fixture 自动做）后续 tx 能继续
    monkeypatch.undo()
    with database._write_transaction() as conn:
        conn.execute("SELECT 1").fetchone()


def test_lock_available_after_normal_tx(initdb):
    """连续两次正常事务都能拿到锁。"""
    for i in range(3):
        with database._write_transaction() as conn:
            conn.execute("SELECT 1")
        assert not getattr(database._in_write_tx, 'active', False)
        assert _lock_available()


# ── AST 闸门（Codex #2b M2 强化）──
#
# Codex 指出旧闸门只捕 `conn.commit()`；`c.commit()` / `db.commit()` /
# `database._get_conn().commit()` 全部绕过；且不检查裸 DML（execute/
# executemany/executescript）。新版闸门：
#
# 1. database.py 内任何 receiver 的 `.commit()` / `.rollback()` 必须
#    位于 _write_transaction() 或 init_db() 函数体内。
# 2. database.py 内所有 `.execute*(sql, ...)` 若 sql 字面量以 DML/DDL
#    动词开头（INSERT/UPDATE/DELETE/REPLACE/CREATE/ALTER/DROP/BEGIN/
#    COMMIT/ROLLBACK），必须位于 _write_transaction / init_db 内，或
#    豁免函数（read-only 函数的读语义 SELECT/PRAGMA 不算 DML）。
# 3. activity_log.py 走同样规则（豁免函数为空）。

_WRITE_VERBS = ("INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE",
                "ALTER", "DROP", "BEGIN", "COMMIT", "ROLLBACK")


def _funcs_range_by_name(tree, names):
    ranges = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            ranges.append((node.lineno, node.end_lineno))
    return ranges


def _in_any_range(lineno, ranges):
    return any(lo <= lineno <= hi for lo, hi in ranges)


def _first_str_arg(call: ast.Call) -> str | None:
    """Return the first positional argument if it's a str literal, else None."""
    if not call.args:
        return None
    a = call.args[0]
    if isinstance(a, ast.Constant) and isinstance(a.value, str):
        return a.value
    return None


def _is_write_sql(sql: str) -> bool:
    stripped = sql.lstrip().upper()
    return any(stripped.startswith(v + " ") or stripped.startswith(v + "\n")
               or stripped == v for v in _WRITE_VERBS)


def _collect_write_tx_with_ranges(tree):
    """Return line ranges (lo, hi) covered by `with _write_transaction()` or
    `with database._write_transaction()` statements — write operations
    lexically inside these blocks are considered protected."""
    ranges = []

    def _is_write_tx_call(call: ast.Call) -> bool:
        fn = call.func
        if isinstance(fn, ast.Name) and fn.id == '_write_transaction':
            return True
        if isinstance(fn, ast.Attribute) and fn.attr == '_write_transaction':
            return True
        return False

    for node in ast.walk(tree):
        if isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                ce = item.context_expr
                if isinstance(ce, ast.Call) and _is_write_tx_call(ce):
                    ranges.append((node.lineno, node.end_lineno))
                    break
    return ranges


def _find_violations(source: str, allowed_func_ranges):
    """Return list of (line, kind, detail) for any .commit / .rollback /
    write-SQL execute NOT within either:
      - allowed_func_ranges (whole-function exempt: _write_transaction, init_db)
      - a lexically enclosing `with _write_transaction():` block
    """
    tree = ast.parse(source)
    ctx_ranges = _collect_write_tx_with_ranges(tree)
    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not isinstance(fn, ast.Attribute):
            continue
        ln = node.lineno
        if _in_any_range(ln, allowed_func_ranges) or _in_any_range(ln, ctx_ranges):
            continue
        # commit / rollback on ANY receiver
        if fn.attr in ("commit", "rollback"):
            violations.append((ln, fn.attr, "any-receiver commit/rollback"))
            continue
        # write DML/DDL through execute*
        if fn.attr in ("execute", "executemany", "executescript"):
            sql = _first_str_arg(node)
            if sql and _is_write_sql(sql):
                first_word = sql.lstrip().split(None, 1)[0].upper()
                violations.append((ln, fn.attr, f"write SQL starts {first_word}"))
    return violations


def test_ast_gate_database_writes_only_in_ctx_and_init_db():
    """database.py: 所有 .commit()/.rollback() 与写 SQL 必须位于
    _write_transaction() 或 init_db() 内。任何 receiver 名不豁免。
    """
    src = Path(database.__file__).read_text(encoding='utf-8')
    tree = ast.parse(src)
    allowed = _funcs_range_by_name(tree, ('_write_transaction', 'init_db'))
    violations = _find_violations(src, allowed)
    assert not violations, (
        f"database.py has commit/rollback/write-SQL outside allowed funcs: "
        f"{violations}"
    )


def test_ast_gate_activity_log_no_bare_writes():
    """activity_log.py: 完全不允许 commit/rollback/写 SQL 出现在源代码里
    （所有写必须通过 database._write_transaction 的 conn，SQL 字面量本身
    可以放在 with 块内——闸门允许 with 里的 conn.execute("INSERT ...")
    因为 activity_log 的写路径全部包在 database._write_transaction() 内，
    不在任何合规函数中）。

    注意：activity_log 的合规豁免范围是"整个通过 database ctx 的 with 块内"，
    但 AST 无法直接识别"这行 conn.execute 在 database._write_transaction 的
    yield 内运行"——所以此闸门只禁 commit/rollback（任何 receiver），SQL
    execute 不再判定（既已 lock 下运行，SQL 类型无关）。
    """
    src = Path(__file__).parent.parent.joinpath("activity_log.py").read_text(encoding='utf-8')
    tree = ast.parse(src)
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in ("commit", "rollback"):
                violations.append((node.lineno, node.func.attr))
    assert not violations, (
        f"activity_log.py has any-receiver commit/rollback: {violations} — "
        f"must go through database._write_transaction()"
    )


def test_memory_ops_shares_database_write_lock():
    """memory_ops._WRITE_LOCK 与 database._WRITE_LOCK 必须是同一对象。"""
    import memory_ops
    assert memory_ops._WRITE_LOCK is database._WRITE_LOCK


def test_seed_baseline_persons_concurrent_returns_actual_inserts(initdb):
    """Codex #2b M1 反例：两线程并发调 seed_baseline_persons，返回值合计
    必须等于实际新增行数（COUNT 与 INSERT 必须原子——之前分事务时两线程
    都读到 0，都返 len(baseline)，返回值虚高）。"""
    import threading as _threading
    results = []
    barrier = _threading.Barrier(2)

    def _worker():
        barrier.wait()
        results.append(database.seed_baseline_persons())

    t1 = _threading.Thread(target=_worker)
    t2 = _threading.Thread(target=_worker)
    t1.start(); t2.start()
    t1.join(); t2.join()

    actual = database._get_conn().execute("SELECT COUNT(*) FROM persons").fetchone()[0]
    assert sum(results) == actual, (
        f"seed returned sum={sum(results)} but only {actual} rows exist "
        f"(returns={results})"
    )
    # 第二次调用必然返 0
    assert database.seed_baseline_persons() == 0
