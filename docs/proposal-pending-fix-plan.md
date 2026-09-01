# Proposal Pending Bug 修复方案 v2

> 分支：`phase20/proposal-pending-fix-plan-v2`（doc-only；从 main `da88279`，Step 0-A 后）
> 硬前置：**Step 0-A 已合并**（不可"先按旧事务模式开工再 rebase"——事务边界 helper 所有权全变了）
> 独立于 UX 层："对话式 Proposal 审批 UX"作为独立"下一步体验功能" PR，不塞进本修复
>
> **版本历史**：
> - v1（作废）：`remember(existing_id=...)` 假装原子提交；recovery 只清 claim 不重跑；stale worker 无 fencing；旧 40 条按新 deterministic ID 自动 reconcile
> - **v2**（本文）：Codex 4 High + 3 Medium + 1 Low 全部收敛

---

## v1 → v2 关键设计变更

| 项 | v1 | v2 |
|---|---|---|
| Atomic 层 | `_promote_proposal_with_stable_id → remember(existing_id=)` | **两阶段**：无副作用 payload 构造 + `commit_promotion_atomic()` 单事务 commit |
| Recovery 语义 | 只释放 stale claim | 扫 stale + 空 claim + 原子换新 token + **实际执行 promotion** |
| Fencing | 未提 | UPDATE/INSERT 全部 `WHERE promotion_claim_id=? AND status='pending'` + rowcount==1 |
| 旧 40 条 | deterministic ID 自动 reconcile | **只 report-only 人工审**，不进 auto retry |
| `needs_review` | 未明说 | Recovery 永远不 auto-approve `needs_review` |
| Migration 幂等 | `ALTER TABLE ADD COLUMN` 直接跑 | `PRAGMA table_info(proposals)` 检查后再加 |
| Sweep 生命周期 | 简单 sleep loop | main.py lifespan cancel + await drain + 计数输出 |

---

## Q1｜要解决什么问题

- **存量**：40 条 auto_approve pending 卡池（原报告 15 条，2 周涨到 40 — bug 每天累积；oldest `2026-07-24`）
- **结构**：async 中断 / 崩溃 / 重启后自动恢复；两 worker 不能重复晋升；同一 proposal 重放不能多创建；`needs_review` 严格不 auto-approve

## Q2｜现状

### 现有 promote 流程（3 步非原子）
`memory_ops.py:1301-1327`：
1. `proposal["status"] = "pending"` → `database.insert_proposal(proposal)` [tx 1 提交]
2. `result = await _promote_proposal(proposal)` — 内部走 `remember(quick=False)` 完整 dedup + relation 分类 + 落库 [tx 2..N]
3. `database.update_proposal_status(prop_id, "auto_approved", applied_memory_id=result["id"])` [tx N+1]

**bug**：3 段之间任何 await 边界 cancel / restart / crash → `pending` + `promotion_claim_*` 都不存在（本次修复引入）→ 永远卡池。

### `_promote_proposal` 走完整 `remember(quick=False)` 的隐患（v2 H1 依据）
`remember(quick=False)` 内部：
- `vector_search` 找相关 → 关系分类 `analyzer.classify_relation`
- 若 dedup 命中或分类为 supplement/supersede → **返回一个跟传入 payload 不同 ID 的 memory**

也就是说 `_promote_proposal` 返回的 `result["id"]` **不是** `f"mem_from_prop_{prop_id}"`。Recovery 用这个 deterministic ID 去查 memories 表 → 找不到 → 判断"没写"→ 重新跑 promotion → **重复创建**。

**但 Trace 上游**：`_promote_proposal` 由 `_create_proposal` 里 auto_approve 分支调用；到达 auto_approve 时 `_create_proposal` 本身已经做过一遍 related_mems + maintenance action 判断（`resolve_thread` / `reopen_thread` / `_map_relation_to_action` / …），**只有** related_mems 判断为 "无匹配 or unrelated" **才**落入 auto_approve 分支。所以 `_promote_proposal` 再走一遍 dedup 是**冗余且有害**——它可能拿到跟第一次判断不同的结果（数据在两次判断之间被别的写路径变了）。

