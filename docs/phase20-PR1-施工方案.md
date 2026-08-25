# Phase 2.0 PR1 施工方案 v2.6 · Data Health

> 分支：本文推送分支 `phase20/pr1-plan-v3`（doc-only）；开工分支 `phase20/pr1-data-health`（v2.6 pass 后开）
> 依赖：Phase 1.7 全部合并（main 至 `604bc72`）
>
> **版本历史**：
> - v1 → v2.4（作废，见既往）
> - v2.5（作废）：Codex 三审后按现有生产 signature 收敛 4H+3M+1L；但仍有 3 High（auto-resolve 循环依赖 / read helpers 清单漏 11 个 + 写错 1 个 / state self-supersede 修法只写在测试没进主伪代码）+ 2 Medium（24h audit 去重不是真幂等 / v2.4 残留 callsite 冲突）+ 2 Low
> - **v2.6**（本文）：Codex 四审后修 3 High + 2 Medium + 2 Low

---

## v2.5 → v2.6 变更总览（3 High + 2 Medium + 2 Low，Codex 四审）

| 序 | 项 |
|---|---|
| H1 | auto-resolve **循环依赖修**：抽出 `resolve_patterns.py` 独立模块，含 `_matches_resolve_pattern` + 所有 resolve constants / regex；`database.py`（用于 `check_auto_resolve_atomic`）和 `memory_ops.py` 都从它 import；补真实 import 测试 `test_h1_no_circular_import` |
| H2 | database.py **纯读函数完整清单**（26 个，v2.5 漏 11 个 + 写错 1 个）：`get_memory / get_memory_by_client_request_id / list_stale_intent_ledgers / get_ledger / list_memories_by_status / query_memories / count_memories / get_proposal / list_proposals / count_proposals / list_audits / count_audits / get_profile / list_profiles / vector_search / fts_search / cjk_like_search / get_all_memory_ids / get_memories_batch / iter_memories / get_person / list_persons / get_memories_by_subject / count_memories_by_subject / resolve_alias / get_all_aliases`（v2.5 的 `get_person_by_alias` 不存在，实际是 `resolve_alias`）。新增 AST 测试 `test_h2_all_pure_reads_use_read_conn`：解析 database.py AST，白名单外的 public 函数其函数体不允许出现 `_get_conn()`（含 `_get_read_conn` 的除外）|
| H3 | state supersede **主伪代码写死 5 条规则**（v2.5 只在测试说明里写，主 code 没跟）：`_state_supersede_in_tx()` 起点先按 ID 查 existing；existing active + 同 key + payload fingerprint 相同 → idempotent no-op return；同 ID 但 payload 不同 → 抛 `IdempotencyConflict`；查旧 SELECT 加 `AND id <> ?`；audit 走**永久幂等**（M1）|
| M1 | audit **永久幂等**（不再 24h）：`maintenance_audit` 加 `UNIQUE(action, target_id, operation_id)` 索引；`operation_id` 由 caller 传（同 op 重放显式复用同 id）；同 target_id 的 state_supersede **永久唯一**（默认 operation_id = target_id + new_mem_id 组合）|
| M2 | 删除 v2.4 残留：`_state_supersede_in_tx()` 章节里"finalize/daemon 直接调 in_tx helper"的说法（第 341–343 行）与 v2.5 "全走 public wrapper、不改 finalize" 冲突 → 直接删掉这段，避免施工时选错 |
| L1 | 交付流程末尾"v2.2 推分支 / 都审 v2.2" → v2.6 |
| L2 | callsite 计数"3 类 4 处" → "5 分流点 4 函数"（`remember` 2 + daemon 3；函数计数 `memory_ops.remember` + 3 daemon = 4 函数）|

---

## v2.4 → v2.5 变更总览（4 High + 3 Medium + 1 Low，Codex 三审，存档）

| 序 | 项 |
|---|---|
| H1 | `check_auto_resolve_atomic` 按现有生产 `_check_auto_resolve(content, related_mems, source_ai) -> list[str]` 收敛：签名改 `(candidate_ids: list[str], new_content: str, source_ai: str) -> list[str]`；内部复用 `_matches_resolve_pattern(new_content)` 短路 + 事务内 SELECT 候选 + 逐条 UPDATE + 逐条 audit；不引入不存在的 `trigger_pattern` |
| H2 | state 分流路径按现有 pipeline 收敛：**不改** `commit_finalize_atomic`（它只做 ledger + skeleton 终态，`remember()` 已经落库）；不假设 daemon 在 `commit_maintenance_atomic` tx 内；**state 分流只在 `memory_ops.remember()` 一处 + daemon 三处，全部走 public `commit_state_supersede_atomic()` wrapper**（各自起自己的 tx）；`_state_supersede_in_tx(conn, ...)` 只留给未来真正持有 tx 的 database 内部调用，PR1 本身**无生产 caller 直接用它**（wrapper 唯一入口）|
| H3 | `_state_supersede_in_tx` SELECT 加 `AND id <> ?`；同 ID/同 key active 重放 → idempotent no-op；audit 对 `action + target_id` 24h 内去重；补 self-link 反例测试 |
| H4 | `commit_maintenance_atomic` drift gate 闭环：加 `pre_execute_check` 后不再用 caller 传入的旧 `expected_updated_at` 做 SQL WHERE；改为**事务内先 SELECT 读当前 `updated_at`**，callback pass 后 UPDATE 用**读到的当前值**（`BEGIN IMMEDIATE` 期间无并发 writer，安全）；补"PLAN 后 touch → EXECUTE 成功 / 改 content → drift" 反例测试 |
| M1 | database.py **所有 public read helpers** 迁 `_get_read_conn()`：`get_memory` / `query_memories` / `vector_search` / `fts_search` / `get_memories_batch` / persons/profiles/audits/ledger 全套 SELECT 函数；grep 断言范围扩展到 database.py 内部（读函数不再用 `_get_conn()`）|
| M2 | `_get_read_conn()` 加 DB_PATH 失效机制：缓存 `read_conn` + `read_db_path`；路径变化时关旧建新；进程 shutdown hook 关闭当前线程连接；新增"同一线程先读 DB A 后切 DB B"测试 |
| M3 | `state_before` 格式定死：`{'strict_hash', 'stable_hash', 'snapshot'}`；strict_hash 覆盖白名单全字段，stable_hash 排除 `updated_at/activation_count/last_activated`；**白名单从 `_ALL_COLUMNS` 自动生成**（排除 embedding + 三 volatile 列），保证不漏 `tags/source_context/domain/history/comments` 等 |
| Low | 总实施方案 + 本文档所有"v2.2 唯一依据 / v2.2 总估时 / v2.2 交付流程"等残留标题统一改 v2.5 |

