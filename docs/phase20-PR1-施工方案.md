# Phase 2.0 PR1 施工方案 v2.2 · Data Health

> 分支：本文推送分支 `phase20/pr1-plan-v3`（doc-only）；开工分支 `phase20/pr1-data-health`（v2.2 pass 后开）
> 依赖：Phase 1.7 全部合并（main 至 `604bc72`）
>
> **版本历史**：
> - v1（作废）：分析假设错误 4 处
> - v2：Codex 审后收敛 2C+6H+6M
> - v2.1（作废）：user 一审后修 H3/H1，但 H3 仍未闭环、State supersede 非原子、总方案未同步
> - **v2.2**（本文）：user 二审后修 5 High + 7 Medium

---

## v2.1 → v2.2 变更总览

| 序 | 项 | 类型 |
|---|---|---|
| 1 | 共享连接**所有**写路径统一走内部 `_write_transaction()` context manager，锁不导出 | High |
| 2 | 嵌套锁 fail-fast（threading.local sentinel + RuntimeError），删除 v2.1 的"wait_for 超时兜底"测试 | High |
| 3 | 新增 `commit_state_supersede_atomic()`：查旧+插新+转 superseded+audit 单事务 | High |
| 4 | State key 规则按 layer 分：shared 允许 `owner_ai=''`；private/per-AI 必须非空 | High |
| 5 | `docs/phase20-implementation-plan.md` 同步（PR1 章节标记"以 v2.2 为唯一施工依据"）| High |
| 6 | Backfill owner_ai 缺 subject_id → **只 report-only**（不再"房间+source_ai 补齐"）| Med |
| 7 | Daemon CREATE 三点修正行号：`compress_diaries:139 / archive_old_work:235 / distill_psychology:385`；`tidy_living_room:305` 是 UPDATE-in-place**不算 CREATE**（v2/v2.1 全写错）| Med |
| 8 | 时间注入接入点补 `memory_ops.recall:1698` 主入口 | Med |
| 9 | Current status prompt 改动落在 `daemon.py:483 refresh_current_status()`（不是 current_status.py 模块）| Med |
| 10 | Context isolation soft 模式定义收敛：**observe-only（不改 room、不改 tags）** vs **redirect-on / reject-off**，二选一，明确写死不模糊叫 dry-run | Med |
| 11 | 删除 `--ignore-drift-if-only-touch`，改成"drift 后完整 state_before 比对，只允许 `updated_at / activation_count` 变化才继续" | Med |
| 12 | 删除 `daemon:{category}` 伪 subject_id；缺 subject 一律 report/skip，未来若需再引入独立 `state_scope_key` | Med |

---

## Q1｜要解决什么问题？

（v2 保留）7 类 write 污染：越界值 / `relationships` 复数房间 / owner_ai 空 / prefix 错配 / event 时间感缺失 / State 无时效 / Context isolation 缺失。修生成器 + 修存量（plan/execute）+ 修消费路径（**5 入口**含 `recall`）。

---

## Q2｜现在 Hub 是什么样？

### CREATE 点（v2.2 修正 daemon 行号）

| # | 位置 | 场景 |
|---|---|---|
| 1 | `memory_ops.remember()` :484 | create-no-relation 默认路径 |
| 2 | `memory_ops.remember()` :424 | create-with-supersede 分支 |
| 3 | `database.insert_pending_memory()` :544 | MCP async remember 骨架 |
| 4 | `daemon.compress_diaries()` :139 | weekly 周报合成（**改**）|
| 5 | `daemon.archive_old_work()` :235 | 归档旧任务合成（**新增**）|
| 6 | `daemon.distill_psychology()` :385 | career_mem 合成（**行号改**）|

**不是 CREATE**：`daemon.tidy_living_room:305`（UPDATE-in-place，v2/v2.1 错列，v2.2 移除）；`_promote_proposal`（内部走 `remember(quick=False)`，validation 已覆盖）。

