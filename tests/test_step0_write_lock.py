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

# 明确豁免为"读且已知不动数据"的 SQL 起始动词
_READ_VERBS = ("SELECT", "PRAGMA", "WITH", "EXPLAIN")


def _funcs_range_by_name(tree, names):
    ranges = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            ranges.append((node.lineno, node.end_lineno))
    return ranges


def _first_str_arg(call: ast.Call) -> str | None:
    """Return the first positional argument if it's a str literal, else None."""
    if not call.args:
        return None
    a = call.args[0]
    if isinstance(a, ast.Constant) and isinstance(a.value, str):
        return a.value
    # ast.JoinedStr = f-string; ast.Name = variable — not a literal
    return None


def _is_read_only_sql(sql: str) -> bool:
    """字面量以纯读动词开头 → 视为 read-only；其它一律视为写（fail-closed）。"""
    stripped = sql.lstrip().upper()
    for v in _READ_VERBS:
        if stripped.startswith(v + " ") or stripped.startswith(v + "\n") \
                or stripped == v or stripped.startswith(v + "("):
            return True
    return False


def _build_parent_map(tree):
    """Map each node → its parent, so we can walk ancestry for lexical containment."""
    pmap = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            pmap[child] = node
    return pmap


def _is_write_tx_ctx_call(call: ast.Call) -> bool:
    fn = call.func
    if isinstance(fn, ast.Name) and fn.id == '_write_transaction':
        return True
    if isinstance(fn, ast.Attribute) and fn.attr == '_write_transaction':
        return True
    return False


def _inside_write_tx_via_ancestry(node, pmap) -> bool:
    """Walk parent chain — return True iff `node` is lexically inside a
    `with (... _write_transaction(...) ...):` body."""
    cur = pmap.get(node)
    while cur is not None:
        if isinstance(cur, (ast.With, ast.AsyncWith)):
            for item in cur.items:
                ce = item.context_expr
                if isinstance(ce, ast.Call) and _is_write_tx_ctx_call(ce):
                    return True
        cur = pmap.get(cur)
    return False


def _inside_any_func(node, pmap, names) -> bool:
    """True iff `node`'s enclosing function has name in `names`."""
    cur = pmap.get(node)
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)) and cur.name in names:
            return True
        cur = pmap.get(cur)
    return False


def _find_violations(source: str, allowed_func_names=('_write_transaction', 'init_db'),
                     pure_read_helper_names: frozenset[str] = frozenset()):
    """Fail-closed AST gate. Returns list of (line, kind, detail).

    A .commit()/.rollback()/.execute*() call violates iff NONE of:
      - lexically inside a `with database._write_transaction():` block
      - lexically inside a function named in `allowed_func_names`
      - (execute-only) lexically inside a function in `pure_read_helper_names`
        AND SQL is a str literal starting with a read verb
        (dynamic SQL in pure_read_helpers is still flagged unless enclosing
        function is on the pure-read whitelist — fail-closed on dynamic SQL
        elsewhere).
    """
    tree = ast.parse(source)
    pmap = _build_parent_map(tree)
    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not isinstance(fn, ast.Attribute):
            continue

        in_write_tx = _inside_write_tx_via_ancestry(node, pmap)
        in_allowed_func = _inside_any_func(node, pmap, allowed_func_names)
        in_pure_read = _inside_any_func(node, pmap, pure_read_helper_names)

        # 1) commit/rollback on ANY receiver — must be in ctx or allowed func
        if fn.attr in ("commit", "rollback"):
            if not (in_write_tx or in_allowed_func):
                violations.append((node.lineno, fn.attr, "any-receiver commit/rollback"))
            continue

        # 2) execute* — decide via SQL literal & enclosing context
        if fn.attr in ("execute", "executemany", "executescript"):
            if in_write_tx or in_allowed_func:
                continue
            sql = _first_str_arg(node)
            if sql is None:
                # dynamic SQL (variable / f-string) — fail-closed unless in
                # a whitelisted pure_read_helper
                if not in_pure_read:
                    violations.append((node.lineno, fn.attr, "dynamic SQL outside write ctx / pure_read helper"))
                continue
            if _is_read_only_sql(sql):
                # SELECT/PRAGMA/etc literal outside ctx — fine
                continue
            # literal SQL not clearly read → write, must be in ctx
            first_word = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else "<empty>"
            violations.append((node.lineno, fn.attr, f"non-read SQL starts {first_word}"))
    return violations