---

## v2.3 → v2.4 变更总览（3 High + 3 Medium，Claude 自审，存档）

| 序 | 项 |
|---|---|
| H-A | 抽 `_state_supersede_in_tx(conn, ...)` 内部 helper；`commit_state_supersede_atomic` 只作 public wrapper；finalize / daemon 已在 tx 内的调用者直接调 in_tx helper，避免嵌套 `_write_transaction()` 撞 RuntimeError |
| H-B | `check_auto_resolve_atomic(mem_id, new_content, trigger_pattern) -> {'resolved': bool, 'audit_id': int\|None}` signature 定死；读 + regex + 写全在同一 `_write_transaction()` 内（regex 纯 CPU <1ms/条），避免 TOCTOU |
| H-C | Backfill `pre_execute_check` 补 3 参数：`(conn, current_row, plan_state_before)`；hash 对比在事务内完成 |
| M-A | daemon `try/except ValidationError` **wrap 位置落到每个 step 内 for 循环单次 iteration 最外层**（不塞 helper） |
| M-B | 所有 `git grep` 静态断言加 `':!tests/'` 排除测试目录，避免 fixture 用共享 conn 触发假红 |
| M-C | 开工前 spike 10 分钟确认 `_get_read_conn()` 是模块级单例还是 `threading.local()` 每线程一个；结论写进 Step 0 commit message |

---

## v2.2 → v2.3 变更总览（5 High + 5 Medium，存档）

**Critical 已处理**（不列入本表）：`927be84` 代码回退 → `3494ed0` fixup restore。

### High

| 序 | 项 |
|---|---|
| H1 | `_write_transaction()` 修 try/finally 结构：`began` flag + 连接获取/BEGIN 也纳入 try，避免中间抛异常导致 `_in_write_tx.active` 永久污染线程 |
| H2 | Step 0 补 `activity_log.py`：3 处 `database._get_conn()` + `conn.execute` + `conn.commit` 全部迁移。schema 初始化移入 `database.init_db()`，写/清理走 database 层公开原子 helper |
| H3 | 锁封装契约收紧：`memory_ops` 不直接调 `_write_transaction()`，改公开 helper `touch_recalled_memories_atomic()` / `check_auto_resolve_atomic()`；测试禁止 `import _WRITE_LOCK`；grep 断言"生产模块（非 database.py）不引用 `_write_transaction` / `_WRITE_LOCK`" |
| H4 | 读连接一致性：生产读路径统一走独立 `_get_read_conn()`（PRAGMA query_only=ON 只读连接）；grep 断言"读路径不用共享 `_conn`"；同一 connection 事务中间态泄漏问题消失 |
| H5 | State 落库真路径：不再新造 `_raw_insert_memory()`；抽取 `_set_memory_in_tx(conn, mem)`（含 `memories_vec` + `vec_id_map` 同步 + `client_request_id` / claim / link 保留）。`set_memory()` 变成"包 `_write_transaction()` + 调 `_set_memory_in_tx()`"；`commit_state_supersede_atomic()` 自身事务内也调它。state 分流点明确落 `memory_ops.remember()` 两处 CREATE 分支的 `set_memory()` 之前，不再"return 前处理" |

### Medium

| 序 | 项 |
|---|---|
| M1 | Backfill `state_before` JSON 序列化：定义 JSON-safe 字段白名单，`embedding` 只存 `sha256(bytes).hexdigest()` |
| M2 | 字段名修正：`last_activated_at` → `last_activated`（memory schema 实际字段名）|
| M3 | Backfill drift gate 语义快照 hash 必须在**同一 `BEGIN IMMEDIATE`** 内校验，不能事务外快照 → 事务内更新（TOCTOU）|
| M4 | Context isolation 默认值：`observe`（未配置或非法值走 observe，而非 v2.2 的 redirect），与"第一周 observe"一致 |
| M5 | CREATE 校验失败处理分路径：daemon 循环内 `log + continue`；MCP / `remember()` 直路径必须**结构化拒绝** or `raise ValidationError`（wrapper 转 4xx），不能 continue |

---

## v2.1 → v2.2 变更总览（存档）

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

### 共享 `_conn` 上的**其他**写路径（不走 BEGIN IMMEDIATE，直接 execute+commit — v2.3 全部改造）

| 函数 / 位置 | 行 |
|---|---|
| `database.insert_pending_memory` | :544 |
| `database.update_memory_status` | :586 |
| `database.mark_replaced` | :626 |
| `database.close_stale_intent` | :718 |
| `database.write_intent_ledger` | :644 |
| `database.set_memory` | :1234 |
| （database 内 proposals / maintenance_audit / persons / profiles / async_remember_ledger insert/update 同类） | — |
| **`activity_log.py:123` schema 建表（v2.3 补）** | 迁入 `database.init_db()` |
| **`activity_log.py:181` INSERT activity_log（v2.3 补）** | 走新 `database.append_activity_log_atomic()` |
| **`activity_log.py:197` DELETE 清理（v2.3 补）** | 走新 `database.trim_activity_log_atomic()` |

**dream.py 独立连接**：`dream.py` 自维护单独 `sqlite3.connect()`，不共享 `_conn`，**排除在锁范围外**（sqlite 文件锁 + WAL 隔离）。v2.3 审计单明确此豁免。

### `subject_id / source_actor_id` 已可用

主判据只用 `subject_id`（v2.1 H1 保留）。

---

## Q3｜打算改成什么？

### **Step 0（v2.3 重写）：共享写锁真闭环 + 内部 tx ctx + 读连接一致 + activity_log 归位**

**核心契约**：

1. **锁与 ctx 只属于 database.py**：`_WRITE_LOCK: threading.Lock` 和 `_write_transaction()` context manager **仅在 `database.py` 内部使用**；生产模块（`memory_ops` / `activity_log` / `daemon` / …）**禁止**直接引用它们。删除 `memory_ops.py:719` 旧 `_WRITE_LOCK` 定义。