### 共享 `_conn` 上的**全部**BEGIN IMMEDIATE 写路径（v2.2 完整清单）

| 函数 | 行 |
|---|---|
| `close_stale_intent_atomic` | :735 |
| `commit_finalize_atomic` | :991 |
| `commit_maintenance_atomic` | :1091 |

### 共享 `_conn` 上的**其他**写路径（不走 BEGIN IMMEDIATE，直接 execute+commit — v2.2 全部改造）

| 函数 | 行 |
|---|---|
| `insert_pending_memory` | :544 |
| `update_memory_status` | :586 |
| `mark_replaced` | :626 |
| `close_stale_intent` | :718 |
| `write_intent_ledger` | :644 |
| `set_memory` | :1234 |
| （proposals / audit / persons / profiles / async_remember_ledger 全部 insert/update 同类）| — |

**dream.py 独立连接**：`dream.py` 自维护单独 `sqlite3.connect()`，不共享 `_conn`，**排除在锁范围外**。v2.2 审计单里明确此豁免（reviewer 要求）。

### `subject_id / source_actor_id` 已可用

主判据只用 `subject_id`（v2.1 H1 保留）。

---

## Q3｜打算改成什么？

### **Step 0（v2.2 重写）：共享写锁真正闭环 + 内部 transaction context manager**

**核心契约**：
1. `_WRITE_LOCK: threading.Lock` **只存在 `database.py` 模块内**，**不 re-export**，`memory_ops.py:719` 的旧定义整体删除；旧 `memory_ops` 里两个持锁位（`_touch_recalled_memories:737`、`_check_auto_resolve:847`）**改为调用 `_write_transaction()`**。
2. 新增 `_write_transaction()` 内部 context manager：
   ```python
   # database.py
   _WRITE_LOCK = threading.Lock()
   _in_write_tx = threading.local()  # 嵌套侦测

   @contextlib.contextmanager
   def _write_transaction():
       """共享 _conn 上任何写操作必须包在本 ctx 内。
       非重入：同线程嵌套调用立即 RuntimeError（不 deadlock）。
       退出时 commit；异常 rollback。"""
       if getattr(_in_write_tx, 'active', False):
           raise RuntimeError(
               "nested _write_transaction() forbidden — "
               "caller inside a write tx must not call another write helper. "
               "Refactor to do all work inside one _write_transaction() block."
           )
       with _WRITE_LOCK:
           _in_write_tx.active = True
           conn = _get_conn()
           conn.execute("BEGIN IMMEDIATE")
           try:
               yield conn
               conn.commit()
           except BaseException:
               try:
                   conn.execute("ROLLBACK")
               except Exception:
                   pass
               raise
           finally:
               _in_write_tx.active = False
   ```
3. **所有共享 `_conn` 写函数**改造：
   - 3 个已有 BEGIN IMMEDIATE 函数（`close_stale_intent_atomic` / `commit_finalize_atomic` / `commit_maintenance_atomic`）：移除函数体内的 `conn.execute("BEGIN IMMEDIATE")` / `conn.commit()` / `conn.execute("ROLLBACK")`，改成 `with _write_transaction() as conn:` 包住原有 SQL。
   - **所有 execute+commit 直写函数**：`insert_pending_memory` / `update_memory_status` / `mark_replaced` / `close_stale_intent` / `write_intent_ledger` / `set_memory` / proposals / audit / persons / profiles / async_remember_ledger 系全部相关函数 — 用 `with _write_transaction() as conn:` 替换现有 execute+commit。
   - grep 断言：`git grep -n "conn.commit()" database.py` 应**只出现在** `init_db()` 里（schema 初始化用），其余全部走 ctx。
