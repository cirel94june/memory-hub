# Proposal Pending Bug 修复方案 v1

> 分支：`phase20/proposal-pending-fix-plan-v1`（doc-only，等 Codex 复审）
> 依赖：main `604bc72`
> 独立于 PR1 Step 0-A（`phase20/pr1-data-health`），不做交叉

---

## 背景

Codex 已给出根因报告（转发在方案末尾"附：根因报告原文"）。核心：新 `auto_approve` proposal 卡 `status=pending`，2026-07-21 `51e853e` 引入，累积 15 条 ≥28 天。

4 层根因（Codex 已定）：
1. `auto_approve` 不是原子操作（proposal INSERT → `_promote_proposal` → `update_proposal_status` 三步）
2. `asyncio.CancelledError` 不属于 `Exception`，`except Exception:` 漏接
3. `_finalize_pending_memory` / 10 分钟 sweep 只管 `memories.pending`，不查 `proposals`
4. `maintain` 不修 proposals（`retriage_pending_proposals` 需单独 endpoint）

修复必须满足 Codex 5 条约束（见 §Q3）。

---

## Q1｜要解决什么问题

- 存量：15 条 auto_approve pending 卡池（正式记忆有的已写、有的未写，需分类处理）
- 结构：async 中断 / 崩溃 / 重启后自动恢复；两个 worker 不能重复晋升；同一 proposal 重放不能多创建

## Q2｜现状（Hub 是什么样）

### 表结构（database.py `_PROPOSAL_COLUMNS`）

已有：`id / content / proposed_room / evidence_excerpt / proposer_ai_id / confidence / status / created_at / reviewed_at / reviewed_by / reject_reason / triage_reason / applied_memory_id / failure_reason / ...`

**没有**：`promotion_claim_id` / `promotion_claim_at`（对应 PR1 里 memories 已经的 `finalize_claim_id/at` 契约）；也**没有** ledger-style 双阶段表。

### 提交时序（当前 `memory_ops.py:1301-1327`）

```
1. proposal["status"] = "pending"
2. database.insert_proposal(proposal)            # commit 事务 1
3. result = await _promote_proposal(proposal)    # 内部再走 remember() → 完整落库
4. database.update_proposal_status(prop_id, "auto_approved", applied_memory_id=mem.id)  # commit 事务 2
5. _write_audit(...)                             # commit 事务 3
```

3 段之间任一 await 边界被 cancel / 服务重启 / 进程死 → 留 pending。

`except Exception:` 分支只处理**同步或普通业务异常** → `CancelledError` 直接透传 → `promotion_failed` 也不会写。

### memories 侧的成熟机制（可对齐）

- `finalize_claim_id / finalize_claim_at` 列（PR1 之前就有）— TOCTOU-safe 原子 claim
- `async_remember_ledger` 表 — 两阶段（in_flight → active/replaced/failed）
- `close_stale_intent_atomic()` — 崩溃恢复（Codex confirmed 覆盖 memories，**不覆盖 proposals**）
- `pending_sweep.py` — 10min 高频 sweep memories.pending（**不覆盖 proposals**）

**关键设计判断**：proposals 侧需要**独立但对称**的一套（Codex 约束 4），不复用 memories 的 sweep / ledger（两套 wire 独立）。

## Q3｜打算改成什么

按 Codex 5 条约束逐条对应设计。

### C1｜Promotion 原子抢占（两 worker 不能同时晋升）

**新增列**（`proposals` 表 migration）：
```sql
ALTER TABLE proposals ADD COLUMN promotion_claim_id TEXT NOT NULL DEFAULT '';
ALTER TABLE proposals ADD COLUMN promotion_claim_at TEXT NOT NULL DEFAULT '';
```

**新增 helper** `database.try_claim_promotion(proposal_id, claim_token, stale_after_minutes=15) -> bool`：
- 走 `_write_transaction()` 内单条 UPDATE + rowcount 判定（对齐 `try_claim_finalize`）
- 只 claim `status='pending' AND triage_reason IN ('auto_approve', 'auto_approve_silent')` 的 proposal
- 允许 stale 抢占（原 claim > stale_after_minutes → 视为 crashed holder，可 takeover）