2. **`_write_transaction()` 修正版**（H1，`began` flag + 完整 try/finally 保护）：
   ```python
   # database.py
   import contextlib, threading
   _WRITE_LOCK = threading.Lock()
   _in_write_tx = threading.local()

   @contextlib.contextmanager
   def _write_transaction():
       """共享 _conn 上任何写操作必须包在本 ctx 内。
       非重入：同线程嵌套调用立即 RuntimeError（不 deadlock）。
       H1 修正：连接获取 + BEGIN 也在 try/finally 内；began flag 控制 ROLLBACK；
       active 标志无论中间抛什么都会恢复。"""
       if getattr(_in_write_tx, 'active', False):
           raise RuntimeError(
               "nested _write_transaction() forbidden — "
               "caller inside a write tx must not call another write helper. "
               "Refactor to do all work inside one _write_transaction() block."
           )
       lock_acquired = False
       began = False
       try:
           _WRITE_LOCK.acquire()
           lock_acquired = True
           _in_write_tx.active = True
           conn = _get_conn()
           conn.execute("BEGIN IMMEDIATE")
           began = True
           yield conn
           conn.commit()
           began = False
       except BaseException:
           if began:
               try:
                   conn.execute("ROLLBACK")
               except Exception:
                   pass
           raise
       finally:
           _in_write_tx.active = False
           if lock_acquired:
               _WRITE_LOCK.release()
   ```

3. **公开的 database.py atomic helpers** — `memory_ops` 只调这些，从不接触 ctx：
   - 现有：`commit_maintenance_atomic` / `commit_finalize_atomic` / `close_stale_intent_atomic` / `set_memory` / `insert_pending_memory` / `update_memory_status` / `mark_replaced` / `close_stale_intent` / `write_intent_ledger` 全部改造为**内部包 `with _write_transaction()`**。
   - **v2.3 新增**（H3 支撑）：
     - `touch_recalled_memories_atomic(mem_ids: list[str], now_iso: str) -> None` — 原 `memory_ops._touch_recalled_memories:737` 的写部分迁入
     - `check_auto_resolve_atomic(candidate_ids: list[str], new_content: str, source_ai: str) -> list[str]` — **v2.6 修循环依赖**。语义对齐 `memory_ops._check_auto_resolve(content, related_mems, source_ai) -> list[str]`。**新增 `resolve_patterns.py` 独立模块**（避免 database.py 反向 import memory_ops 循环）：
       - `resolve_patterns.py` 承载：`_matches_resolve_pattern(text) -> bool` + 所有 resolve constants / regex（中英词表 / 否定 / 疑问 / 分句检测），全部从 `memory_ops.py:630` 迁入
       - `memory_ops.py` 改为 `from resolve_patterns import _matches_resolve_pattern`（保留原调用点）
       - `database.check_auto_resolve_atomic` 也 `from resolve_patterns import _matches_resolve_pattern`
       - 步骤（`_matches_resolve_pattern(new_content)` 短路 + 事务内 SELECT 全部候选内容 + 逐条条件 UPDATE + 逐条写 audit）**全在同一 `_write_transaction()` 内**（`_matches_resolve_pattern` 纯 CPU，多候选累计持锁 <5ms 可接受）
       - **v2.6 H1 测试**：`test_h1_no_circular_import` — 真实 `import database; import memory_ops; import resolve_patterns` 三个都成功，不用 mock；`test_h1_resolve_pattern_shared_across_modules` 断言两侧引用是同一函数对象
   - **v2.3 新增**（H2 activity_log 归位）：
     - `init_db()` 里加 `CREATE TABLE IF NOT EXISTS activity_log ...`（从 `activity_log.py:123` 迁入）
     - `append_activity_log_atomic(row: dict) -> None` — INSERT
     - `trim_activity_log_atomic(keep_last_n: int) -> None` — 清理
   - **v2.5 修正**（H2 按现有 pipeline 收敛）：
     - `_state_supersede_in_tx(conn, new_mem, key, source_ai) -> dict` — 内部 helper（不自持锁，需在已开 tx 的 conn 上调用）。含"事务内重新查旧 → `_set_memory_in_tx` 插新 → 全部旧 → superseded → 写 audit"。**PR1 本身无生产 caller 直接用它**——只作为 wrapper 的实现细节，以及未来 database 内部若出现真正持 tx 的 caller 时的复用点。
     - `commit_state_supersede_atomic(new_mem, key, source_ai)` = `with _write_transaction() as conn: return _state_supersede_in_tx(conn, ...)` — **唯一生产入口**。
     - **不改 `commit_finalize_atomic`**（现签名 `(skeleton_id, client_request_id, terminal_state, result_memory_id, skeleton_update, owner_token)`，只做 ledger + skeleton 终态转移；`memory_ops.remember()` 已经落库，state 分流在它内部完成，跟 finalize 无关）。
     - **不假设 daemon 三处 CREATE 在 `commit_maintenance_atomic` tx 内**（实际是直接 `store.set_memory()`）。若合成 state，同样在 daemon iteration 内**调 public `commit_state_supersede_atomic()`**——起自己独立 tx。

4. **`activity_log.py` v2.3 改造**（H2）：删掉自己的 `_get_conn()` + 建表 + INSERT + DELETE + `conn.commit()`；改为 `from database import append_activity_log_atomic, trim_activity_log_atomic`。