4. **锁不对外**：删除 v2.1 `memory_ops.py` 的 `from database import _WRITE_LOCK`。外部想批量写 → 只能通过 `database.commit_*()` helper 或未来提供的批量 helper。
5. **dream.py 独立连接豁免**：在 `database.py` `_write_transaction()` 的 docstring 里明写"本锁只覆盖 `_get_conn()` 返回的共享连接；`dream.py` 自维护独立 sqlite 连接，靠 sqlite 文件锁 + WAL 隔离，不在本契约内。"审计表也记一条 exemption。

**测试（v2.2 重写）**：删除 v2.1 的"故意 nested deadlock + wait_for 超时"测试（reviewer 说得对：如果同步锁阻死事件循环，`wait_for` 本身也可能 timer 起不来；线程放里放外都不安全）。改为：

1. `test_step0_nested_fail_fast` — 同线程内 `_write_transaction()` 里再调 `commit_maintenance_atomic()` → 立即 `RuntimeError`（不 hang，用 `pytest.raises`）。
2. `test_step0_finalize_and_maintenance_concurrent_no_deadlock` — 两线程分别调 finalize / maintenance，每个 100 次，总时长有上限（正常不超 5s，超则 fail 提示可能死锁）。
3. `test_step0_touch_and_maintenance_serialize` — 断言最终状态一致，无交错破坏。
4. `test_step0_all_write_paths_use_ctx` — 静态断言：`grep 'conn.commit()' database.py` 只出现在 `init_db`。
5. `test_step0_lock_not_exported` — 断言 `from database import _WRITE_LOCK` 依然可行（不禁 import，但契约上无用），并断言 `memory_ops` 模块里不再引用 `_WRITE_LOCK`。

**Step 0 独立提交** + Codex 复审 pass → 才继续 D-*。

### D-0：`context_kind` 字段贯穿全链路（v2 保留）

```sql
ALTER TABLE memories  ADD COLUMN context_kind TEXT NOT NULL DEFAULT '';
ALTER TABLE proposals ADD COLUMN context_kind TEXT NOT NULL DEFAULT '';
```
值域：`''` / `game` / `dream` / `roleplay` / `joke` / `chat` / `system`。链路：extractor → proposal → remember → pending → finalize → daemon。

### D-1：`memory_validation.py`（~260 行）

**导出**：`ROOM_ALIASES`、`PER_AI_ROOMS`、`CONTEXT_PRIMARY_ROOM`、`CONTEXT_ALLOWED_ROOMS`、`validate_memory_write`、`validate_context_isolation`、`is_ai_soliloquy_structured`、`safe_clamp_importance`。

**`ROOM_ALIASES`**：只 `preference → preferences`；不 alias `relationships ↔ relationship`（两个不同房间，v2 保留）。

**`is_ai_soliloquy_structured`**（v2.1 保留主判据只用 subject_id）：
```python
def is_ai_soliloquy_structured(mem, source_ai):
    if not source_ai:
        return False
    aliased_ai = _AI_ALIASES.get(source_ai, source_ai)
    subj = (mem.get('subject_id') or '').strip()
    if not subj:
        return False
    aliased_subj = _AI_ALIASES.get(subj, subj)
    return aliased_subj == aliased_ai
```

**反例测试 v2.1 保留**：`ai_about_user_not_flagged` / `source_actor_id_alone_insufficient` / `subject_ai_via_alias` / `subject_other_ai_not_flagged`。

### D-2 Event：`annotate_event()`（v2 保留，30 天前缀 + created_at fallback + `max(0, days)` clamp future）

### D-2 State：`state_ttl.py` + 5 列 migration

Migration 5 列：`valid_from / valid_until / last_confirmed_at / state_ttl_days / context_kind`。`_preserve_on_empty` 加 3；`state_ttl_days` DEFAULT 7；`_prep` 空/None → 7。

#### **State supersede key（v2.2 按 layer 分规则）**

