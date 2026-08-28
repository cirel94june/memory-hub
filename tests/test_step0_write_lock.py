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

# Codex round-3 M: 只 SELECT/EXPLAIN 是无争议纯读起始动词
# WITH ... DELETE 可写；PRAGMA journal_mode=DELETE 可 checkpoint 数据；都不能一刀切放行
_READ_VERBS = ("SELECT", "EXPLAIN")

# PRAGMA 按完整语句精确白名单豁免（当前 production 需要在非 allowed func /
# 非 with ctx 位置用到的两条）
_SAFE_PRAGMA_STMTS = frozenset({
    "PRAGMA busy_timeout=200",
    "PRAGMA query_only=ON",
})


def _funcs_range_by_name(tree, names):
    ranges = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            ranges.append((node.lineno, node.end_lineno))
    return ranges


def _leading_text_of(node, pmap, seen: set | None = None) -> str | None:
    """Try to extract the leading literal string of `node` (used as SQL first arg).

    Handles:
      - ast.Constant (str)                                     → the value
      - ast.JoinedStr (f-string)                               → leading Constant parts
      - ast.Name pointing to a variable last-assigned in the
        same enclosing function to a literal / f-string        → recurse on RHS
      - ast.BinOp of `+` concatenating strings                  → left leading text

    Returns None if leading text cannot be statically extracted.
    """
    if seen is None:
        seen = set()
    node_id = id(node)
    if node_id in seen:
        return None
    seen.add(node_id)

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value

    if isinstance(node, ast.JoinedStr):
        # extract leading Constant part(s) before any FormattedValue
        parts = []
        for v in node.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                parts.append(v.value)
            else:
                break
        return ''.join(parts) if parts else None

    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _leading_text_of(node.left, pmap, seen)

    if isinstance(node, ast.Name):
        # look up ALL assignments to this name in enclosing function
        # (Codex round-4 M: 保守处理——如果任一分支赋 write SQL，整条 fail-closed)
        enc = pmap.get(node)
        while enc is not None:
            if isinstance(enc, (ast.FunctionDef, ast.AsyncFunctionDef)):
                break
            enc = pmap.get(enc)
        if enc is None:
            return None
        target_name = node.id
        candidate_rhss = []
        for stmt in ast.walk(enc):
            # 只看纯 Assign (`x = ...`)；AugAssign (`x += ...`) 是叠加不是覆盖
            if not isinstance(stmt, ast.Assign):
                continue
            if stmt.lineno >= node.lineno:
                continue
            # skip stmts inside nested func
            p = pmap.get(stmt)
            skip = False
            while p is not None and p is not enc:
                if isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                    skip = True
                    break
                p = pmap.get(p)
            if skip:
                continue
            for tgt in stmt.targets:
                if isinstance(tgt, ast.Name) and tgt.id == target_name:
                    candidate_rhss.append(stmt.value)
        if not candidate_rhss:
            return None
        # Resolve leading text for every candidate. If any is None → unresolvable
        # → return None (caller treats as dynamic/fail-closed). If all resolve to
        # safe read prefix, return the FIRST candidate's leading text (they're
        # all read; caller just needs to see a read verb).
        resolved = [_leading_text_of(rhs, pmap, seen.copy()) for rhs in candidate_rhss]
        if any(r is None for r in resolved):
            return None
        # All resolved. If ANY is non-read, caller must fail-closed → return
        # the first non-read one so caller sees it as write.
        for r in resolved:
            if not _is_read_only_sql(r):
                return r
        # All safe reads → return any (first works)
        return resolved[0]

    return None


def _first_str_arg(call: ast.Call, pmap=None) -> str | None:
    """Return leading text of the first positional arg, or None if not
    statically extractable. Uses _leading_text_of for name/f-string handling."""
    if not call.args:
        return None
    if pmap is None:
        return None if not (
            isinstance(call.args[0], ast.Constant)
            and isinstance(call.args[0].value, str)
        ) else call.args[0].value
    return _leading_text_of(call.args[0], pmap)