5. **读连接一致性**（H4）：**生产读路径全部走 `_get_read_conn()`**（PRAGMA query_only=ON 独立只读连接，无 tx 参与，看不到其他线程未提交状态）。

   **v2.6 H2 修正——完整清单（26 个真实 pure read helpers）**：Codex 四审指出 v2.5 清单漏 11 个 + 错写 1 个（`get_person_by_alias` 不存在，实际是 `resolve_alias`）。以下按现有 database.py 的行号扫过一遍确认：

   | 函数 | 行 | 函数 | 行 |
   |---|---|---|---|
   | `get_memory` | 521 | `count_proposals` | 1572 |
   | `get_memory_by_client_request_id` | 532 | `list_audits` | 1601 |
   | `list_stale_intent_ledgers` | 694 | `count_audits` | 1616 |
   | `get_ledger` | 932 | `get_profile` | 1671 |
   | `list_memories_by_status` | 1209 | `list_profiles` | 1681 |
   | `query_memories` | 1405 | `vector_search` | 1743 |
   | `count_memories` | 1504 | `fts_search` | 1869 |
   | `get_proposal` | 1540 | `cjk_like_search` | 2010 |
   | `list_proposals` | 1546 | `get_all_memory_ids` | 2047 |
   | `get_memories_batch` | 2056 | `iter_memories` | 2076 |
   | `get_person` | 2254 | `list_persons` | 2262 |
   | `get_memories_by_subject` | 2276 | `count_memories_by_subject` | 2286 |
   | `resolve_alias` | 2302 | `get_all_aliases` | 2397 |

   全 26 个 body 里的 `_get_conn()` 替换为 `_get_read_conn()`。

   **`ro_*` 变体已在 read_conn 上，不动**（`ro_iter_memories:2118 / ro_vector_search:2144 / ro_fts_search:2166 / ro_cjk_like_search:2183 / ro_get_memory:2200`）。

   **规则**：内部逻辑若确实需要读**未提交**数据（如原子事务里先 SELECT 再 UPDATE），必须显式接受 `conn` 参数（属于 in_tx helper 系列，如 `_state_supersede_in_tx` 里的查旧）。上层 `memory_ops` / `corridor` / `smart_context` / `gateway` / `mcp_server` / `main` 通过调 database public helpers 间接迁移，无需自己动 `_get_conn()`。

   **v2.6 H2 测试**：`test_h2_all_pure_reads_use_read_conn`：AST 解析 `database.py`，对上表 26 个函数每个函数体：
   - 允许出现 `_get_read_conn()` 调用（≥1）
   - 禁止出现 `_get_conn()` 调用
   - 未来若新增 pure read helper 忘迁 → 立即红（比 grep 更严格，因 grep 可能被"路径不匹配"绕过）

   **v2.5 M2 修正——`_get_read_conn()` 加 DB_PATH 失效机制**（Codex 复审确认现状是 `threading.local` 缓存，缺失效）：
   ```python
   # database.py:31 修正
   def _get_read_conn() -> sqlite3.Connection:
       conn = getattr(_local, 'read_conn', None)
       cached_path = getattr(_local, 'read_db_path', None)
       cur_path = str(DB_PATH)
       if conn is not None and cached_path == cur_path:
           return conn
       # 路径变了 → 关旧建新
       if conn is not None:
           try: conn.close()
           except Exception: pass
       conn = sqlite3.connect(f"file:{cur_path}?mode=ro", uri=True, check_same_thread=False)
       conn.row_factory = sqlite3.Row
       conn.execute("PRAGMA busy_timeout=200")
       conn.execute("PRAGMA query_only=ON")
       # sqlite_vec 加载不变
       _local.read_conn = conn
       _local.read_db_path = cur_path
       return conn

   def close_thread_read_conn() -> None:
       """进程 shutdown 或线程结束时调，避免 fd 泄漏。"""
       conn = getattr(_local, 'read_conn', None)
       if conn is not None:
           try: conn.close()
           except Exception: pass
           _local.read_conn = None
           _local.read_db_path = None
   ```
   `main.py` 的 shutdown hook 里调 `close_thread_read_conn()`；`--db-path` backfill 脚本切库前也调。

   **v2.5 M2 测试**：
   - `test_m2_read_conn_invalidates_on_db_path_switch` — 同线程先读 DB A、`init_db(path_B)` 后读 → 应看到 DB B 数据
   - `test_m2_close_thread_read_conn_releases_fd`

6. **grep 断言全套**（H3 + H4 支撑；**v2.4 M-B**：全部加 `':!tests/'` 排除测试目录，避免 fixture 用共享 conn 触发假红）：
   - `git grep -nE "_write_transaction|_WRITE_LOCK" -- '*.py' ':!tests/'` → **只在 `database.py` 出现**（生产模块 + scripts/ 均禁用）
   - `git grep -nE "database\._get_conn|\bfrom database import _get_conn" -- '*.py' ':!tests/'` → 只在 `database.py` 内部出现
   - `git grep -n "conn.commit()" database.py` → 只在 `init_db()` 出现
   - `git grep -n "database\._get_conn" activity_log.py` → 0 命中
   - `git grep -nE "database\._get_conn" -- 'memory_ops.py' 'corridor.py' 'smart_context.py' 'gateway.py' 'mcp_server.py' 'main.py'` → 0 命中（读全走 `_get_read_conn`）

7. **dream.py 独立连接豁免**：docstring 明写，审计表记 exemption。

**测试（v2.3 收敛）**：

1. `test_step0_nested_fail_fast` — 同线程内 tx 里再调 `commit_maintenance_atomic` → 立即 `RuntimeError`，用 `pytest.raises` 断言（不 hang）。
2. `test_step0_active_flag_recovered_on_begin_failure` — mock `conn.execute("BEGIN IMMEDIATE")` 抛异常 → 断言 `_in_write_tx.active` 事后仍为 False，后续 tx 正常。**H1 直测**。
3. `test_step0_active_flag_recovered_on_yield_failure` — tx body 抛异常 → 断言 active 恢复 + lock 释放（连续 3 次都 OK）。
4. `test_step0_finalize_and_maintenance_concurrent_no_deadlock` — 两线程各 100 次，总时长上限（正常 <5s）。
5. `test_step0_touch_and_maintenance_serialize` — 最终状态一致断言。
6. `test_step0_all_write_paths_use_ctx` — 静态 grep 断言 `conn.commit()` 只在 `init_db`。
7. `test_step0_lock_not_exported_outside_database` — 静态 grep 断言：非 database.py / 非 `tests/test_write_lock_step0.py` 的所有 `.py` 文件不含 `_write_transaction` 或 `_WRITE_LOCK`。
8. `test_step0_no_shared_conn_writes_outside_database` — 静态 grep 断言：非 database.py 生产文件不用 `database._get_conn()`（读也不用）。
9. `test_step0_activity_log_uses_public_helpers` — 单元测试 activity_log 写入正确落 DB + 观察不到共享 conn 直用。
10. `test_step0_read_paths_use_read_conn` — 静态 grep 断言：生产模块读操作走 `_get_read_conn`。

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

#### **`commit_state_supersede_atomic()` + `_state_supersede_in_tx()`（v2.6 最终版）**