v2 的核心决策：**auto_approve promote 只做 "insert-as-new"，不再重跑 dedup**。deterministic mem ID 唯一被 UPSERT 覆盖，与 `remember()` 的复杂路径解耦。

### 已确认（Step 0-A 后）可复用契约

- `_write_transaction()` — 非重入、锁在 database.py 内、事务原子
- `_set_memory_in_tx(conn, mem)` — in-tx helper，接受已开 conn；`_preserve_on_empty` 保护
- `_IN_TX_HELPERS` 显式清单 + 跨文件 AST 闸门
- `_get_read_conn()` — 独立只读连接，DB_PATH 失效缓存
- AST 闸门 fail-closed：任何 receiver `.commit()/.rollback()` / 非白名单 PRAGMA / 动态 SQL 一律违规

---

## Q3｜设计（按 Codex 4H + 3M + 1L 逐条对应）

### C1【H1 修】两阶段 promotion：无副作用 payload + 原子 commit helper

**Phase 1（事务外，无副作用）** — `_build_promotion_payload(proposal) -> dict`
- 纯数据变换：从 proposal 字段 → memory 字段
- **不查 DB，不写 DB，不走 dedup**
- `id = f"mem_from_prop_{proposal['id']}"` deterministic
- 返回完整 memory dict 可直接喂 `_set_memory_in_tx`

**Phase 2（database.py 内新 in-tx 原子 helper）** — `commit_promotion_atomic(prop_id, claim_token, mem_payload, source_ai) -> dict`：
```python
def commit_promotion_atomic(prop_id, claim_token, mem_payload, source_ai):
    """全部动作在一个 _write_transaction() 内：
      1. Fencing gate: UPDATE proposals SET status='auto_approved', ...
         WHERE id=? AND status='pending' AND promotion_claim_id=?
         若 rowcount != 1 → raise PromotionClaimLost (让 caller 跳过)
      2. _set_memory_in_tx(conn, mem_payload)  # UPSERT deterministic id
      3. INSERT INTO maintenance_audit (action='promotion_committed', ...)
    """
    with _write_transaction() as conn:
        now = _now_iso()
        cur = conn.execute(
            "UPDATE proposals SET status='auto_approved', "
            "applied_memory_id=?, reviewed_at=?, reviewed_by='system', "
            "promotion_claim_id='', promotion_claim_at='' "
            "WHERE id=? AND status='pending' AND promotion_claim_id=?",
            (mem_payload['id'], now, prop_id, claim_token)
        )
        if cur.rowcount != 1:
            # claim 已被 stale takeover / 状态已变 → 放弃，不写 memory
            raise PromotionClaimLost(
                f"proposal {prop_id} claim {claim_token!r} no longer valid"
            )
        _set_memory_in_tx(conn, mem_payload)
        conn.execute(
            "INSERT INTO maintenance_audit "
            "(action, target_id, new_content, decision_reason, "
            " state_before, state_after, source_ai, auto_executed, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("promotion_committed", mem_payload['id'],
             mem_payload.get("content", ""),
             f"auto_approve promote proposal={prop_id}",
             json.dumps({"proposal_id": prop_id, "status": "pending"}),
             json.dumps({"memory_id": mem_payload['id'], "status": "auto_approved"}),
             source_ai or "", 1, now)
        )
    return {"memory_id": mem_payload['id'], "proposal_id": prop_id}
```

**加入 `_IN_TX_HELPERS`**：`commit_promotion_atomic` **不**加入（它是 public helper 自带 `_write_transaction`）；只有 `_set_memory_in_tx` 已在清单里。

**PromotionClaimLost 异常**：新 module-level exception，caller 应 catch 后当作 no-op（记录 metric 就够）。

### C2【H2 修】Recovery sweep 真正完成 promotion，不只清 claim