def _is_read_only_sql(sql: str) -> bool:
    """严格判定：只允许 SELECT/EXPLAIN 起始，或精确 whitelist 的 PRAGMA。
    WITH/PRAGMA 一律不放行（Codex round-3 M）。"""
    stripped = sql.strip()
    stripped_upper = stripped.upper()
    # exact PRAGMA whitelist
    if stripped_upper.startswith("PRAGMA"):
        return stripped in _SAFE_PRAGMA_STMTS
    # SELECT / EXPLAIN only
    for v in _READ_VERBS:
        if stripped_upper.startswith(v + " ") or stripped_upper.startswith(v + "\n") \
                or stripped_upper == v or stripped_upper.startswith(v + "("):
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


def _is_func_boundary(node) -> bool:
    """FunctionDef/AsyncFunctionDef/Lambda 界定一个新的执行边界——嵌套
    函数体不属于外层 with 的动态作用域（函数被调用时才执行）。"""
    return isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))


def _inside_write_tx_via_ancestry(node, pmap) -> bool:
    """True iff `node` is lexically inside a `with (... _write_transaction(...) ...):`
    body — 遇到嵌套函数边界立即停止（Codex round-3 M4）。"""
    cur = pmap.get(node)
    while cur is not None:
        if _is_func_boundary(cur):
            return False
        if isinstance(cur, (ast.With, ast.AsyncWith)):
            for item in cur.items:
                ce = item.context_expr
                if isinstance(ce, ast.Call) and _is_write_tx_ctx_call(ce):
                    return True
        cur = pmap.get(cur)
    return False


def _enclosing_func_name(node, pmap) -> str | None:
    """Return immediately enclosing FunctionDef/AsyncFunctionDef name (skip Lambda),
    or None if at module level. 嵌套定义只返回最内层函数。"""
    cur = pmap.get(node)
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cur.name
        cur = pmap.get(cur)
    return None