**分层**：
- **public**：`commit_state_supersede_atomic()` = 包 `_write_transaction()` + 调 in_tx helper。**PR1 唯一生产入口**——`memory_ops.remember()` 分流点 + daemon 三处 CREATE 都调此。
- **internal**：`_state_supersede_in_tx(conn, new_mem, key, source_ai)` — 不自持锁 / 不自开事务，接受已开 tx 的 conn。**PR1 无生产 caller 直接调**——只作为 public wrapper 的实现细节，以及未来若出现真正持 tx 的 database 内部 caller 时的复用点。
- **不改** `commit_finalize_atomic`（只做 ledger + skeleton 终态，`memory_ops.remember()` 已经落库；async pipeline 重构远超 PR1 范围）。

```python
# database.py

def _state_supersede_in_tx(conn, new_state_mem, key, source_ai):
    """内部 helper — 已在 tx 内的 conn 上调用，不自持锁。
    v2.6 H3: 5 条规则全部写死在主 code。"""
    if key is None:
        raise ValueError("empty state key — cannot supersede")

    subj, cat, room, layer, owner = key
    new_id = new_state_mem['id']

    # v2.6 H3 规则 1-3: idempotent 保护
    existing = conn.execute(
        "SELECT id, status, content, subject_id, category, room, layer, owner_ai "
        "FROM memories WHERE id = ?", (new_id,)
    ).fetchone()
    if existing is not None:
        existing_dict = dict(existing)
        existing_key = state_supersede_key(existing_dict)
        # 计算 payload fingerprint（内容 + 关键字段）
        new_fp = _payload_fingerprint(new_state_mem)
        existing_fp = _payload_fingerprint(existing_dict)
        if existing_dict['status'] == 'active' and existing_key == key and existing_fp == new_fp:
            # 规则 2: 完全相同的 idempotent 重放 → no-op
            return {'inserted_id': new_id, 'superseded_ids': [], 'idempotent': True}
        if existing_fp != new_fp:
            # 规则 3: 同 ID 但 payload 不同 → 禁止覆盖
            raise IdempotencyConflict(
                f"memory id {new_id} already exists with different payload; "
                f"caller must use fresh id or explicit update path"
            )
        # 同 ID 同 payload 但已 superseded → 也当 no-op（避免复活死记忆）
        return {'inserted_id': new_id, 'superseded_ids': [], 'idempotent': True}

    # v2.6 H3 规则 4: 查旧加 AND id <> ?
    # 1) 事务内重新查所有匹配 active（排除新 id 自身）
    if layer == 'shared':
        old_rows = conn.execute(
            "SELECT id, updated_at FROM memories WHERE status='active' "
            "AND info_type='state' AND subject_id=? AND category=? "
            "AND room=? AND layer='shared' AND owner_ai='' "
            "AND id <> ?",  # v2.6 H3 规则 4
            (subj, cat, room, new_id)
        ).fetchall()
    else:
        old_rows = conn.execute(
            "SELECT id, updated_at FROM memories WHERE status='active' "
            "AND info_type='state' AND subject_id=? AND category=? "
            "AND room=? AND layer=? AND owner_ai=? "
            "AND id <> ?",  # v2.6 H3 规则 4
            (subj, cat, room, layer, owner, new_id)
        ).fetchall()
    old_ids = [r[0] for r in old_rows]

    # 2) 插入新 state（走 _set_memory_in_tx，含 vec_id_map + memories_vec 同步 + preserve 保护）
    _set_memory_in_tx(conn, new_state_mem)

    # 3) 全部旧 active → superseded
    for old_id in old_ids:
        conn.execute(
            "UPDATE memories SET status='superseded', "
            "valid_until=?, superseded_by=?, updated_at=? "
            "WHERE id=? AND status='active'",
            (new_state_mem['valid_from'], new_state_mem['id'],
             _now_iso(), old_id)
        )

    # 4) audit — v2.6 M1: 永久幂等，靠 (action, target_id, operation_id) UNIQUE 索引
    #    默认 operation_id = state_supersede:{new_id} —— 同 target_id 只允许一条 state_supersede audit
    operation_id = f"state_supersede:{new_id}"
    conn.execute(
        "INSERT OR IGNORE INTO maintenance_audit "
        "(action, target_id, operation_id, new_content, "
        "decision_reason, state_before, state_after, source_ai, "
        "auto_executed, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ('state_supersede', new_id, operation_id,
         new_state_mem.get('content', ''),
         f'new state supersedes {len(old_ids)} old',
         json.dumps({'old_ids': old_ids}),
         json.dumps({'new_id': new_id}),
         source_ai, 1, _now_iso())
    )
    return {'inserted_id': new_id, 'superseded_ids': old_ids, 'idempotent': False}


def commit_state_supersede_atomic(new_state_mem, key, source_ai):
    """public wrapper — PR1 唯一生产入口。
    memory_ops.remember() 分流点 + daemon 三处 CREATE 合成 state 都调此。
    _state_supersede_in_tx 只作为本 wrapper 的实现细节，
    以及未来若出现真正持 tx 的 database 内部 caller 时的复用点。"""
    with _write_transaction() as conn:
        return _state_supersede_in_tx(conn, new_state_mem, key, source_ai)
```

**v2.6 生产 callsite 定死**（**5 分流点、4 函数**，全走 public wrapper）：
1. `memory_ops.remember()` **两处** CREATE 分支（`:484` + `:424`）的 `set_memory()` 之前分流点 → 调 `commit_state_supersede_atomic(...)`
2. `daemon.compress_diaries:139` / `daemon.archive_old_work:235` / `daemon.distill_psychology:385` **三处** iteration 内合成 state 时 → 调 `commit_state_supersede_atomic(...)`
3. **不改** `commit_finalize_atomic`（现签名只做 ledger + skeleton 终态；`memory_ops.remember()` 已经落库 state；async pipeline "LLM 只生成 write intent、最终统一事务落库"的重构远超 PR1 范围）

**v2.6 H3 + M1 schema 支撑**：
```sql
-- database.init_db() migration 段新增
ALTER TABLE maintenance_audit ADD COLUMN operation_id TEXT NOT NULL DEFAULT '';
CREATE UNIQUE INDEX IF NOT EXISTS idx_audit_op_unique
  ON maintenance_audit(action, target_id, operation_id)
  WHERE operation_id != '';  -- 部分索引，空值不参与唯一约束（兼容存量）
```