**新增 `list_promotion_candidates_for_recovery(older_than_minutes=15)` helper**（database.py）— 返回两类可 recover 的 proposal：

1. **stale claim**：`promotion_claim_id != '' AND promotion_claim_at < cutoff`（原 worker 崩了）
2. **orphan pending**：`promotion_claim_id = '' AND triage_reason IN ('auto_approve', 'auto_approve_silent') AND status='pending' AND created_at < cutoff`（原 worker 崩在 claim 之前）

**明确排除**：`triage_reason='needs_review'` / `human_pending` / 任何非 auto_approve 分支 — Recovery **永远不动**这些（v2 L1 修）。

**`proposal_sweep.py` 主循环**：
```python
async def proposal_recovery_loop(interval_sec=600):
    while not _shutdown.is_set():
        try:
            candidates = database.list_promotion_candidates_for_recovery(older_than_minutes=15)
            for prop in candidates:
                # 原子换新 token（对 stale 是 takeover；对 orphan 是首次 claim）
                new_token = uuid.uuid4().hex
                won = database.try_claim_promotion(
                    prop["id"], new_token,
                    expected_current_claim=prop["promotion_claim_id"],  # '' or old token
                )
                if not won:
                    continue  # 别的 worker 抢先了

                # 实际执行 promotion（Phase 1 build + Phase 2 commit）
                try:
                    mem_payload = _build_promotion_payload(prop)
                    database.commit_promotion_atomic(
                        prop["id"], new_token, mem_payload,
                        source_ai=prop.get("proposer_ai_id", ""),
                    )
                except PromotionClaimLost:
                    pass  # 又被更快的 worker 抢了，跳过
                except Exception as e:
                    logger.exception(
                        f"recovery promotion failed for {prop['id']}: {e}"
                    )
                    database.mark_promotion_failed(
                        prop["id"], new_token, str(e)
                    )
        except Exception:
            logger.exception("proposal_recovery_loop iteration failed")
        await asyncio.sleep(interval_sec)
```

**关键点**：sweep 是"claim → build → commit"完整闭环，不是"清 claim 完事"。

### C3【H3 修】Fencing token — stale worker 醒来后写不进

**`try_claim_promotion(prop_id, new_token, expected_current_claim='')` helper**：
```python
def try_claim_promotion(prop_id, new_token, expected_current_claim='', stale_after_minutes=15):
    """原子 CAS claim：只有当前 promotion_claim_id 匹配 expected 时才切到 new_token。
    - 新 claim：expected_current_claim='' → 只在 claim 空时抢
    - stale takeover：expected_current_claim='<old_token>' → 只在 old_token 未变时接管
    - 兜底：即使 expected 对，claim_at < cutoff 也允许接管（用于 crashed original worker）
    """
    with _write_transaction() as conn:
        cutoff = _iso_minutes_ago(stale_after_minutes)
        now = _now_iso()
        cur = conn.execute(
            "UPDATE proposals SET promotion_claim_id=?, promotion_claim_at=? "
            "WHERE id=? AND status='pending' "
            "  AND (promotion_claim_id=? "
            "       OR (promotion_claim_id != '' AND promotion_claim_at < ?))",
            (new_token, now, prop_id, expected_current_claim, cutoff)
        )
        return cur.rowcount == 1
```

**Stale worker 醒来的场景**：
1. Original worker 拿 claim=T1，开始 promote，卡在 network I/O
2. 15 min 后 recovery sweep 取代 claim=T2
3. Sweep 用 T2 成功 `commit_promotion_atomic` → proposal 状态变 `auto_approved`
4. Original worker 醒来，尝试用 T1 走 `commit_promotion_atomic`
5. Fencing gate `WHERE promotion_claim_id=T1` → rowcount=0 → `PromotionClaimLost` → memory 不写