**Promotion 流程改造**（`memory_ops.py:1301`）：
```
1. proposal["status"] = "pending"
2. database.insert_proposal(proposal)
3. claim_token = uuid.uuid4().hex
   won = database.try_claim_promotion(prop_id, claim_token)
   if not won: return {..."proposal_status": "in_flight_by_other"}
4. try:
       result = await _promote_proposal_with_stable_id(proposal, claim_token)
       # 内部走 database.commit_promotion_atomic(...) 单事务：
       #   - INSERT/UPSERT memory (稳定 ID，见 C2)
       #   - UPDATE proposal SET status='auto_approved', applied_memory_id=mem.id,
       #                          promotion_claim_id='', promotion_claim_at=''
       #   - INSERT maintenance_audit
       # 上述三写在一个 _write_transaction() 内 → 全成或全回滚
   except (asyncio.CancelledError, KeyboardInterrupt):
       # 保持 claim 不 release；下次 recovery sweep 会重跑（C3）
       raise
   except Exception as e:
       database.mark_promotion_failed(prop_id, str(e), claim_token)
       # ↑ 内部 UPDATE：status='promotion_failed', failure_reason=e,
       #   promotion_claim_id='', promotion_claim_at=''（释放 claim）
       return {...}
```

**关键点**：
- **`except (CancelledError, KeyboardInterrupt): raise` 必须放在 `except Exception` 之前**（不然被 `Exception` 吃）
- Cancel 时**不释放 claim**（stale timeout 后 recovery 拿）
- 普通异常 catch 后**立即释放 claim + 标 failed**（防止占用 claim 无限期）

### C2｜Proposal ↔ Memory 幂等关联（重放同一 memory_id）

**关键决策**：memory ID 由 proposal ID 派生（deterministic），不再走 `mem_{now_ms}_{ns}` 随机。

**方案**：给 `_promote_proposal` 加参数 `existing_memory_id`（复用现有 `remember(existing_id=...)` 路径）：
```python
def _memory_id_for_proposal(prop_id: str) -> str:
    return f"mem_from_prop_{prop_id}"
```

`_promote_proposal_with_stable_id(proposal, claim_token)`：
- 计算 `mem_id = _memory_id_for_proposal(proposal['id'])`
- 调 `remember(..., existing_id=mem_id)` — `remember()` 已有幂等分支（`set_memory` UPSERT + `_preserve_on_empty` 保护 crq/link/created_at）

**要求 `remember()` 支持 `existing_id` 参数**（如果目前没有：加参数直穿到 `set_memory({..., "id": existing_id, ...})`）。

**唯一性防护**（database migration）：
```sql
CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_id_unique ON memories(id);
-- (id 已是 PRIMARY KEY，索引冗余但显式)
```

同一 proposal 重跑 → memory UPSERT（ON CONFLICT DO UPDATE），不会创建重复。

### C3｜崩溃恢复同一 memory（不重复创建）

**新增 helper** `database.list_stale_promotion_claims(older_than_minutes=15) -> list[dict]`：
- SELECT proposal WHERE `status='pending' AND promotion_claim_id != '' AND promotion_claim_at < cutoff`
- 与 `list_stale_intent_ledgers` 对称

**新增 helper** `database.reconcile_promotion_atomic(proposal_id, claim_token, reason) -> dict`：
- 事务内 SELECT 派生 `mem_id = _memory_id_for_proposal(prop_id)`
- 检查 memories 表 `mem_id` 是否已存在且 `status='active'`：
  - **是** → 已写但 proposal 未 update；`UPDATE proposal SET status='auto_approved', applied_memory_id=mem_id, promotion_claim_id=''`；写 audit `promotion_reconciled_active`
  - **否** → 释放 claim（`promotion_claim_id=''`）让下轮 promotion worker 重跑；写 audit `promotion_claim_released_for_retry`
- 全在同一 `_write_transaction()` 内

**Recovery sweep**（新模块 `proposal_sweep.py`，或在 `pending_sweep.py` 内独立 task）：
```python
async def proposal_recovery_loop(interval_sec=600):  # 10 分钟对齐
    while True:
        try:
            stale = database.list_stale_promotion_claims(older_than_minutes=15)
            for prop in stale:
                database.reconcile_promotion_atomic(
                    prop["id"], prop["promotion_claim_id"], "sweep_stale_claim",
                )
        except Exception:
            logger.exception("proposal recovery loop iteration failed")
        await asyncio.sleep(interval_sec)
```

在 `main.py` lifespan 独立启动此 task（**与 memories `pending_sweep` 并列，不合并**——Codex 约束 4）。

### C4｜专用 proposal recovery，不混 memory skeleton sweep