`_payload_fingerprint(mem)` helper：
```python
_FINGERPRINT_FIELDS = ('content', 'importance', 'subject_id', 'source_actor_id',
                       'owner_ai', 'room', 'category', 'layer', 'context_kind',
                       'valid_from', 'valid_until', 'state_ttl_days')
def _payload_fingerprint(mem: dict) -> str:
    snap = {k: mem.get(k) for k in _FINGERPRINT_FIELDS}
    return hashlib.sha256(json.dumps(snap, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
```

`IdempotencyConflict(Exception)` — 新自定义异常，`memory_ops.remember()` 分流点 catch 后包装为 `ValidationError` 返回给 client。

**v2.6 H3 + M1 反例测试**：
- `test_h3_same_id_same_payload_replay_is_noop` — 连调两次 → 第二次返回 `{'idempotent': True}`；DB 恰好 1 条 active；audit 恰好 1 条（`INSERT OR IGNORE` 触发）
- `test_h3_same_id_diff_payload_raises_conflict` — 同 ID 改 content → 抛 `IdempotencyConflict`；DB 里 existing row 不变
- `test_h3_supersede_excludes_self` — 断言 `superseded_by` 永不指向自己
- `test_m1_audit_permanent_idempotent_across_days` — 手动改 audit `created_at` 为 30 天前，再重放 → audit 仍恰好 1 条（M1 永久幂等，非 24h 窗口）

**关键 v2.3（H5）**：**不新造缩水版 INSERT**。改从现有 `set_memory()` 抽取核心逻辑到 `_set_memory_in_tx(conn, mem)` 内部 helper：
- 完整 UPSERT `memories` 表（含 `_preserve_on_empty` / `_preserve_always` 处理，保护 `client_request_id` / `finalize_claim_id` / `link_to_real_id` / `created_at` 等字段不被空值覆盖）
- 同步维护 `memories_vec` 表（embedding 更新）
- 同步维护 `vec_id_map`（新 mem 分配 rowid）
- **不自持锁 / 不自开事务**（接受已打开的 conn）

改造后：
- `database.set_memory()` = `with _write_transaction() as conn: _set_memory_in_tx(conn, mem)`
- `commit_state_supersede_atomic()` 自身 tx 内调 `_set_memory_in_tx(conn, new_state_mem)`
- 所有 CREATE 点（`memory_ops.remember()` 两处、daemon 三处、`insert_pending_memory` 路径）**继续走 `set_memory()`**，无感

**state 分流点明确**（H5 二审要求）：在 `memory_ops.remember()` 的两处 CREATE 分支（`:484` create-no-relation + `:424` create-with-supersede）里，**调 `set_memory()` 之前**分流：
```python
# memory_ops.remember() 两处伪代码
if info_type == 'state':
    key = state_supersede_key(mem_dict)
    if key is not None:
        # 走原子 supersede（内含 _set_memory_in_tx 插新 + 老 → superseded + audit）
        result = database.commit_state_supersede_atomic(mem_dict, key, source_ai)
        return result['inserted_id']
    # key is None（缺 subject_id 等）→ 落 report-only backfill 单，不写入
    logger.warning(f"state without valid key: {mem_dict.get('id')} → not persisted")
    return None
# 非 state → 正常 set_memory
database.set_memory(mem_dict)
```

- `insert_pending_memory` 路径也同分流（在 finalize 阶段升级 state 时判断）。
- daemon 三处若合成 state 记忆亦走 `commit_state_supersede_atomic`。

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

**上线模式明确三选一**（v2.3 M4：默认改回 `observe`，与上线第一周一致）：

| 模式 | env 值 | 行为 |
|---|---|---|
| **observe-only（v2.3 默认）** | `''` / `observe` / 非法值 | 只 `logger.warning` 记录建议，**不改 room、不改 tags** |
| **redirect-on / reject-off** | `MEMORY_HUB_CONTEXT_ISOLATION_MODE=redirect` | 立即重定向 room + 打 `_redirected_from_*` tag，但**不 raise** |
| **strict** | `=strict` | 上述 + 对 `roleplay/joke → 主房间` 直接 `raise ValueError` |

**上线计划**：第 1 周 `observe`（默认）；第 2 周显式切 `redirect`；观察 2 周稳定后切 `strict`。**未配置 → observe**，避免"忘 set env 就立即改数据"的意外。

