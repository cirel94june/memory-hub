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


def test_ast_gate_conn_commit_only_in_ctx_and_init_db():
    """AST 闸门：database.py 内 conn.commit() 只允许出现在
    _write_transaction() 函数体和 init_db() 函数体，其余全部违规。
    """
    db_path = Path(database.__file__)
    tree = ast.parse(db_path.read_text(encoding='utf-8'))

    # 收集"合法"函数节点范围（含 async def）
    allowed_ranges = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in ('_write_transaction', 'init_db'):
            allowed_ranges.append((node.lineno, node.end_lineno))

    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Attribute) and fn.attr == 'commit':
                if isinstance(fn.value, ast.Name) and fn.value.id == 'conn':
                    ln = node.lineno
                    if not any(lo <= ln <= hi for lo, hi in allowed_ranges):
                        violations.append(ln)

    assert not violations, (
        f"database.py has conn.commit() outside _write_transaction/init_db "
        f"at lines: {violations}"
    )


def test_ast_gate_no_conn_commit_in_activity_log():
    """activity_log.py 不应有任何 conn.commit()（应通过 database._write_transaction）。"""
    al_path = Path(__file__).parent.parent / "activity_log.py"
    tree = ast.parse(al_path.read_text(encoding='utf-8'))
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Attribute) and fn.attr == 'commit':
                if isinstance(fn.value, ast.Name) and fn.value.id == 'conn':
                    violations.append(node.lineno)
    assert not violations, (
        f"activity_log.py has conn.commit() at lines: {violations} — "
        f"should go through database._write_transaction()"
    )


def test_memory_ops_shares_database_write_lock():
    """memory_ops._WRITE_LOCK 与 database._WRITE_LOCK 必须是同一对象。"""
    import memory_ops
    assert memory_ops._WRITE_LOCK is database._WRITE_LOCK