```python
STATE_KEY_BASE = ('subject_id', 'category', 'room', 'layer')

def state_supersede_key(mem):
    """按 layer 决定 owner_ai 是否参与 key。
    - shared: owner_ai='' 是合法的（用户共享 state），key 里 owner_ai 位置固定填 ''
    - private / per-AI room: owner_ai 必须非空
    - base 四项任一为空 → return None（不参与 supersede）
    """
    base = tuple((mem.get(f) or '').strip() for f in STATE_KEY_BASE)
    if any(not part for part in base):
        return None
    layer = base[3]
    owner_ai = (mem.get('owner_ai') or '').strip()
    room = base[2]
    if layer == 'shared':
        return base + ('',)  # owner_ai='' 合法
    # private / per-AI: owner_ai 必须非空
    if not owner_ai:
        return None
    return base + (owner_ai,)
```

#### **`commit_state_supersede_atomic()`（v2.2 新增，真正原子）**

单锁单事务内完成：**重新查旧行 → 插新 state → 转全部旧 active state 为 superseded → 写 audit**。任何一步失败全部回滚。

```python
# database.py
def commit_state_supersede_atomic(
    new_state_mem: dict,       # 完整字段，含 id / valid_from / …
    key: tuple,                # state_supersede_key(new_state_mem)
    source_ai: str,
) -> dict:
    """返回 {'inserted_id': ..., 'superseded_ids': [...]}"""
    if key is None:
        raise ValueError("empty state key — cannot supersede")

    with _write_transaction() as conn:
        # 1) 事务内重新查所有匹配 active
        subj, cat, room, layer, owner = key
        if layer == 'shared':
            old_rows = conn.execute(
                "SELECT id, updated_at FROM memories WHERE status='active' "
                "AND info_type='state' AND subject_id=? AND category=? "
                "AND room=? AND layer='shared' AND owner_ai=''",
                (subj, cat, room)
            ).fetchall()
        else:
            old_rows = conn.execute(
                "SELECT id, updated_at FROM memories WHERE status='active' "
                "AND info_type='state' AND subject_id=? AND category=? "
                "AND room=? AND layer=? AND owner_ai=?",
                (subj, cat, room, layer, owner)
            ).fetchall()
        old_ids = [r[0] for r in old_rows]

        # 2) 插入新 state（走 helper 里的 raw INSERT，不再嵌套调用外层 helper）
        _raw_insert_memory(conn, new_state_mem)

        # 3) 全部旧 active → superseded
        for old_id in old_ids:
            conn.execute(
                "UPDATE memories SET status='superseded', "
                "valid_until=?, superseded_by=?, updated_at=? "
                "WHERE id=? AND status='active'",
                (new_state_mem['valid_from'], new_state_mem['id'],
                 _now_iso(), old_id)
            )

        # 4) audit
        conn.execute(
            "INSERT INTO maintenance_audit (action, target_id, new_content, "
            "decision_reason, state_before, state_after, source_ai, "
            "auto_executed, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            ('state_supersede', new_state_mem['id'], new_state_mem.get('content',''),
             f'new state supersedes {len(old_ids)} old',
             json.dumps({'old_ids': old_ids}),
             json.dumps({'new_id': new_state_mem['id']}),
             source_ai, 1, _now_iso())
        )
    return {'inserted_id': new_state_mem['id'], 'superseded_ids': old_ids}
```

**关键**：`_raw_insert_memory(conn, ...)` 是新加的内部 helper，接受已打开的 conn，**不再自持锁 / 不自开事务** — 避免 v2.1 那种"嵌套调 commit_maintenance_atomic 会 nested"的问题。

**并发测试（v2.2 新增）**：
- `test_state_supersede_concurrent_exactly_one_active` — 5 线程同 key 各调 `commit_state_supersede_atomic` → 最终 `SELECT count(*) WHERE status='active' AND [key]` **恰好等于 1**。
- `test_state_supersede_multiple_legacy_all_superseded` — 预置 3 条同 key 老 active（历史遗留）→ 一次 supersede 后全部转 superseded。
- `test_state_supersede_shared_layer_owner_empty_ok` — layer=shared + owner_ai='' → key 有效，supersede 成功。
- `test_state_supersede_private_layer_owner_empty_returns_none` — layer=private + owner_ai='' → key=None，不参与 supersede。