```python
def _isolation_mode() -> str:
    val = os.environ.get('MEMORY_HUB_CONTEXT_ISOLATION_MODE', '').strip().lower()
    # v2.3 M4: 未配置 / 非法值 默认 observe（不改数据），只有显式配置才 redirect/strict
    return val if val in ('observe', 'redirect', 'strict') else 'observe'

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

### D-4：`scripts/data_health_backfill.py`（v2.3 修正 M1/M2/M3）

架构同 v2：`--plan` / `--execute --plan-file` / `--db-path` / `--check` / `--max-fixes`。

**drift gate（v2.3 M3 修正 TOCTOU）**：
- PLAN 阶段：从每条 memory 抽取 **JSON-safe 字段白名单**（v2.3 M1）计算 `snapshot_hash = sha256(json.dumps(snapshot, sort_keys=True))`；`state_before = {'snapshot_hash': ..., 'sample_fields': {id, status, updated_at, ...}}`。
- EXECUTE 阶段：**在 `commit_maintenance_atomic` 的同一 `BEGIN IMMEDIATE` 事务内**（v2.3 M3：不能事务外查后再进 tx 更新，那样有 TOCTOU）：
  1. 读取当前 memory 完整字段
  2. 计算当前 `snapshot_hash`
  3. 允许字段变化后重算：如果只有 `updated_at` / `activation_count` / `last_activated`（v2.3 M2 修正字段名）变化，用**"忽略这三字段"版本的白名单**重算 hash 对比
  4. hash 一致 → 执行；不一致 → drift，事务内 ROLLBACK 并 log skip
- 为了让上面 4 步在 tx 内跑，`commit_maintenance_atomic` 增加可选参数 **v2.4 H-C**：
  ```python
  pre_execute_check: Callable[[conn, current_row: dict, plan_state_before: dict], bool] | None
  ```
  三参数：已开事务的 conn、事务内读到的当前完整 row、PLAN 阶段快照的 `state_before`。callback 返回 False → `commit_maintenance_atomic` 抛 `MaintenanceDrift`（事务回滚 + backfill 侧 skip + log）。

**v2.5 H4 drift gate 真正闭环**：v2.4 遗留 bug —— `commit_maintenance_atomic` 的 UPDATE 现有代码是 `WHERE id=? AND updated_at=?`（用 caller 传的 `expected_updated_at`）。即使 `pre_execute_check` 判"只是 activation touch，可继续"，最终 UPDATE 仍然 `rowcount=0` 抛 `MaintenanceDrift`。

修法：**backfill 路径下不再传 `expected_updated_at`**。事务内的流程改为：
1. 事务内 SELECT 读当前完整 row（含现值 `updated_at`）
2. 调 `pre_execute_check(conn, current_row, plan_state_before)` — callback 对比 strict_hash / stable_hash
3. callback pass → UPDATE 用**事务内读到的当前 `updated_at`** 作 WHERE（或直接不加 updated_at 条件，因 `BEGIN IMMEDIATE` 已锁死无并发 writer）
4. callback fail → 抛 `MaintenanceDrift`

对于**非 backfill 的老 callers**（`memory_ops` 内维护动作、`dedup_legacy.py`），保留旧 `expected_updated_at` 路径不变；`pre_execute_check` 和 `expected_updated_at` **互斥**（同时传 → assert）。

**v2.5 H4 反例测试**：
- `test_h4_plan_then_activation_touch_execute_succeeds` — PLAN 后并发线程做 `touch_recalled_memories_atomic(mem_id)` → EXECUTE `pre_execute_check` 判 stable_hash 未变 → UPDATE 成功
- `test_h4_plan_then_content_change_execute_drifts` — PLAN 后 UPDATE `content` → EXECUTE strict_hash 变 + stable_hash 变 → 抛 `MaintenanceDrift`
- `test_h4_plan_only_room_change_drifts` — PLAN 后 UPDATE `room` → stable_hash 变 → drift
- **删除 v2.1 的 `--ignore-drift-if-only-touch`**（reviewer 早前指出）；改为白名单排除。

**JSON-safe state_before 结构（v2.5 M3 修正——从 `_ALL_COLUMNS` 自动生成，避免手写漏字段）**：

Codex 复审指出 v2.4 白名单漏了 `tags/source_context/domain/history/comments/supersedes/linked_memories/event_date/emotion_arousal/valence/source_platform/anchored/resolved` 等 —— 用自动生成方案避免这类漏。

```python
# database.py 或 backfill 脚本内
_STATE_BEFORE_DRIFT_ALLOWED = frozenset({'updated_at', 'activation_count', 'last_activated'})
_STATE_BEFORE_EXCLUDE = frozenset({'embedding'})  # bytes 单独 hash

def state_before_snapshot(mem_row: dict) -> dict:
    """v2.5 M3: 从 _ALL_COLUMNS 自动生成 JSON-safe 白名单快照。
    返回 {'strict_hash', 'stable_hash', 'snapshot'} 三键。"""
    fields = [c for c in _ALL_COLUMNS if c not in _STATE_BEFORE_EXCLUDE]
    snap = {k: mem_row.get(k) for k in fields}
    emb = mem_row.get('embedding')
    snap['embedding_sha256'] = hashlib.sha256(emb).hexdigest() if emb else ''

    # strict = 全字段 hash
    strict_json = json.dumps(snap, sort_keys=True, ensure_ascii=False)
    strict_hash = hashlib.sha256(strict_json.encode()).hexdigest()

    # stable = 排除三个 volatile 字段后 hash（用于 backfill drift gate 判"只是 touch"）
    stable = {k: v for k, v in snap.items() if k not in _STATE_BEFORE_DRIFT_ALLOWED}
    stable_json = json.dumps(stable, sort_keys=True, ensure_ascii=False)
    stable_hash = hashlib.sha256(stable_json.encode()).hexdigest()

    return {'strict_hash': strict_hash, 'stable_hash': stable_hash, 'snapshot': snap}
```

**backfill `pre_execute_check` 语义**（v2.5 M3 收敛）：
- strict_hash 一致 → 完全没变，直接执行
- strict_hash 变 + stable_hash 一致 → 只 touch 了 volatile 字段，可继续
- stable_hash 变 → 真实漂移，drift skip

**新增测试**：`test_m3_snapshot_covers_all_columns` — 断言 `set(_STATE_BEFORE_DRIFT_ALLOWED | _STATE_BEFORE_EXCLUDE | fields) == set(_ALL_COLUMNS)`，未来加新列忘更新会立即红。

**owner_ai backfill**（v2.2 保留）：有 `subject_id` 且能判定独白 → 补；无 → report-only。

**prefix 修复**：`--check prefix` 只 report-only（v2 保留）。

### D-5：接入 CREATE 点（v2.3 M5：分路径处理）

**分两类**：

| 路径 | 校验失败处理 |
|---|---|
| **MCP / REST `remember()` 直路径**（`memory_ops.remember` 两处 CREATE）| **`raise ValidationError`**（新自定义异常），FastAPI wrapper 转 400；MCP tool 层捕获转结构化错误 `{"ok": False, "error": "validation_failed", "detail": ...}`。**不 continue**。 |
| **daemon 循环**（`compress_diaries:139` / `archive_old_work:235` / `distill_psychology:385`）| **v2.4 M-A：wrap 必须落在每个 daemon step 内 for 循环的单次 iteration 最外层**，不能塞进内部 helper。伪代码：<br>`for batch in batches:`<br>`    try:`<br>`        await _synthesize_and_remember(batch)  # remember() 可能 raise ValidationError`<br>`    except ValidationError as e:`<br>`        logger.warning(f"skip {batch.id}: {e}")`<br>`        continue`<br>三处 daemon 各自的 batch 变量名（`week` / `task_group` / `session`）在开工时按现有代码定名。|
| `insert_pending_memory` finalize 阶段 | 结构化拒绝 → ledger 记 `finalize_failed`，wrapper 转客户端错误 |

