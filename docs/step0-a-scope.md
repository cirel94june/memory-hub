# Step 0-A · Scope Amendment（Codex 定稿）

> 分支：`phase20/pr1-data-health`（从 `origin/main = 604bc7297d7ab63d9f1efb2af6561ebb5ec95e12`）
> 施工依据：[phase20-PR1-施工方案.md v2.9](phase20-PR1-施工方案.md)
> 本文档：Step 0 的施工范围收窄记录，避免与 D-2/state schema 撞车

---

## 背景

Codex 七审通过 v2.9 时批准开工 Step 0，但八审指出 v2.9 的 Step 0 原始清单里混入了依赖 state schema 的项：`commit_state_supersede_atomic()` / `_state_supersede_in_tx()` / `_payload_fingerprint()` / audit `operation_id` migration / `valid_from / valid_until / last_confirmed_at / state_ttl_days / context_kind` 五列对 `_ALL_COLUMNS` 的更新。若 Step 0 加列进 `_ALL_COLUMNS` 却不做 migration，现有 `set_memory()` 会立即对不存在的列执行 INSERT；若顺手加 migration，又违反"不做 D-2 生产 schema 变更"的闸门。

因此 Step 0 拆成 Step 0-A（本次）+ Step 0-B（跟 D-2 一起走）。**不升级为 v3.0，只作为 scope amendment 记录**。

---

## Step 0-A · 施工范围（本 commit）

### 新增文件

- `resolve_patterns.py` — 从 `memory_ops.py:630` 迁入 `_matches_resolve_pattern()` + 所有 resolve constants/regex（H1 循环依赖前置）
- `tests/test_step0_write_lock.py` — nested fail-fast / active-flag 恢复 / 并发不死锁 / all_write_paths_use_ctx
- `tests/test_step0_read_conn.py` — DB_PATH 失效 / close_thread_read_conn / 26 read helpers AST 双向对齐
- `tests/test_step0_atomic_helpers.py` — touch_recalled / check_auto_resolve / activity_log helpers

### 改动文件

**`database.py`**
- 顶部新增 `_WRITE_LOCK: threading.Lock` + `_in_write_tx: threading.local` + `_write_transaction()` context manager（v2.9 H1 修正版）
- 顶部新增 `_prepare_memory_value(key, value)` — 忠实抽取现有 `set_memory._prep`
- 顶部新增 `_PURE_READ_HELPERS: frozenset` 26 项清单
- 修 `_get_read_conn()`：加 DB_PATH 失效（缓存 `read_conn` + `read_db_path`）+ `close_thread_read_conn()` 供 shutdown
- `set_memory()` 变为 `with _write_transaction() as conn: _set_memory_in_tx(conn, mem)` + 抽出 `_set_memory_in_tx(conn, mem)` 内部 helper（含 memories_vec + vec_id_map + preserve 保护）
- 现有 `commit_maintenance_atomic` / `commit_finalize_atomic` / `close_stale_intent_atomic`：函数体内 `BEGIN IMMEDIATE / commit / ROLLBACK` 替换为 `with _write_transaction() as conn:`
- 现有 `insert_pending_memory` / `update_memory_status` / `mark_replaced` / `close_stale_intent` / `write_intent_ledger`：同上迁移
- 现有 `insert_proposal` / `update_proposal_status` / `insert_audit` / `upsert_profile` / `approve_profile` / `supersede_profile` / `delete_profile` / `upsert_person` / `delete_person` / `remove_memory`：同上迁移
- 新增 `touch_recalled_memories_atomic(mem_ids, now_iso)` — 从 `memory_ops._touch_recalled_memories:737` 写部分迁入
- 新增 `check_auto_resolve_atomic(candidate_ids, new_content, source_ai) -> list[str]` — 从 `memory_ops._check_auto_resolve:821` 整段迁入
- 新增 `append_activity_log_atomic(row)` + `trim_activity_log_atomic(keep_last_n)` — 从 `activity_log.py` 迁入
- 26 个 pure read helpers 全部改用 `_get_read_conn()`
- `init_db()` 加 `CREATE TABLE IF NOT EXISTS activity_log ...`（从 `activity_log.py:123` 迁入）