**Daemon archive**：新增 step `archive_stale_states`，扫 `last_confirmed_at < now - 3*ttl` → 走 `commit_maintenance_atomic` 标 `status='archived'`。

**State TTL**：`{'mood':3, 'health':14, 'work_status':14, 'energy':3, 'default':7}`。

### D-2b：Current status prompt 时间约束（v2.2 修正位置）

**改动落 `daemon.py:483 refresh_current_status()`**（reviewer 修正），不改 `current_status.py`。在拿到 raw memories 后 `apply_temporal_annotation()`；prompt 加"看到日期或 X 天前前缀禁止写'近期/当前/正在'"约束段。

### D-6：Context Isolation（v2.2 soft 模式定义收敛）

**上线模式明确二选一**（不用"dry-run"这个词）：

| 模式 | env 值 | 行为 |
|---|---|---|
| **observe-only** | `MEMORY_HUB_CONTEXT_ISOLATION_MODE=observe` | 只 `logger.warning` 记录建议，**不改 room、不改 tags** |
| **redirect-on / reject-off**（默认）| `''` 或 `redirect` | 立即重定向 room + 打 `_redirected_from_*` tag，但**不 raise** |
| **strict** | `=strict` | 上述 + 对 `roleplay/joke → 主房间` 直接 `raise ValueError` |

**上线计划**：第 1 周 `observe`；第 2 周切 `redirect`；观察 2 周稳定切 `strict`。

```python
def _isolation_mode() -> str:
    val = os.environ.get('MEMORY_HUB_CONTEXT_ISOLATION_MODE', '').strip().lower()
    return val if val in ('observe', 'redirect', 'strict') else 'redirect'

def validate_context_isolation(mem):
    kind = (mem.get('context_kind') or '').strip().lower()
    if not kind or kind not in CONTEXT_ALLOWED_ROOMS:
        return mem
    allowed = CONTEXT_ALLOWED_ROOMS[kind]
    room = (mem.get('room') or '').strip()
    if room in allowed:
        return mem

    mode = _isolation_mode()
    if mode == 'observe':
        logger.warning(f"context_isolation OBSERVE: {kind} would redirect from {room}")
        return mem  # 不改

    # redirect / strict
    if mode == 'strict' and kind in ('roleplay', 'joke') and room in CANONICAL_ROOMS:
        raise ValueError(f"context_isolation strict: {kind} → {room!r} rejected")

    original_room = room
    mem['room'] = CONTEXT_PRIMARY_ROOM[kind]
    tags = _parse_tags(mem.get('tags'))
    tags.append(f'_redirected_from_{kind}_{original_room or "empty"}')
    mem['tags'] = json.dumps(tags, ensure_ascii=False)
    return mem
```

### D-4：`scripts/data_health_backfill.py`（v2.2 修正）

架构同 v2：`--plan` / `--execute --plan-file` / `--db-path` / `--check` / `--max-fixes`。

**drift gate（v2.2 收严）**：PLAN 快照 `state_before = 完整 dict`；EXECUTE 阶段读取当前状态与 `state_before` 逐字段比对：
- 允许变化字段：`updated_at`、`activation_count`、`last_activated_at`（recall touch 副作用）
- 其他任何字段变化 → drift，跳过并 log。
- **删除 v2.1 的 `--ignore-drift-if-only-touch`**（reviewer 指出 updated_at 变化不能证明只是 touch）。

**owner_ai backfill（v2.2 收严）**：
- 有 `subject_id` 且能判定独白 → 补 `owner_ai`。
- **无 subject_id → report-only，人工审**（不再"房间+source_ai 补齐"，reviewer 指出这会把提取者当主体）。