**新自定义异常**：`memory_validation.ValidationError(Exception)`，`validate_memory_write` / `validate_context_isolation`（strict 模式）改为 raise 此类型，而非 `ValueError`（区分度更好，避免误捕获）。

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
| **0** | `_WRITE_LOCK` + `_write_transaction()` ctx（H1 try/finally 修正）；**全部**共享 `_conn` 写路径改造（含 `activity_log.py` 3 处，H2）；`memory_ops` 走公开 helper 不接触锁/ctx（H3）；生产读路径迁 `_get_read_conn()`（H4）；抽取 `_set_memory_in_tx(conn, mem)`（H5）；10 条测试（含 grep 静态断言 4 条 + active-flag 恢复 2 条 + 并发 2 条 + activity_log 1 条 + 读连接 1 条）| grep 断言 4 类全过；并发/嵌套快速拒绝通；activity_log 走公开 helper | **3 d**（v2.2 是 2，v2.3 +1d 处理 activity_log 迁移 + 读连接迁移 + `_set_memory_in_tx` 抽取）|
| 1 | `context_kind` migration + 全链路 | 端到端持续测试通 | 1 d |
| 2 | `memory_validation.py` + `annotate_event` + soliloquy v2.1 + 34 单元测试 | 反例测试全过 | 1.5 d |
| 3 | `state_ttl.py` + 5 列 migration + `commit_state_supersede_atomic` + 隔离键分层 + 18 单元测试（含 4 条并发） | 并发恰好 1 条 active + shared/private layer 测试通 | 2 d |
| 4 | `apply_temporal_annotation()` 接入 5 入口 + 快照测试 5 条 | 5 入口各断言 | 1 d |
| 5 | `daemon.py:483 refresh_current_status` prompt 加时间约束 | mock LLM 收到含约束段 prompt | 0.5 d |
| 6 | 6 处 CREATE 接入 validation + daemon 新 step `archive_stale_states` | grep 6 处 + 集成测试 | 1 d |
| 7 | `scripts/data_health_backfill.py` plan/execute + 完整 state_before 比对 | 构造脏数据 → plan 报告 + drift 完整比对 pass | 1 d |
| 8 | VPS backfill plan → Ceci 审 → execute | audit 每条 + rebuild_all_corridors | 0.5 d |

**v2.5 总估时：11 d**（v2.1=9 → v2.2=10 +1d for Step 0 全量迁移 + State supersede 原子化；v2.3=11 +1d for activity_log 迁移 + 读连接迁移 + `_set_memory_in_tx` 抽取；v2.4/v2.5 不加 d，只是把契约写死 + 收敛 signature）

---

## v2.5 单元测试清单（约 75 条）

- Step 0（5 条）：`nested_fail_fast` / `concurrent_no_deadlock` / `touch_and_maintenance_serialize` / `all_write_paths_use_ctx` / `lock_not_referenced_in_memory_ops`
- 独白判定（4 条 v2.1）：`ai_about_user_not_flagged` / `source_actor_id_alone_insufficient` / `subject_ai_via_alias` / `subject_other_ai_not_flagged`
- D-1 核心（10 条 v2 保留）
- 独白 owner_ai 补齐（3 条）
- D-6 context_kind isolation + 3 mode（10 条：observe/redirect/strict 各含 raise/no-raise）
- Event annotation（4 条）
- State ttl（18 条含 4 条并发 supersede + shared/private layer 4 条）
- 时间注入 5 入口（5 条）
- Backfill script（6 条含完整 state_before drift 比对）

全套目标 420+（1.7 基础 352 + PR1 v2.3 ~70：Step 0 10 条含 activity_log/读连接/active-flag 恢复；+ 分模式 CREATE 校验 5 条）。

---

## Q5｜风险

### 高

1. **Step 0 全量写 + 读路径迁移影响面**（v2.3 收敛）
   写：`insert_pending_memory / update_memory_status / set_memory / write_intent_ledger / activity_log` 全部改 ctx。读：`memory_ops / corridor / smart_context / gateway / mcp_server` 里所有共享 `_conn` 读迁到 `_get_read_conn()`。
   **缓解**：4 类 grep 断言（写只走 ctx / 锁只在 database / 生产不引用 ctx / 读走 read_conn）+ 独立提交 + Codex 复审。**若某处漏迁，grep 立即红**。

2. **`_set_memory_in_tx` 抽取的正确性**（H5 直接对应）
   `set_memory()` 现有约 100 行含 UPSERT + `memories_vec` + `vec_id_map` + preserve 保护。抽取时如果漏一段（比如 vec_id_map 分配），state 落库后 vector recall 找不到。
   **缓解**：（a）抽取前先跑一遍现有 `test_set_memory` 全套确认基线；（b）抽完后新增 `test_state_supersede_new_state_appears_in_vector_recall` 断言 supersede 后新 state 立即能被 `vector_search` 找到；（c）Codex 复审专门看这个 diff。

3. **`commit_state_supersede_atomic` 与 `remember()` 的分流点**（v2.3 H5 定死）
   已明确：`remember()` 两处 CREATE 分支的 `set_memory()` 之前分流；缺 key 时 skip 落 report-only。开工先跑通 spike 30 分钟。

4. **`context_kind` 全链路串**（v2 保留）

### 中

5. **State supersede shared 语义**：文档写清。
6. **Backfill snapshot_hash 覆盖率**：白名单未列的字段变化不会触发 drift，但也不会误伤。若发现漏字段（如未来加新列），白名单要同步更新——加个 pytest 从 `_ALL_COLUMNS` 反查白名单完备性。
7. **observe → redirect → strict 上线节奏**：3 周切完，靠 grafana 看 warning count 曲线。

### 低

8. event_date 与 created_at 都缺 → annotate 返回原 mem。
9. `MEMORY_HUB_CONTEXT_ISOLATION_MODE` 值大小写不敏感；v2.3 默认 `observe`（安全默认）。

---

## 附：文件改动预览 v2.5

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
  docs/phase20-implementation-plan.md (PR1 章节同步 → v2.5 唯一依据)
```

约 17 个文件、+3000 行 additive、65 条新测试。

---

## 交付流程 v2.5

1. **本方案 v2.6 推分支** `phase20/pr1-plan-v3`（含 implementation-plan.md 同步）
2. Ceci + Codex 都审 v2.6
3. 都过 → 开工分支 `phase20/pr1-data-health`
4. **Step 0 独立提交 + Codex 复审 pass 后**才继续 D-*
5. 全套测试 415+ 通过
6. 开 PR → Codex 复审 2 轮
7. 合并 + VPS 部署
8. VPS backfill plan → Ceci 审 → execute
9. Ceci 观察 1 周体感
10. 稳定后开 PR2