**`mark_promotion_failed(prop_id, claim_token, reason)` 也带 fencing**：
```python
def mark_promotion_failed(prop_id, claim_token, reason):
    with _write_transaction() as conn:
        cur = conn.execute(
            "UPDATE proposals SET status='promotion_failed', "
            "failure_reason=?, promotion_claim_id='', promotion_claim_at='' "
            "WHERE id=? AND status='pending' AND promotion_claim_id=?",
            (reason[:500], prop_id, claim_token)
        )
        # rowcount == 0 是 stale token，静默忽略
        return cur.rowcount == 1
```

### C4【H4 修】旧 40 条 proposal 走 report-only 人工审

**独立脚本** `scripts/audit_stuck_proposals.py`：
- `--report` (default): 输出每条 pending proposal 的分类
  - **A 类 (可以人工 approve)**: content 内容明确、无 duplicate、无 similar-active-memory
  - **B 类 (可能已经写入)**: 派生 mem_id 存在（**注意：旧 proposal 没走 deterministic 路径，几乎不会命中，但要 check**）；或按 content hash 查到 similar active memory
  - **C 类 (不清楚 / 需要人查)**: content 有歧义、多个可能匹配
- `--execute` **禁止对旧 proposal 使用**（脚本主动拒绝 `created_at < 2026-<修复上线日>`）
- 输出格式 markdown 表格，Ceci 走 `mcp review_proposal(id, approve/reject)` 手动处理

**为什么不能 auto reconcile 旧 40 条**：
- 它们不是 deterministic ID 时代产物，`mem_from_prop_{prop_id}` 派生的查询几乎必然返回 None
- 若把返回 None 当"没写"信号自动重跑 → 每条都创建**全新记忆**（新 mem_id + proposer_ai 甚至可能对现有活跃记忆造成 duplicate）
- 只能一条条人查

### C5【M1 修】Migration 幂等

`database.init_db()` 里加：
```python
prop_cols = {row[1] for row in conn.execute("PRAGMA table_info(proposals)").fetchall()}
if 'promotion_claim_id' not in prop_cols:
    conn.execute("ALTER TABLE proposals ADD COLUMN promotion_claim_id TEXT NOT NULL DEFAULT ''")
if 'promotion_claim_at' not in prop_cols:
    conn.execute("ALTER TABLE proposals ADD COLUMN promotion_claim_at TEXT NOT NULL DEFAULT ''")
```

`_PROPOSAL_COLUMNS` 加两项，`_preserve_on_empty` set 加 `{'promotion_claim_id', 'promotion_claim_at'}`（对齐 memories 侧的 finalize_claim 保护）。

### C6【M2 修】Sweep 生命周期完整

`main.py lifespan`：
```python
# startup
proposal_sweep_task = asyncio.create_task(
    proposal_sweep.proposal_recovery_loop(interval_sec=600)
)

# shutdown
proposal_sweep._shutdown.set()
try:
    await asyncio.wait_for(proposal_sweep_task, timeout=10)
except asyncio.TimeoutError:
    proposal_sweep_task.cancel()
    await asyncio.gather(proposal_sweep_task, return_exceptions=True)
logger.info(f"proposal recovery loop stopped, processed {proposal_sweep.processed_count} items")
```

对齐 `pending_sweep.py` 的生命周期模式。

### C7【M3 修】observability

- 每次 recovery iteration 结束 log 一次：`processed=N stale_takeover=M orphan_claim=K failed=J`
- `commit_promotion_atomic` 成功后 log info：`promoted proposal={prop_id} → memory={mem_id}`
- `PromotionClaimLost` log warning（不是 error — 属于正常并发路径）
- `mark_promotion_failed` log error（+ 附 reason 前 200 字符）

### C8【M4 修】Recovery 严格不 auto-approve `needs_review`

`list_promotion_candidates_for_recovery` SQL WHERE 硬约束：
```sql
WHERE status='pending'
  AND triage_reason IN ('auto_approve', 'auto_approve_silent')
```