**`memory_ops.py`**
- 删除 `_WRITE_LOCK` 定义（v2.9 契约：锁只属于 database.py）
- `_matches_resolve_pattern` 定义删除，改为 `from resolve_patterns import _matches_resolve_pattern`
- `_touch_recalled_memories:737` 写部分改调 `database.touch_recalled_memories_atomic()`
- `_check_auto_resolve:821` 整段改调 `database.check_auto_resolve_atomic()`

**`activity_log.py`**
- 删除自己的 `_get_conn()` + 建表 + INSERT + DELETE + `conn.commit()`
- 改为 `from database import append_activity_log_atomic, trim_activity_log_atomic`

**`corridor.py` / `smart_context.py` / `gateway.py` / `mcp_server.py` / `main.py`**
- 所有共享 `_conn` 读迁 `_get_read_conn()`（间接通过 database public helpers 已迁移无感；如有直接 `database._get_conn()` 调用需换）
- `main.py` shutdown hook 加 `database.close_thread_read_conn()`

### 保持不动（等 Step 0-B/D-2 一起走）

- `commit_state_supersede_atomic()` — 需 state schema
- `_state_supersede_in_tx()` — 需 state schema
- `_payload_fingerprint()` — 需 state 字段进 `_ALL_COLUMNS`
- `IdempotencyConflict` 异常 — state 分流点未建立
- audit `operation_id` 列 + `UNIQUE(action, target_id, operation_id)` 部分索引 — 归 Step 0-B
- `_ALL_COLUMNS` 加 `valid_from / valid_until / last_confirmed_at / state_ttl_days / context_kind` — 归 D-2 一起
- `state_ttl_days` 在 `_prepare_memory_value` 里的默认值 7 —— **本次也不加**（列不存在，加了就无效但也不会红；为避免施工方误以为已生效，先不埋，等 D-2 加列同时加默认）

**注**：`_prepare_memory_value` 本次只保留 v2.9 五条既有语义，state_ttl_days 分支等 D-2。

### 已确认现状（无需 spike）

`database.py:31–53` 已经是 `threading.local` 缓存单读连接。本 commit 只补 DB_PATH 失效 + shutdown close，不改缓存策略。

---

## Step 0-B（后续，与 D-2 同一提交）

- `_ALL_COLUMNS` 加 5 列 + `_preserve_on_empty` 加 3
- migration 5 列 + audit `operation_id` 列
- `_prepare_memory_value` 加 `state_ttl_days` 默认 7 分支
- `_payload_fingerprint` + `_FINGERPRINT_FIELDS`
- `_state_supersede_in_tx(conn, ...)` + public wrapper `commit_state_supersede_atomic()`
- `IdempotencyConflict` 异常
- state supersede 全套并发/幂等/CRQ fail-closed 测试（约 15 条）

---

## 验收（Step 0-A）

- `git diff --name-only origin/main..HEAD` 只出现本文档列出的文件
- 全套现有测试通过（Phase 1.7 基础 352 条）
- 新增 Step 0-A 测试 ~20 条通过
- grep 断言 4 类过：
  - `_write_transaction` / `_WRITE_LOCK` 只在 database.py 出现（`':!tests/'` 排除）
  - `database._get_conn()` 生产模块 0 命中（读走 read_conn，写走 helpers）
  - `conn.commit()` 只在 `database.init_db()` 出现
  - `_get_conn()` 在 activity_log.py 0 命中
- AST 测试：26 个 pure read helpers 函数体不含 `_get_conn()`；database.py 内所有 public 纯 SELECT 函数必须在 `_PURE_READ_HELPERS` 内
- `set_memory` golden 对比：抽取前后同一输入 DB row 逐字段一致
- **不做** 生产 backfill、VPS DB 修改、生产 schema 迁移
- proposal pending bug 不混进本提交（除非 baseline 时证明直接阻塞 Step 0 测试）

---

## 提交约定

- Step 0-A 独立 commit（不与 Step 0-B / D-* 合），message 里明确 scope amendment
- Codex 复审 pass 后才继续 D-*
- 中途若 Step 0-A 自身触发 v3.0 级别问题，停手报告，不擅自推进