**prefix 修复**：`--check prefix` 只 report-only，不动 content（v2 保留）。

### D-5：接入 CREATE 点（v2.2 修正 daemon 三处）

6 处 CREATE 全部包 `try: validate_memory_write(...) except ValueError as e: logger.warning(...); continue`（daemon 里单条不 crash step）。

### D-2c：`apply_temporal_annotation()` 接入 **5 入口**（v2.2 补 recall）

| 入口 | 位置 |
|---|---|
| `memory_ops.recall()` | :1698（**v2.2 新增**）|
| `smart_context.get_smart_context()` | :73 |
| `corridor.build_corridor()` | 每板块 items |
| `gateway.build_context()` | :249 |
| `memory_ops.dream_recall()` | :1983 |

---

## Q4｜分步 + 验收

| Step | 工作 | 验收 | 估时 |
|---|---|---|---|
| **0** | `_WRITE_LOCK` + `_write_transaction()` ctx；**全部**共享 _conn 写路径改造；memory_ops 侧删除锁 import；5 条测试 | grep 断言通过 + 并发/嵌套快速拒绝测试通 | **2 d**（比 v2.1 +1d，因涉及 execute+commit 直写全部迁移）|
| 1 | `context_kind` migration + 全链路 | 端到端持续测试通 | 1 d |
| 2 | `memory_validation.py` + `annotate_event` + soliloquy v2.1 + 34 单元测试 | 反例测试全过 | 1.5 d |
| 3 | `state_ttl.py` + 5 列 migration + `commit_state_supersede_atomic` + 隔离键分层 + 18 单元测试（含 4 条并发） | 并发恰好 1 条 active + shared/private layer 测试通 | 2 d |
| 4 | `apply_temporal_annotation()` 接入 5 入口 + 快照测试 5 条 | 5 入口各断言 | 1 d |
| 5 | `daemon.py:483 refresh_current_status` prompt 加时间约束 | mock LLM 收到含约束段 prompt | 0.5 d |
| 6 | 6 处 CREATE 接入 validation + daemon 新 step `archive_stale_states` | grep 6 处 + 集成测试 | 1 d |
| 7 | `scripts/data_health_backfill.py` plan/execute + 完整 state_before 比对 | 构造脏数据 → plan 报告 + drift 完整比对 pass | 1 d |
| 8 | VPS backfill plan → Ceci 审 → execute | audit 每条 + rebuild_all_corridors | 0.5 d |

**v2.2 总估时：10 d**（v2.1 是 9，v2.2 +1d for Step 0 全量写路径迁移 + State supersede 原子化）

---

## v2.2 单元测试清单（约 65 条）

- Step 0（5 条）：`nested_fail_fast` / `concurrent_no_deadlock` / `touch_and_maintenance_serialize` / `all_write_paths_use_ctx` / `lock_not_referenced_in_memory_ops`
- 独白判定（4 条 v2.1）：`ai_about_user_not_flagged` / `source_actor_id_alone_insufficient` / `subject_ai_via_alias` / `subject_other_ai_not_flagged`
- D-1 核心（10 条 v2 保留）
- 独白 owner_ai 补齐（3 条）
- D-6 context_kind isolation + 3 mode（10 条：observe/redirect/strict 各含 raise/no-raise）
- Event annotation（4 条）
- State ttl（18 条含 4 条并发 supersede + shared/private layer 4 条）
- 时间注入 5 入口（5 条）
- Backfill script（6 条含完整 state_before drift 比对）

全套目标 415+（1.7 基础 352 + PR1 v2.2 ~65）。

---

## Q5｜风险

### 高

1. **Step 0 全量写路径迁移影响面**
   `insert_pending_memory / update_memory_status / set_memory / write_intent_ledger / …` 全部改成 `with _write_transaction()` — 涉及文件多，调用方要么依然通过这些 helper（无感），要么如果外部代码直接 `_get_conn().execute(...)` 会绕过锁。
   **缓解**：（a）grep 断言 `_get_conn().execute\|_get_conn()\.execute` 只在 `database.py` 出现；（b）Step 0 独立提交后 Codex 复审专门看这个断言；（c）新增测试 `test_step0_no_bare_conn_writes_outside_database`。