**加一条永久防御断言**（database.py commit_promotion_atomic 内）：
```python
# Belt-and-suspenders: even if caller passes a needs_review proposal by mistake
row = conn.execute(
    "SELECT triage_reason FROM proposals WHERE id=?", (prop_id,)
).fetchone()
if row and row[0] not in ('auto_approve', 'auto_approve_silent'):
    raise ValueError(
        f"commit_promotion_atomic refuses {prop_id}: "
        f"triage_reason={row[0]!r} (not auto_approve)"
    )
```

### C9【L1 修】UX 层"对话式 Proposal 审批"独立 PR

- **不进本 PR**（Codex 建议采纳）
- 新 issue：`Phase 2.0 UX-Conv: 对话式 Proposal 审批`（等 pending 崩溃修复合并后开）
- 交互设计：AI 自然问 "我记着 X，对吗？" + Ceci 语义响应（嗯/不完全对/别记）+ 生成修订稿/拒绝/暂缓
- 依赖：本修复的 database.commit_promotion_atomic + review_proposal 已封装，UX 只加一层 chat wrapper

---

## Q4｜施工步骤 + 验收

**⚠️ 硬前置**：Step 0-A 已在 main（`da88279`）。若未合，本方案不能启动（helpers / AST 闸门 / _IN_TX_HELPERS 契约全变）。

| Step | 工作 | 验收 |
|---|---|---|
| **S0** | 存量分类扫描 `audit_stuck_proposals.py --report` | 40 条按 A/B/C 分类；Ceci 决定 A 类的手动 approve 顺序 |
| S1 | database migration + `_PROPOSAL_COLUMNS` / `_preserve_on_empty` 更新 + `PromotionClaimLost` exception | init_db 重跑幂等；旧行 `promotion_claim_*=''` 无 crash |
| S2 | `try_claim_promotion` / `list_promotion_candidates_for_recovery` / `commit_promotion_atomic` / `mark_promotion_failed` helpers | 5 单元测试：claim/takeover/candidates_list/commit atomic/mark_failed 各覆盖；needs_review 防御测试通过 |
| S3 | `_build_promotion_payload` (memory_ops) + 改造 `_create_proposal` auto_approve 分支走 claim + build + commit | 现有 22 条 proposal 测试全过；新增 12 条 promote/recovery 测试通过 |
| S4 | `proposal_sweep.py` + main.py lifespan wire | sweep 独立起停；`processed=N` log 输出；`_shutdown` 生效 |
| S5 | `audit_stuck_proposals.py` (report 模式 + 拒绝对旧数据 execute) | dry-run 输出 40 条 markdown 分类；`--execute --before <date>` 直接拒绝 |
| S6 | VPS 部署 + Ceci 人工 review 存量 A 类 | 40 条池清空；1 周内无新增卡池 |

**总估时**：3 天（v1 估的 4 天砍到 3 天——deterministic ID + fencing 让代码更简单）

---

## Q5｜风险

### 高

1. **`_build_promotion_payload` 与 `remember(quick=False)` 语义差异**  
   现有走 `remember(quick=False)` 会做很多副作用（写 client_request_id / finalize_claim_id / vec_id_map / memories_vec / write_gate 校验 / …）。v2 的 payload builder **只做纯数据映射**，让 `_set_memory_in_tx` 处理落库（含 vec 索引 + preserve 保护）。**write_gate**（如果有对 auto_approve promote 生效的路径）需要在 payload build 阶段调用（out-of-tx，无副作用只做校验）。
   **缓解**：S3 spike 30 分钟检查 write_gate 是否对 auto_approve 生效；若生效，`_build_promotion_payload` 加 `write_gate.validate(payload)` 前置。

2. **旧 40 条中的 A 类（可自动 approve）实际数量**  
   Codex 报告 15 条时未细分。40 条里可能大部分是"内容不清晰 / 已被别路径覆盖"→ C 类居多，需 Ceci 人查。
   **缓解**：S0 report 输出细分数量；若 A 类 < 5 条，Ceci 直接一条一条 approve（用 `mcp review_proposal`），完全跳过 S6 复杂流程。