**明确设计**：
- `pending_sweep.py` 继续只管 `memories.pending` 骨架（Phase 1.7 PR C 定义）
- 新 `proposal_sweep.py` 只管 `proposals.pending + auto_approve*` claim
- 两者调度频率、超时阈值可独立调；日志前缀 `[memory-sweep]` / `[proposal-sweep]` 便于区分
- 两者共用 `_write_transaction()` ctx（同一把 `_WRITE_LOCK`），锁不冲突

### C5｜覆盖测试（Codex 明列 5 类）

新增 `tests/test_proposal_promotion.py`（预估 12+ 条）：

**取消**：
- `test_promote_cancelled_mid_way_leaves_claim_intact` — promote 里 `await` 处 cancel → claim 仍在、proposal 仍 pending、无 promotion_failed 状态
- `test_promote_normal_exception_releases_claim_and_marks_failed` — 内部普通异常 → status=promotion_failed + claim 释放

**崩溃**：
- `test_reconcile_promotion_memory_exists_marks_approved` — mem_id 存在 → proposal 标 auto_approved + applied_memory_id 对上 + audit 写入
- `test_reconcile_promotion_memory_absent_releases_claim` — mem_id 不存在 → 只清 claim，proposal 仍 pending，下轮再跑

**重启**：
- `test_end_to_end_cancel_then_recovery_ok` — cancel → sweep → 断言 proposal 最终 auto_approved 且 memory 只 1 条（不重复）

**并发**：
- `test_two_workers_claim_only_one_wins` — 2 线程同时 `try_claim_promotion(same_id)` → 恰好一个返 True
- `test_stale_claim_takeover_after_timeout` — 手动 backdate `promotion_claim_at` 15+ 分钟 → 第二次 claim 成功

**状态更新失败**：
- `test_commit_promotion_partial_write_all_rollback` — mock audit INSERT 抛异常 → memory UPSERT 也回滚，proposal 状态不变，claim 保留
- `test_idempotent_replay_same_prop_id_no_duplicate_memory` — 用同 proposal.id 连调两次 `_promote_proposal_with_stable_id` → memories 表恰好 1 条（同 mem_id UPSERT 复用）

**存量迁移**：
- `test_migration_backfills_empty_claim_columns` — 现有 15 条 pending 迁移后 `promotion_claim_id=''` / `promotion_claim_at=''` 默认值

---

## Q4｜施工步骤 + 验收

| Step | 工作 | 验收 |
|---|---|---|
| **S0** | 存量分类扫描（不动数据）| 15 条 pending 分类：mem_id 已存在（可迁 auto_approved）/ 未存在（需人工判断是否再跑）|
| S1 | `database.py` 加 2 列 migration + 单元测试 | init_db 重跑通过、老 DB 加列成功、无数据丢失 |
| S2 | `database.try_claim_promotion` + `list_stale_promotion_claims` + `reconcile_promotion_atomic` + `commit_promotion_atomic` + `mark_promotion_failed` helpers | 单元测试 5 条：claim/stale/reconcile/commit/mark_failed 各覆盖 |
| S3 | `_memory_id_for_proposal` + `remember(existing_id=...)` 支持（若尚缺）| existing_id 走 UPSERT 无重复；测试 3 条 |
| S4 | `memory_ops.py:1301` promote 流程改造（CancelledError catch 优先 + claim/commit_promotion_atomic 走单事务）| 全套 promote 测试通过（现有 22 条 + 新增 12 条 = 34 条）|
| S5 | `proposal_sweep.py` 新模块 + `main.py` lifespan 启动 | 独立测试：mock stale claim → recovery 生效；不动 memory-sweep 行为 |
| S6 | 存量迁移脚本 `scripts/reconcile_stuck_promotions.py`（dry-run/execute）| dry-run 输出 15 条分类；execute 走 reconcile_promotion_atomic 逐条 |

**总估时**：4 天（Step 0-A 开工经验：单个 helper 大概半天含 Codex 复审）

---

## Q5｜风险

### 高

1. **存量 15 条的分类可能非一一对应**  
   有些 proposal 可能只 INSERT 了 proposal 行，`_promote_proposal` 根本没进（例如服务在 step 2 后立即 cancel）→ 没有对应 memory。派生 `mem_id = mem_from_prop_{prop_id}` 查也不存在。这类走"释放 claim + 让 sweep 重跑"分支。
   **缓解**：S0 分类扫描 + 每条人工审前的三点核对（Codex 应急期禁止动作里定的）。