def _find_violations(source: str, allowed_func_names=('_write_transaction', 'init_db'),
                     pure_read_helper_names: frozenset[str] = frozenset()):
    """Fail-closed AST gate. Returns list of (line, kind, detail).

    A .commit()/.rollback()/.execute*() call violates iff NONE of:
      - lexically inside a `with database._write_transaction():` block
        (ancestry stops at nested function boundaries — Codex round-3 M4)
      - **immediately** enclosing function is in `allowed_func_names`
        (so a nested closure inside `_write_transaction` body is NOT auto-allowed)
      - (execute-only, literal SQL) SQL is str literal starting with SELECT/EXPLAIN
        or in _SAFE_PRAGMA_STMTS exact whitelist

    Dynamic SQL (variable / f-string / concatenation) always violates outside
    ctx or allowed func — pure_read_helper_names 白名单**已废弃**（Codex
    round-3: 动态 SQL 中可藏 DELETE，等批次 #4 全部切 query_only=ON 读连接
    再放行）。
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
        enc_func = _enclosing_func_name(node, pmap)
        in_allowed_func = enc_func in allowed_func_names

        # 1) commit/rollback on ANY receiver — must be in ctx or allowed func
        if fn.attr in ("commit", "rollback"):
            if not (in_write_tx or in_allowed_func):
                violations.append((node.lineno, fn.attr, "any-receiver commit/rollback"))
            continue

        # 2) executescript — Codex round-5 M：executescript 会**隐式提交**
        #    当前事务，导致 _write_transaction() 的 rollback 无法撤销之前的写。
        #    只允许 init_db 内使用（那里没有嵌套事务需要保护）。
        if fn.attr == "executescript":
            if enc_func != "init_db":
                violations.append((node.lineno, fn.attr,
                                   "executescript() only allowed in init_db "
                                   "(implicit commit breaks _write_transaction atomicity)"))
            continue

        # 3) execute/executemany — decide via SQL literal & enclosing context
        if fn.attr in ("execute", "executemany"):
            if in_write_tx or in_allowed_func:
                continue
            sql = _first_str_arg(node, pmap)
            if sql is None:
                # 无法静态提取 SQL 起始文本 → fail-closed
                violations.append((node.lineno, fn.attr,
                                   "dynamic SQL outside write ctx / allowed func"))
                continue
            if _is_read_only_sql(sql):
                continue
            first_word = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else "<empty>"
            violations.append((node.lineno, fn.attr, f"non-read SQL starts {first_word}"))
    return violations


# Codex round-3 已废弃 pure_read helper 白名单——动态 SQL 一律 fail-closed。
# 批次 #4 全部切到 query_only=ON 读连接后再考虑是否重新引入。


# Explicit in-tx helper list（Step 0-A #3 起的命名约定）—— 显式维护而非自动
# 收集，避免"命名恰好带 _in_tx 就被无条件放行"的越权。每新增一个 in-tx
# helper 必须同时更新本清单 + 保证所有调用点都在 with _write_transaction()
# 内（由 test_ast_gate_in_tx_helpers_only_called_inside_write_ctx 兜底）。
_IN_TX_HELPERS = frozenset({
    '_set_memory_in_tx',
})


def test_ast_gate_database_writes_only_in_ctx_and_init_db():
    """database.py fail-closed 闸门。允许函数：
      - _write_transaction    ctx 本身
      - init_db               schema 初始化
      - _IN_TX_HELPERS 里的每个 in-tx 内部 helper（契约上 caller 已开 tx）
    """
    src = Path(database.__file__).read_text(encoding='utf-8')
    allowed = ('_write_transaction', 'init_db') + tuple(_IN_TX_HELPERS)
    violations = _find_violations(src, allowed_func_names=allowed)
    assert not violations, f"database.py AST gate violations: {violations}"


def test_ast_gate_in_tx_helpers_only_called_inside_write_ctx_in_database():
    """database.py 内部：_IN_TX_HELPERS 只允许在
      - with _write_transaction(): 块内
      - 另一个 _IN_TX_HELPERS 函数体内（in-tx 可级联）
    """
    src = Path(database.__file__).read_text(encoding='utf-8')
    tree = ast.parse(src)
    pmap = _build_parent_map(tree)
    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.id if isinstance(fn, ast.Name) else (
            fn.attr if isinstance(fn, ast.Attribute) else None
        )
        if name not in _IN_TX_HELPERS:
            continue
        in_ctx = _inside_write_tx_via_ancestry(node, pmap)
        enc_name = _enclosing_func_name(node, pmap)
        in_another_in_tx = enc_name in _IN_TX_HELPERS
        if not (in_ctx or in_another_in_tx):
            violations.append((node.lineno, name,
                               f"called outside _write_transaction() ctx / other in-tx helper "
                               f"(enclosing func: {enc_name!r})"))
    assert not violations, f"database.py in-tx helper misuse: {violations}"


# Codex round-2 M: 跨文件闸门 — _IN_TX_HELPERS 只允许 database.py 内部使用
# 其他生产模块出现 `database._set_memory_in_tx(...)` 或
# `from database import _set_memory_in_tx` 一律违规（会绕过写锁）
_PRODUCTION_MODULES_TO_SCAN_FOR_IN_TX_MISUSE = None  # 运行时扫描 project root


def _enumerate_production_py_files():
    """列出项目根目录下所有生产 .py 文件（排除 tests/、scripts/、__pycache__、
    database.py 自身、docs/、frontend/ 等静态资源目录）。"""
    project_root = Path(__file__).parent.parent
    # 只扫模块根一层的 .py（当前项目结构：所有生产 py 都在根目录）
    py_files = []
    for p in project_root.iterdir():
        if not p.is_file() or p.suffix != '.py':
            continue
        if p.name == 'database.py':
            continue
        py_files.append(p)
    return py_files


def test_ast_gate_in_tx_helpers_forbidden_outside_database():
    """Codex round-2 M: 跨文件契约 — _IN_TX_HELPERS 不允许被 database.py 之外的
    任何生产模块调用（会绕过写锁 + 事务保证）。

    违规形式：
      - `database._set_memory_in_tx(...)` 或 `db._set_memory_in_tx(...)` Attribute 调用
      - `from database import _set_memory_in_tx` 导入语句
      - `import database as X` 后 `X._set_memory_in_tx(...)`

    简化检测：AST 扫每个非 database.py 生产文件，禁止：
      1. 任何名字与 _IN_TX_HELPERS 元素相同的 attr 引用（Attribute 或 Name）
      2. 任何 from ... import 里出现 _IN_TX_HELPERS 元素
    """
    violations = []
    for path in _enumerate_production_py_files():
        try:
            src = path.read_text(encoding='utf-8')
        except UnicodeDecodeError:
            continue
        tree = ast.parse(src)
        for node in ast.walk(tree):
            # import statements
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name in _IN_TX_HELPERS:
                        violations.append((path.name, node.lineno,
                                           f"imports {alias.name!r} from {node.module!r}"))
            # attribute access (database._set_memory_in_tx, db._set_memory_in_tx, etc.)
            elif isinstance(node, ast.Attribute):
                if node.attr in _IN_TX_HELPERS:
                    violations.append((path.name, node.lineno,
                                       f"attribute reference to {node.attr!r}"))
            # bare Name reference (only meaningful if imported — caught by ImportFrom above,
            # but also check direct Name usage for imports like `from database import *`)
            elif isinstance(node, ast.Name):
                if node.id in _IN_TX_HELPERS:
                    violations.append((path.name, node.lineno,
                                       f"bare name reference to {node.id!r} (possibly star-import)"))
    assert not violations, (
        f"in-tx helpers referenced outside database.py: {violations}"
    )


# ── Batch #4: 26 pure-read helpers 迁 _get_read_conn + DB_PATH 失效 ──

_PURE_READ_HELPERS_LIST = (
    'get_memory', 'get_memory_by_client_request_id', 'list_stale_intent_ledgers',
    'get_ledger', 'list_memories_by_status', 'query_memories', 'count_memories',
    'get_proposal', 'list_proposals', 'count_proposals', 'list_audits', 'count_audits',
    'get_profile', 'list_profiles', 'vector_search', 'fts_search', 'cjk_like_search',
    'get_all_memory_ids', 'get_memories_batch', 'iter_memories', 'get_person',
    'list_persons', 'get_memories_by_subject', 'count_memories_by_subject',
    'resolve_alias', 'get_all_aliases',
)


def test_ast_gate_pure_read_helpers_use_read_conn():
    """Batch #4: 26 个 pure-read helpers 必须使用 _get_read_conn()（不再 _get_conn()）。

    AST 扫每个函数体，禁止 _get_conn() 调用，允许 _get_read_conn() 调用。
    """
    src = Path(database.__file__).read_text(encoding='utf-8')
    tree = ast.parse(src)
    pmap = _build_parent_map(tree)

    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        # 只关心 _get_conn()（不含属性访问）
        if isinstance(fn, ast.Name) and fn.id == '_get_conn':
            enc = _enclosing_func_name(node, pmap)
            if enc in _PURE_READ_HELPERS_LIST:
                violations.append((node.lineno, enc, "pure-read helper uses _get_conn()"))

    assert not violations, (
        f"pure-read helpers must use _get_read_conn(), not _get_conn(): {violations}"
    )


def test_ast_gate_pure_read_helpers_all_present():
    """确保 26 个 pure-read helpers 确实定义在 database.py（防止清单漂移）。"""
    src = Path(database.__file__).read_text(encoding='utf-8')
    tree = ast.parse(src)
    defined = {
        n.name for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    missing = [n for n in _PURE_READ_HELPERS_LIST if n not in defined]
    assert not missing, f"pure-read helpers missing from database.py: {missing}"


def test_init_db_failed_swap_leaves_old_state_intact(tmp_path):
    """Codex #4 round-2 Medium regression: if init_db(new_path) fails partway
    through setup, DB_PATH / _conn / read helpers must all still point at the
    original DB. Prior implementation eagerly assigned DB_PATH before the new
    conn was fully initialised → failure state left DB_PATH pointing at bad
    path while _conn still held old conn (write A, read fails on B).
    """
    import asyncio as _asyncio

    db_a = tmp_path / "a.db"
    _asyncio.run(database.init_db(str(db_a)))
    database.set_memory({"id": "row_a", "content": "in DB A"})
    assert database.get_memory("row_a") is not None

    old_db_path = database.DB_PATH
    old_conn = database._conn

    # Point at a bad path (parent dir doesn't exist → connect will fail on
    # first PRAGMA or actual disk touch)
    bad_path = tmp_path / "does_not_exist_dir" / "b.db"

    with pytest.raises(Exception):
        _asyncio.run(database.init_db(str(bad_path)))

    # Post-condition: DB_PATH untouched, _conn untouched, A still accessible
    assert database.DB_PATH == old_db_path, (
        f"DB_PATH changed on failed init: {database.DB_PATH} vs {old_db_path}"
    )
    assert database._conn is old_conn, "_conn replaced despite failed init"

    # Both write and read on A still function correctly
    database.set_memory({"id": "row_a_after", "content": "still writable"})
    got = database.get_memory("row_a")
    assert got is not None and got["content"] == "in DB A"
    got2 = database.get_memory("row_a_after")
    assert got2 is not None and got2["content"] == "still writable"


def test_init_db_syncs_global_db_path(tmp_path):
    """Codex #4 High regression: `init_db(path_b)` must update the module-level
    DB_PATH so `_get_read_conn()` sees the new path. Without this fix, write
    would go to B but read helpers keep hitting A ——scripts/supersede_old_profiles.py
    passes db_path via env var and immediately calls read helpers, hitting the bug.
    """
    import asyncio as _asyncio

    db_a = tmp_path / "a.db"
    db_b = tmp_path / "b.db"

    # setup A explicitly
    database.DB_PATH = db_a
    _asyncio.run(database.init_db(str(db_a)))
    database.set_memory({"id": "row_a", "content": "in DB A"})
    assert database.get_memory("row_a") is not None

    # switch to B via init_db(path_b) ONLY (no manual DB_PATH mutation)
    database._conn = None
    _asyncio.run(database.init_db(str(db_b)))

    # Post-condition 1: DB_PATH module global must now be db_b
    assert str(database.DB_PATH) == str(db_b), (
        f"init_db(db_b) did not sync DB_PATH: got {database.DB_PATH}"
    )

    # Post-condition 2: read helpers must operate against B
    database.set_memory({"id": "row_b", "content": "in DB B"})
    got_a = database.get_memory("row_a")
    got_b = database.get_memory("row_b")
    assert got_a is None, "read helper still hitting DB A — DB_PATH not synced"
    assert got_b is not None and got_b["content"] == "in DB B"


def test_read_conn_invalidates_on_db_path_switch(tmp_path):
    """M2: 同线程先读 DB A → 切 DB_PATH → 再读应看到 DB B 的数据。"""
    import asyncio as _asyncio

    db_a = tmp_path / "a.db"
    db_b = tmp_path / "b.db"

    # init A + 用 set_memory 插入 marker A
    database.DB_PATH = db_a
    _asyncio.run(database.init_db(str(db_a)))
    database.set_memory({"id": "marker_a", "content": "in DB A"})

    # 首次读：应该看到 A
    got_a = database.get_memory("marker_a")
    assert got_a is not None and got_a["content"] == "in DB A"

    # 切到 B（需要重置 _conn 因为它是全局单例）
    database._conn = None
    database.DB_PATH = db_b
    _asyncio.run(database.init_db(str(db_b)))
    database.set_memory({"id": "marker_b", "content": "in DB B"})

    # 同线程再读 —— read_conn 应已失效重建，看到 B 的数据
    got_a_after = database.get_memory("marker_a")
    got_b = database.get_memory("marker_b")
    assert got_a_after is None, "should NOT see DB A rows after switch"
    assert got_b is not None and got_b["content"] == "in DB B"


def test_close_thread_read_conn_releases_fd(initdb):
    """M2: close_thread_read_conn 关闭当前线程 read_conn；再次调 _get_read_conn 会重建。"""
    conn1 = database._get_read_conn()
    assert conn1 is not None
    # verify it's actually usable
    row = conn1.execute("SELECT 1").fetchone()
    assert row[0] == 1

    database.close_thread_read_conn()
    assert getattr(database._local, 'read_conn', None) is None
    assert getattr(database._local, 'read_db_path', None) is None

    # subsequent get should rebuild — different object
    conn2 = database._get_read_conn()
    assert conn2 is not None
    assert conn2 is not conn1, "expected a fresh connection after close"

    # cleanup for other tests
    database.close_thread_read_conn()


def test_ast_gate_in_tx_helpers_forbidden_outside_database_reversal():
    """反例 sanity check：把一个假 memory_ops.py 内容传给同一 AST 逻辑，必须抓住。"""
    fake_src = '''
import database
def bad():
    conn = database._get_conn()
    database._set_memory_in_tx(conn, {"id": "x"})
'''
    # inline 复用与上面 test 一致的检测逻辑
    tree = ast.parse(fake_src)
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in _IN_TX_HELPERS:
            found = True
            break
    assert found, "reversal case not caught — cross-file gate is toothless"


def test_ast_gate_activity_log_no_bare_writes():
    """activity_log.py: 同样的 fail-closed 闸门。无豁免函数。"""
    src = Path(__file__).parent.parent.joinpath("activity_log.py").read_text(encoding='utf-8')
    violations = _find_violations(src, allowed_func_names=frozenset())
    assert not violations, (
        f"activity_log.py AST gate violations: {violations} — "
        f"all writes must go through database._write_transaction()"
    )


# ── Codex round-3 M: 4 类绕过的正式反例测试 ──

def test_ast_gate_catches_cte_wrapping_write():
    """WITH ... DELETE FROM x → 必须被识别为写。"""
    src = '''
def bad():
    conn.execute("WITH doomed AS (SELECT id FROM memories) DELETE FROM memories")
'''
    v = _find_violations(src, allowed_func_names=())
    assert any('WITH' in d for _, _, d in v), f"failed to catch CTE-wrapped DELETE: {v}"


def test_ast_gate_catches_write_pragma():
    """PRAGMA journal_mode=DELETE / 其它写 PRAGMA 不在白名单 → 视为写。"""
    src = '''
def bad():
    conn.execute("PRAGMA journal_mode=DELETE")
'''
    v = _find_violations(src, allowed_func_names=())
    assert any('PRAGMA' in d for _, _, d in v), f"failed to catch write PRAGMA: {v}"


def test_ast_gate_catches_variable_dml_via_leading_resolution():
    """变量赋值的 DELETE 通过 name resolution 追回 → 视为 non-read SQL 命中。"""
    src = '''
def query_memories():
    sql = "DELETE FROM memories"
    conn.execute(sql)
'''
    v = _find_violations(src, allowed_func_names=())
    assert any('DELETE' in d for _, _, d in v), (
        f"failed to catch variable-DELETE via leading text resolution: {v}"
    )


def test_ast_gate_catches_truly_unresolvable_dynamic_sql():
    """无法静态提取起始文本（例如 f-string 首部本身是 FormattedValue）→ fail-closed。"""
    src = '''
def bad():
    tbl = "memories"
    conn.execute(f"{tbl}: DELETE FROM x")  # 首部即 FormattedValue，无法提取
'''
    v = _find_violations(src, allowed_func_names=())
    assert any('dynamic SQL' in d for _, _, d in v), (
        f"failed to catch unresolvable dynamic SQL: {v}"
    )


def test_ast_gate_ancestry_stops_at_function_boundary():
    """嵌套函数体内的 execute 不能被外层 with 保护——ancestry 应在函数边界停止。"""
    src = '''
def outer():
    with _write_transaction() as conn:
        def nested():
            conn.execute("DELETE FROM memories")  # 定义在 with 内但调用时不一定
        nested_ref = nested
    nested_ref()  # 调用在 with 外 → conn.execute 无 tx 保护
'''
    v = _find_violations(src, allowed_func_names=())
    assert any('DELETE' in d for _, _, d in v), (
        f"failed to catch nested-function DELETE outside real ctx: {v}"
    )


def test_ast_gate_catches_branch_reassignment_dml():
    """sql = "DELETE"; if flag: sql = "SELECT"; conn.execute(sql)
    当 flag=False 时执行 DELETE。保守分析必须视全部分支：任一分支非 read → fail-closed。
    """
    src = '''
def bad(flag):
    sql = "DELETE FROM memories"
    if flag:
        sql = "SELECT 1"
    conn.execute(sql)
'''
    v = _find_violations(src, allowed_func_names=())
    assert any('DELETE' in d or 'dynamic' in d for _, _, d in v), (
        f"failed to catch branch-reassignment DELETE: {v}"
    )


def test_ast_gate_catches_if_else_write_branch():
    """if/else 分别赋 DELETE / SELECT，任一分支非 read → 违规。"""
    src = '''
def bad(flag):
    if flag:
        sql = "DELETE FROM memories"
    else:
        sql = "SELECT 1"
    conn.execute(sql)
'''
    v = _find_violations(src, allowed_func_names=())
    assert any('DELETE' in d or 'dynamic' in d for _, _, d in v), (
        f"failed to catch if/else write branch: {v}"
    )


def test_ast_gate_catches_executescript_with_dml():
    """executescript("SELECT 1; DELETE ...") 首词是 SELECT 但脚本内含 DELETE。
    executescript 只允许 init_db 内使用，其它一律违规不做首词分析。
    """
    src = '''
def bad():
    conn.executescript("SELECT 1; DELETE FROM memories;")
'''
    v = _find_violations(src, allowed_func_names=())
    assert any('executescript' in kind or 'executescript' in d
               for _, kind, d in v), (
        f"failed to catch executescript with DML: {v}"
    )


def test_ast_gate_catches_executescript_inside_write_transaction():
    """Codex round-5 M: executescript 会隐式提交当前事务，即使被
    with _write_transaction(): 包裹也不安全——事务里之前的写会被提前
    commit，ctx 的 rollback 无法撤销。只有 init_db 内允许（那里没有
    需要保护的嵌套事务）。
    """
    src = '''
def bad():
    with _write_transaction() as conn:
        conn.execute("INSERT INTO memories VALUES (?)", ("x",))
        conn.executescript("SELECT 1;")  # 隐式提交上面 INSERT，rollback 无用
'''
    v = _find_violations(src, allowed_func_names=('_write_transaction',))
    assert any('executescript' in kind or 'executescript' in d
               for _, kind, d in v), (
        f"failed to catch executescript inside _write_transaction: {v}"
    )


def test_ast_gate_allows_executescript_only_in_init_db():
    """init_db 内的 executescript 允许（schema 初始化专用位置）。"""
    src = '''
def init_db():
    conn.executescript(_SCHEMA_MAIN)
'''
    v = _find_violations(src, allowed_func_names=('init_db',))
    assert not v, f"init_db's executescript should pass: {v}"


def test_ast_gate_allows_safe_read_verbs():
    """SELECT / EXPLAIN / 白名单 PRAGMA 在合规位置外应放行。"""
    src = '''
def read_only():
    conn.execute("SELECT * FROM x")
    conn.execute("EXPLAIN SELECT 1")
    conn.execute("PRAGMA busy_timeout=200")
    conn.execute("PRAGMA query_only=ON")
'''
    v = _find_violations(src, allowed_func_names=())
    assert not v, f"unexpectedly flagged safe reads: {v}"


def test_memory_ops_shares_database_write_lock():
    """memory_ops._WRITE_LOCK 与 database._WRITE_LOCK 必须是同一对象。"""
    import memory_ops
    assert memory_ops._WRITE_LOCK is database._WRITE_LOCK


def test_set_memory_in_tx_extracted_helper(initdb):
    """Step 0-A #3: _set_memory_in_tx accepts an already-open conn, does
    NOT self-acquire the lock or commit. Full set_memory() semantics preserved.

    Golden checks:
      - _set_memory_in_tx works inside an outer _write_transaction()
      - set_memory public wrapper still upserts identically
      - upsert twice with same id → row count == 1 (idempotent identity)
      - _preserve_on_empty semantics still enforced (empty crq keeps existing)
    """
    # 1) _set_memory_in_tx directly, inside caller's tx
    with database._write_transaction() as conn:
        database._set_memory_in_tx(conn, {
            "id": "mem_step3_a",
            "content": "hello step3",
            "layer": "shared",
            "room": "living_room",
            "importance": 0.5,
        })
        # inside same tx we can read what we just wrote
        row = conn.execute("SELECT id, content FROM memories WHERE id = ?",
                           ("mem_step3_a",)).fetchone()
        assert row is not None and row[1] == "hello step3"

    # 2) set_memory public wrapper works
    database.set_memory({
        "id": "mem_step3_b",
        "content": "via public wrapper",
        "importance": 0.7,
    })
    got = database.get_memory("mem_step3_b")
    assert got is not None and got["content"] == "via public wrapper"

    # 3) idempotent upsert
    database.set_memory({"id": "mem_step3_b", "content": "updated", "importance": 0.9})
    with database._write_transaction() as conn:
        cnt = conn.execute("SELECT COUNT(*) FROM memories WHERE id = ?",
                           ("mem_step3_b",)).fetchone()[0]
    assert cnt == 1, f"expected 1 row after upsert, got {cnt}"

    # 4) _preserve_on_empty: existing crq not clobbered by empty
    database.set_memory({
        "id": "mem_step3_c",
        "content": "first",
        "client_request_id": "crq_kept",
    })
    database.set_memory({
        "id": "mem_step3_c",
        "content": "second",
        "client_request_id": "",  # empty must not clobber
    })
    kept = database.get_memory("mem_step3_c")
    assert kept["client_request_id"] == "crq_kept", (
        f"client_request_id should be preserved: got {kept['client_request_id']!r}"
    )
    assert kept["content"] == "second"


def test_set_memory_public_wrapper_uses_in_tx_helper(initdb):
    """Static check: set_memory body must contain a call to _set_memory_in_tx
    (i.e. the wrapper never duplicates upsert logic). Guards against future
    regression where someone re-inlines upsert into set_memory and forgets
    to update _set_memory_in_tx alongside.
    """
    src = Path(database.__file__).read_text(encoding='utf-8')
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == 'set_memory':
            calls = [n for n in ast.walk(node) if isinstance(n, ast.Call)]
            names = [(n.func.id if isinstance(n.func, ast.Name) else
                      n.func.attr if isinstance(n.func, ast.Attribute) else None)
                     for n in calls]
            assert '_set_memory_in_tx' in names, (
                f"set_memory wrapper must call _set_memory_in_tx; call names: {names}"
            )
            return
    pytest.fail("set_memory function not found in database.py")


def test_seed_baseline_persons_concurrent_returns_actual_inserts(initdb, monkeypatch):
    """Codex round-3 L: 确定性复现旧 bug 的 seed 并发测试。

    monkeypatch _write_transaction，让所有 worker 在进入事务前经过 barrier。
    - 新实现（COUNT+INSERT 同事务）：barrier 后串行拿锁，第一个 COUNT=0 后 INSERT，
      其余 COUNT=4 返 0，sum == COUNT(*) == 4。
    - 旧实现（假想 COUNT 在 tx 外）：所有 worker COUNT=0 完成 → barrier → 依次 INSERT
      → 每个返 len(baseline)，sum = N * 4 > actual = 4，deterministic fail。
    """
    import concurrent.futures as cf
    import contextlib as _cl
    import threading as _th

    N = 4
    barrier = _th.Barrier(N)
    original_ctx = database._write_transaction

    @_cl.contextmanager
    def _barrier_before_ctx():
        # 所有 worker 在进入真正的 tx（拿写锁）前先同步一次
        try:
            barrier.wait(timeout=10)
        except _th.BrokenBarrierError:
            pass
        with original_ctx() as conn:
            yield conn

    # 清空 persons 以便测试基线
    with original_ctx() as c:
        c.execute("DELETE FROM persons")

    monkeypatch.setattr(database, '_write_transaction', _barrier_before_ctx)

    with cf.ThreadPoolExecutor(max_workers=N) as ex:
        futures = [ex.submit(database.seed_baseline_persons) for _ in range(N)]
        results = []
        for f in futures:
            results.append(f.result(timeout=15))

    assert len(results) == N, f"expected {N} results, got {len(results)}"

    monkeypatch.undo()
    with original_ctx() as c:
        actual = c.execute("SELECT COUNT(*) FROM persons").fetchone()[0]
    assert sum(results) == actual, (
        f"seed returned sum={sum(results)} but {actual} rows exist "
        f"(returns={results}) — 若这条挂，说明 COUNT/INSERT 又变成分事务"
    )
    # 二次调用必返 0
    assert database.seed_baseline_persons() == 0