2. **`commit_state_supersede_atomic` 与 `remember()` 的关系**
   State 记忆走 `remember()` 时不能直接嵌套调此 helper（RuntimeError）。方案：`remember()` 检测 `info_type='state'` 时**不走 helper 内部**，改在 `remember()` 返回前**外层**调 `commit_state_supersede_atomic`（新记忆 ID 已定）。或者更保守：state 走独立入口 `remember_state()`。
   **缓解**：Step 3 决定二选一并写死；开工时先 spike 30 分钟走通再定。

3. **`context_kind` 全链路串**（v2 保留）

### 中

4. **State supersede shared 语义**：shared state（用户 mood 等）跨 AI 共用，任一 AI 更新会覆盖所有旧 shared → 是期望行为。文档写清。
5. **backfill drift gate 严格化**：完整 state_before 比对开销较大，plan-file 会变大（几倍）→ 可接受。
6. **observe → redirect → strict 上线节奏**：3 周切完，靠 grafana 看 `context_isolation OBSERVE` warning count 曲线判断切换。

### 低

7. event_date 与 created_at 都缺 → annotate 返回原 mem。
8. `MEMORY_HUB_CONTEXT_ISOLATION_MODE` 值大小写不敏感，其他值 → 默认 redirect。

---

## 附：文件改动预览 v2.2

```
新增：
  memory_validation.py             (~260 行)
  state_ttl.py                     (~240 行含 shared/private key 分层)
  scripts/data_health_backfill.py  (~400 行含完整 state_before 比对)
  tests/test_memory_validation.py  (~500 行)
  tests/test_state_ttl.py          (~350 行含 4 并发)
  tests/test_temporal_annotation.py (~180 行含 5 入口)
  tests/test_backfill_script.py    (~200 行)
  tests/test_write_lock_step0.py   (~200 行 5 条)

改动：
  database.py       (+_WRITE_LOCK + _write_transaction ctx + commit_state_supersede_atomic
                     + 5 列 migration + _ALL_COLUMNS + _preserve_on_empty
                     + 所有共享 _conn 写路径改造)
  memory_ops.py     (删除 _WRITE_LOCK 定义 & import；两个持锁位改调 _write_transaction 或 commit_*
                     + 6 处 CREATE 加 validate
                     + recall() apply_temporal_annotation
                     + state 分流到 commit_state_supersede_atomic)
  corridor.py       (板块 items apply_temporal_annotation)
  smart_context.py  (get_smart_context 输出前)
  gateway.py        (build_context 输出前)
  mcp_server.py     (smart_context tool)
  daemon.py         (:483 refresh_current_status prompt 时间约束
                     + :139/:235/:385 三处 CREATE 加 validate
                     + 新增 archive_stale_states step)
  conversation_capture.py (extractor prompt 加 context_kind)
  async_remember.py (finalize 传递 context_kind)
  docs/phase20-implementation-plan.md (PR1 章节同步 → v2.2 唯一依据)
```

约 17 个文件、+3000 行 additive、65 条新测试。

---

## 交付流程 v2.2

1. **本方案 v2.2 推分支** `phase20/pr1-plan-v3`（含 implementation-plan.md 同步）
2. Ceci + 夜鹭 + Codex 都审 v2.2
3. 都过 → 开工分支 `phase20/pr1-data-health`
4. **Step 0 独立提交 + Codex 复审 pass 后**才继续 D-*
5. 全套测试 415+ 通过
6. 开 PR → Codex 复审 2 轮
7. 合并 + VPS 部署
8. VPS backfill plan → Ceci 审 → execute
9. Ceci 观察 1 周体感
10. 稳定后开 PR2