2. **`remember()` 已经有很复杂的 dedup / relation classification 路径**  
   给它加 `existing_id` 参数后走 UPSERT 分支，可能跳过 dedup。要么(a) `existing_id` 分支只做单纯 UPSERT 不走 dedup（信任 claim 已确保幂等）；要么(b) dedup 逻辑对稳定 mem_id 兼容（会自然收敛，因 UPSERT 命中同 id）。**倾向 (a)**，简单可控。
   **缓解**：新增测试 `test_existing_id_bypass_dedup_uses_upsert`。

### 中

3. **CancelledError 时不释放 claim → stale timeout 期间该 proposal 不能被别的 worker 抢**  
   15 分钟 timeout。若真在 15 分钟内多次 cancel + restart → 需要等一次 timeout 才恢复。**可接受**（recovery sweep 频率 10 min，等价一次 sweep 周期）。

4. **Recovery sweep 循环出错要能自愈**  
   外层 try/except，log 后继续。参考 `pending_sweep.py` 现有模式。

### 低

5. **AST 闸门（Step 0-A #2b）需接纳新 database helpers**  
   `try_claim_promotion` / `list_stale_promotion_claims` / `reconcile_promotion_atomic` / `commit_promotion_atomic` / `mark_promotion_failed` 都是新写函数，会走 `_write_transaction()` — 自然满足闸门。**无需改闸门代码**。

6. **测试可能污染 promotion_claim_id 列**  
   pytest 每条测试新 tmp_path DB → 隔离良好。

---

## 应急期（本方案上线前）

Codex 禁止动作已列（不 maintain、不批量 retriage、不盲目 approve）。**方案 pass + 部署前**，Ceci 若真要人工救 pending 池，按 Codex 三点核查：
1. `applied_memory_id` 是否已存在
2. 正式库是否已有相同内容或来源的 active 记忆
3. `maintenance_audit` 是否记录过 `create`

只有三点全"否"才 approve；否则等 recovery sweep。

---

## 与 PR1 Step 0-A 的关系

- 完全独立分支（`phase20/proposal-pending-fix-plan-v1` from main）
- 不依赖 PR1 Step 0-A（PR1 在 `phase20/pr1-data-health`，还没合入 main）
- 施工时机建议：**PR1 Step 0-A 合入 main 后再开工本方案**，可以直接复用 `_write_transaction()` / `_prepare_memory_value` / `_set_memory_in_tx` / `_IN_TX_HELPERS` 契约

若 PR1 一时未合，本方案的 helpers 用**当前 main 的 `_conn.execute("BEGIN IMMEDIATE"); commit()` 模式**也可以（后续 rebase 时再走 ctx），不阻塞。

---

## 交付流程

1. 本方案 v1 推 `phase20/proposal-pending-fix-plan-v1` 分支（doc-only）
2. Ceci + Codex 都审 v1
3. 都过 → 开开工分支 `phase20/proposal-pending-fix`
4. 分批施工（S0 → S1 → S2 → ...），每批 Codex 复审
5. 全套测试通过 → 开 PR → Codex ultra 复审 → 合入
6. **合入后按 S6 走存量迁移**（先 dry-run，Ceci 审，再 execute）

---

## 附：Codex 根因报告原文

```
审计版本：Memory Hub main，SHA 604bc729
症状：新 auto_approve proposal 卡 status=pending 池不入 canonical。
30 条 pending 中 15 条 auto_approve 卡住，最早 2026-07-24T10:10:10，28 天累积。
结论：真 bug，2026-07-21 51e853e 引入，非 PR C 引入。

根因（4 层）：
1. auto_approve 不是原子操作（proposal INSERT → _promote_proposal → status update 三步）
2. except Exception 不覆盖 asyncio.CancelledError（Py 3.12）
3. _finalize_pending_memory 只管 memories.pending，不查 proposals
4. maintain 不修 proposal + retriage 需单独 endpoint

修复约束（5 条必须满足）：
1. Promotion 有原子抢占，不能两 worker 同时晋升
2. proposal 和生成的 memory 有稳定、幂等的关联（重放复现同一 memory_id）
3. "memory 已写、proposal 未更新" 后重启，能恢复为同一条不重复创建
4. 专门的 proposal recovery，不混用 memory skeleton sweep（两套独立但对称）
5. 覆盖测试：取消/崩溃/重启/并发/状态更新失败

应急期禁止：
❌ 不触发 maintain
❌ 不批量调用 retriage
❌ 不直接盲目 approve pending

手动处理每条 proposal 前必须先查三点：
- applied_memory_id 是否已存在
- 正式库是否已有相同内容/来源的 active 记忆
- maintenance_audit 是否记录过 create
```