# Pure read helper 白名单（database.py 内已知的纯读函数，允许动态 SQL）
# —— 与 batch #4 _PURE_READ_HELPERS 清单同步维护
_DATABASE_PURE_READ_HELPERS = frozenset({
    'get_memory', 'get_memory_by_client_request_id', 'list_stale_intent_ledgers',
    'get_ledger', 'list_memories_by_status', 'query_memories', 'count_memories',
    'get_proposal', 'list_proposals', 'count_proposals', 'list_audits', 'count_audits',
    'get_profile', 'list_profiles', 'vector_search', 'fts_search', 'cjk_like_search',
    'get_all_memory_ids', 'get_memories_batch', 'iter_memories', 'get_person',
    'list_persons', 'get_memories_by_subject', 'count_memories_by_subject',
    'resolve_alias', 'get_all_aliases',
    # 内部读辅助
    '_get_read_conn', '_get_conn', '_row_to_dict', '_row_to_dict_no_embedding',
    '_vector_search_impl', '_fts_search_impl', '_cjk_like_search_impl',
    '_build_vec_mem_sql', '_sanitise_order_by',
    # ro_* 只读变体
    'ro_iter_memories', 'ro_vector_search', 'ro_fts_search', 'ro_cjk_like_search',
    'ro_get_memory',
    # 与 read/query 相关的持久化辅助
    '_person_row_to_dict',
})


def test_ast_gate_database_writes_only_in_ctx_and_init_db():
    """database.py fail-closed AST 闸门：
    - .commit()/.rollback()（任何 receiver）必须位于 _write_transaction() 或 init_db() 或
      lexically 在 with database._write_transaction(): 内
    - execute/executemany/executescript 若 SQL 字面量非 SELECT/PRAGMA/WITH/EXPLAIN 开头
      → 视为写，必须在同上合规位置
    - 动态 SQL（变量/f-string）在合规位置外一律违规，除非在 pure_read_helpers 白名单函数内
    """
    src = Path(database.__file__).read_text(encoding='utf-8')
    violations = _find_violations(
        src,
        allowed_func_names=('_write_transaction', 'init_db'),
        pure_read_helper_names=_DATABASE_PURE_READ_HELPERS,
    )
    assert not violations, (
        f"database.py AST gate violations: {violations}"
    )


def test_ast_gate_activity_log_no_bare_writes():
    """activity_log.py: 与 database.py 同样的 fail-closed 闸门。所有写必须通过
    database._write_transaction() 的 conn；.commit()/.rollback()/写 SQL/动态
    SQL 全在合规位置外均违规（无豁免函数，无 pure_read helper）。
    """
    src = Path(__file__).parent.parent.joinpath("activity_log.py").read_text(encoding='utf-8')
    violations = _find_violations(
        src,
        allowed_func_names=frozenset(),        # 没有豁免函数
        pure_read_helper_names=frozenset(),    # 没有 pure-read helper
    )
    assert not violations, (
        f"activity_log.py AST gate violations: {violations} — "
        f"all writes must go through database._write_transaction()"
    )


def test_memory_ops_shares_database_write_lock():
    """memory_ops._WRITE_LOCK 与 database._WRITE_LOCK 必须是同一对象。"""
    import memory_ops
    assert memory_ops._WRITE_LOCK is database._WRITE_LOCK


def test_seed_baseline_persons_concurrent_returns_actual_inserts(initdb):
    """Codex #2b M1 反例（v2 强化）：多线程并发调 seed_baseline_persons，
    sum(returns) 必须 == COUNT(*)。异常传播，len(results)==N。

    比原版加强：
    - 用 8 个 worker + ThreadPoolExecutor.submit().result() 传播异常
    - 循环 3 次，任一次假通过即失败
    - 断言 len(returns) == N 且无异常
    - 每轮结束后手动清 persons 表再重跑（模拟"多次启动竞争"）
    """
    import concurrent.futures as cf
    N = 8
    for trial in range(3):
        # 清空表以便下一轮
        with database._write_transaction() as c:
            c.execute("DELETE FROM persons")

        with cf.ThreadPoolExecutor(max_workers=N) as ex:
            futures = [ex.submit(database.seed_baseline_persons) for _ in range(N)]
            results = []
            for f in futures:
                # .result() re-raises any exception from worker
                results.append(f.result(timeout=10))

        assert len(results) == N, f"trial {trial}: expected {N} results, got {len(results)}"
        with database._write_transaction() as c:
            actual = c.execute("SELECT COUNT(*) FROM persons").fetchone()[0]
        assert sum(results) == actual, (
            f"trial {trial}: seed returned sum={sum(results)} but "
            f"{actual} rows exist (returns={results})"
        )
        # 二次调用必返 0（表已满）
        assert database.seed_baseline_persons() == 0, (
            f"trial {trial}: second call after seeding should return 0"
        )