3. **VPS 部署后 recovery sweep 首次运行会立刻抢占 stale**  
   40 条 pending 中若某些 `promotion_claim_at` 为空（v1 都没有 claim 列），SQL `< cutoff` 会命中 NULL → SQLite 判 NULL < X 为 UNKNOWN → 不命中。所以只有已 claim 但过期的才会被 sweep。
   ~~40 条不会被 sweep 自动碰~~。**这是正确的**（Codex H4 要求）。但要在 test 里断言此不变式。

### 中

4. **`commit_promotion_atomic` 的 `_set_memory_in_tx` 会调 vec index 维护**  
   如果 embedding 计算失败（proposal 阶段没算好），vec 走 None 分支不写映射。这是可接受降级（memory 落库了、后续 backfill_embeddings 会补）。
   **缓解**：S3 测试 payload 无 embedding 时 `commit_promotion_atomic` 仍成功。

5. **两 worker 同时 claim 同一 orphan** (两个 sweep 实例？)  
   目前只有一个 hub 进程一个 sweep loop，理论上不会。多进程部署会。
   **缓解**：`try_claim_promotion` 原子性已保证只有一个成功（`WHERE promotion_claim_id=?` + `rowcount==1`）。测试覆盖并发 claim。

### 低

6. **sweep 频率 10min 是否够快**  
   40 条卡池 28 天没救，10 min recovery 已经比现状好 4000+ 倍。若 Ceci 觉得慢可以调 300s。

---

## 与 PR1 Step 0-A 的关系

- **硬前置**：Step 0-A 已合入 main (`da88279`)。本方案完全构建在其 helpers + 契约上
- `commit_promotion_atomic` 复用 `_write_transaction()` + `_set_memory_in_tx`
- AST 闸门自动覆盖新 helpers（`_write_transaction` 匿名、literal SQL 起始 SELECT/EXPLAIN 白名单）
- 无 `_IN_TX_HELPERS` 新增（`commit_promotion_atomic` 是 public wrapper，内部走 `_set_memory_in_tx` 一处 in-tx helper）

---

## 交付流程

1. 本方案 v2 推 `phase20/proposal-pending-fix-plan-v2` 分支（doc-only）
2. Claude + Codex 都审 v2
3. 都过 → 开开工分支 `phase20/proposal-pending-fix`
4. 分批施工（S0 → S1 → S2 → ...），每批 Codex 复审（对齐 Step 0-A 节奏）
5. 全套测试通过 → 开 PR → Codex ultra 复审 → 合入
6. 合入后：
   - S6 部署 VPS
   - S0 `--report` 输出 40 条分类给 Ceci
   - Ceci 手动 approve A 类；C 类留 Ceci 空闲时查
- 1 周观察：无新增卡池 → 修复确认

## 附：Codex 转发的根因 + 5 条约束（v1 存档）

**症状**：新 `auto_approve` proposal 卡 `status=pending`，2026-07-21 `51e853e` 引入。

**根因**（4 层）：
1. auto_approve 3 步非原子（proposal INSERT → `_promote_proposal` → `update_proposal_status`）
2. `except Exception` 不覆盖 `asyncio.CancelledError`
3. `_finalize_pending_memory` 只管 memories.pending，不查 proposals
4. `maintain` 不修 proposal + `retriage_pending_proposals` 需单独 endpoint

**修复约束**（5 条必须满足）：
1. Promotion 原子抢占，两 worker 不同时晋升
2. proposal ↔ memory 稳定幂等关联，重放同 ID
3. "memory 已写、proposal 未更新"后重启，恢复同一条不重复
4. 专门 proposal recovery，不混 memory skeleton sweep（两套 wire 独立但设计对称）
5. 覆盖测试：取消 / 崩溃 / 重启 / 并发 / 状态更新失败

**应急期禁止动作**：
- ❌ 不 maintain / 不批量 retriage / 不盲目 approve
- 人工救每条前三点核查：applied_memory_id / active 记忆 / audit
