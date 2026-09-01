# Proposal Pending Bug 修复方案 v3

> 分支：`phase20/proposal-pending-fix-plan-v2` (等 v3 pass 后 open `phase20/proposal-pending-fix`)
> 硬前置：Step 0-A 已合并 main `da88279`
> 独立于 UX 层：对话式 Proposal 审批 UX 排后续 PR
>
> **版本历史**：
> - v1（作废）：`remember(existing_id=...)` 假装原子、只清 claim 不重跑、无 fencing、旧数据自动 reconcile
> - v2（作废）：拆两阶段但**只覆盖 1 个入口**、Migration 会让旧 40 条被 orphan 分支自动扫走（Critical）、`commit_promotion_atomic` 硬写 auto_approved 无法服务人工审、PromotionPayload schema 不全
> - **v3**（本文）：Codex 1C + 3H + 3M + 2L 全部收敛；5 项最小闭环全部写死

---

## v3 五项最小闭环（Codex 底线）

1. **`promotion_protocol_version` 列** — 旧行永远 `0`，SQL 层禁止 recovery 命中
2. **列全 4 个 promotion 入口** — 逐个改造，绝无遗漏
3. **统一 fenced atomic commit kernel** — 所有入口走同一 helper；terminal_state / reviewed_by 参数化
4. **完整 PromotionPayload schema** — 逐字段列出 + 与现有 create 路径 golden 对比
5. **4 组关键反例** — legacy exclusion / maintenance 崩溃 / 人工并发 / audit rollback

---

## Q1｜要解决什么问题

- **存量**：40 条 auto_approve pending 卡池（原报 15 → 2 周涨到 40；oldest `2026-07-24`）
- **结构**：async 中断 / 崩溃 / 重启后自动恢复；两 worker 不重复晋升；同一 proposal 重放不多创建；`needs_review` 严格排除；**旧数据永不自动 reconcile**（新老 ID 派生不同 → 会创建 duplicate）

## Q2｜现状（4 个 promotion 入口全列）

| 入口 | 位置 | 现状 | v3 处理 |
|---|---|---|---|
| **A. 普通 auto** | `memory_ops.py:1301-1327` (`_create_proposal` auto_approve 分支) | 3 步非原子 | 改造：claim → build payload → `commit_promotion_atomic` |
| **B. maintenance auto** | `memory_ops.py:1063-1078` (`_execute_maintenance_action` update/supersede 分支) | **旧 memory 先 supersede + 独立 tx 再 promote**（比 A 更糟：崩溃后旧 memory 消失、新 memory 未创建）| **重构为单事务**：drift check + supersede + set_memory + finalize proposal + audit 全在一个 tx |
| **C. 人工 approve** | `memory_ops.py:1194-1222` (`review_proposal(action="approve")`) | 走 `_promote_proposal` (老)，崩溃后重复 promotion 风险 | 走同一 fenced kernel，terminal_state=`approved` + `reviewed_by=<真实调用者>` |
| **D. bulk retriage** | `memory_ops.py:1225-1262` + `main.py:776-780` endpoint | 遍历 pending 直接调 `_promote_proposal` 写库；**可能批量处理旧 40 条**绕过 protocol version | **暂停自动写入**：改为 report-only 生成审核清单；若保留写库，必须走 claim + protocol gate + atomic commit |

### 现有 `_promote_proposal` 走完整 `remember(quick=False)` 隐患
- `remember(quick=False)` 内部 dedup/relation 分类，返回的 mem_id 可能 ≠ deterministic ID
- Recovery 用 deterministic ID 查不到 → 判"没写" → 重复创建 duplicate

### 已确认可复用契约（Step 0-A 后）
- `_write_transaction()` / `_set_memory_in_tx(conn, mem)` / `_IN_TX_HELPERS`
- `_get_read_conn()` DB_PATH 失效
- AST 闸门 fail-closed

---

## Q3｜设计（按 Codex 1C + 3H + 3M + 2L 逐条对应）

### C1【Critical 修】`promotion_protocol_version` 列 — 旧 40 条 SQL 层永久排除

**Migration** (`database.init_db()` 内幂等)：
```python
prop_cols = {row[1] for row in conn.execute("PRAGMA table_info(proposals)").fetchall()}
if 'promotion_claim_id' not in prop_cols:
    conn.execute("ALTER TABLE proposals ADD COLUMN promotion_claim_id TEXT NOT NULL DEFAULT ''")
if 'promotion_claim_at' not in prop_cols:
    conn.execute("ALTER TABLE proposals ADD COLUMN promotion_claim_at TEXT NOT NULL DEFAULT ''")
if 'promotion_protocol_version' not in prop_cols:
    # 关键：DEFAULT 0 → 存量所有 pending 都是 v0（永不入 recovery 视野）
    conn.execute(
        "ALTER TABLE proposals ADD COLUMN promotion_protocol_version INTEGER NOT NULL DEFAULT 0"
    )
```

**新流程 `_create_proposal` 强制写 `= 2`**：
- `insert_proposal(proposal)` 前，`proposal['promotion_protocol_version'] = 2` 硬编码
- `_PROPOSAL_COLUMNS` 加入 `promotion_protocol_version` / `promotion_claim_id` / `promotion_claim_at`

**Recovery SQL 强制 `= 2`**：
```sql
SELECT * FROM proposals
WHERE status='pending'
  AND promotion_protocol_version = 2                   -- **legacy exclusion**
  AND triage_reason IN ('auto_approve', 'auto_approve_silent')
  AND (
    (promotion_claim_id != '' AND promotion_claim_at < ?)     -- stale takeover
    OR (promotion_claim_id = '' AND created_at < ?)           -- orphan
  )
ORDER BY created_at ASC
LIMIT ?
```

**为什么用 protocol_version 而不是 date cutoff**：
- date cutoff 有 race — 部署上线瞬间之前几秒创建的新 proposal 会被误判 legacy
- protocol_version 是**显式契约**，新代码创建的 proposal 必然写 2；旧数据必然是 DEFAULT 0

**反例测试**（4 组之一）：
- `test_v3_c1_legacy_orphan_never_recovered` — 造一条 `status='pending' + triage_reason='auto_approve' + promotion_claim_id='' + promotion_protocol_version=0 + created_at=30 天前`；跑 recovery sweep；断言：**candidates 空、memory 未创建、proposal 状态未变**

### C2【H1 修】重构 maintenance auto 入口（Entry B）— 全在一个 tx

**现状**：
```python
# _execute_maintenance_action(update/supersede)
result = await database.commit_maintenance_atomic(...)  # ← tx 1: 旧 memory → superseded
# ↓ ↓ ↓ 崩溃点：旧 memory 已 superseded，新 memory 未创建
if result and result.get('superseded_id'):
    proposal['status'] = 'pending'
    database.insert_proposal(proposal)                   # ← tx 2
    result = await _promote_proposal(proposal)           # ← tx 3+ (走 remember)
    database.update_proposal_status(prop_id, 'auto_approved', ...)  # ← tx 4
```

**v3 改造**：抽 `commit_maintenance_promotion_atomic(prop_id, claim_token, mem_payload, target_id, expected_target_status, expected_target_updated_at, audit_row, terminal_state, reviewed_by)`：
```python
def commit_maintenance_promotion_atomic(
    prop_id, claim_token, mem_payload,
    target_id, expected_target_status, expected_target_updated_at,
    audit_row, terminal_state, reviewed_by,
):
    """全部在一个 _write_transaction() 内：
      1. Fencing gate: proposal 状态复查 + claim_token 复查
      2. drift gate: 旧 target memory status/updated_at 未变
      3. supersede old memory (via _set_memory_in_tx with status='superseded' + superseded_by)
      4. insert new memory (via _set_memory_in_tx with deterministic id + status='active')
      5. finalize proposal (status → auto_approved/approved, applied_memory_id, claim 释放)
      6. audit row insert
    任何一步失败全回滚。"""
```

**maintenance 入口调用**：
```python
# Entry B 新版
if action in ("update", "supersede"):
    # 1. claim proposal
    token = uuid.uuid4().hex
    proposal["promotion_protocol_version"] = 2
    database.insert_proposal(proposal)
    if not database.try_claim_promotion(prop_id, token, expected_current_claim=""):
        return {"error": "claim_lost_at_entry"}
    # 2. build payload (out-of-tx, no side effect)
    mem_payload = _build_promotion_payload(proposal, target_mem)
    # 3. atomic
    try:
        database.commit_maintenance_promotion_atomic(
            prop_id, token, mem_payload,
            target_id=target_id,
            expected_target_status=target_mem["status"],
            expected_target_updated_at=target_mem["updated_at"],
            audit_row=..., terminal_state="auto_approved",
            reviewed_by="system",
        )
    except PromotionClaimLost:
        return {...}
    except MaintenanceDrift:
        database.mark_promotion_failed(prop_id, token, "drift on target")
        return {...}
```

**关键**：drift 触发时**新 memory 不 insert**，旧 memory **不 supersede**，proposal 保持 pending（下轮 recovery 会重试或 fail）。

### C3【H2 修】4 个入口共用 fenced kernel

**统一 kernel**（database.py 唯一入口）：
```python
def commit_promotion_atomic(
    prop_id: str,
    claim_token: str,
    mem_payload: dict,
    audit_row: dict,
    terminal_state: str,               # 'auto_approved' / 'approved' / 'retriaged_approved'
    reviewed_by: str,                  # 'system' / '<user_id>' / 'retriage'
) -> dict:
    """Unified fenced kernel for ALL promotion entrypoints:
      - Entry A (normal auto):        terminal_state='auto_approved', reviewed_by='system'
      - Entry B (maintenance auto):   use commit_maintenance_promotion_atomic instead
      - Entry C (manual approve):     terminal_state='approved', reviewed_by=<caller>
      - Entry D (retriage):           v3 doesn't call this — retriage is report-only
    """
    if terminal_state not in ('auto_approved', 'approved', 'retriaged_approved'):
        raise ValueError(f"invalid terminal_state: {terminal_state!r}")
    with _write_transaction() as conn:
        now = _now_iso()
        # 1. legacy exclusion: 二次防御，即使 recovery SQL 忘 filter 也不放行
        row = conn.execute(
            "SELECT triage_reason, promotion_protocol_version "
            "FROM proposals WHERE id=?", (prop_id,)
        ).fetchone()
        if not row:
            raise PromotionClaimLost(f"proposal {prop_id} not found")
        if row[1] != 2:
            raise ValueError(
                f"refuse to promote legacy proposal (protocol_version={row[1]})"
            )
        # 2. needs_review 防御（对 auto_approved 分支硬约束）
        if terminal_state == 'auto_approved' and row[0] not in ('auto_approve', 'auto_approve_silent'):
            raise ValueError(
                f"auto_approved requires triage_reason in {{auto_approve, auto_approve_silent}}, "
                f"got {row[0]!r}"
            )
        # 3. fencing gate
        cur = conn.execute(
            "UPDATE proposals SET status=?, applied_memory_id=?, reviewed_at=?, "
            "reviewed_by=?, promotion_claim_id='', promotion_claim_at='' "
            "WHERE id=? AND status='pending' AND promotion_claim_id=?",
            (terminal_state, mem_payload['id'], now, reviewed_by, prop_id, claim_token)
        )
        if cur.rowcount != 1:
            raise PromotionClaimLost(
                f"proposal {prop_id} claim {claim_token!r} no longer valid"
            )
        # 4. memory (via _set_memory_in_tx — vec index + preserve 保护齐全)
        _set_memory_in_tx(conn, mem_payload)
        # 5. audit
        conn.execute(
            "INSERT INTO maintenance_audit "
            "(action, target_id, new_content, source_message_ids, "
            " decision_reason, state_before, state_after, model_id, "
            " source_ai, auto_executed, prompt_version, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                audit_row.get("action", "promotion_committed"),
                mem_payload['id'],
                mem_payload.get("content", ""),
                audit_row.get("source_message_ids", "[]"),
                audit_row.get("decision_reason", ""),
                audit_row.get("state_before", "{}"),
                audit_row.get("state_after", "{}"),
                audit_row.get("model_id", ""),
                audit_row.get("source_ai", ""),
                1 if terminal_state != 'approved' else 0,
                audit_row.get("prompt_version", ""),
                now,
            )
        )
    return {"memory_id": mem_payload['id'], "proposal_id": prop_id,
            "terminal_state": terminal_state, "reviewed_by": reviewed_by}
```

**各入口对应**：

| 入口 | terminal_state | reviewed_by | audit action |
|---|---|---|---|
| A normal auto | `auto_approved` | `"system"` | `promotion_committed` |
| B maintenance auto | `auto_approved` | `"system"` | `promotion_committed_with_supersede`（走 `commit_maintenance_promotion_atomic`）|
| C manual approve | `approved` | 参数传入 | `manual_approve` |
| D retriage | **不走 kernel** — 只生成 `retriage_report.md`；不自动写 | — | — |

**retriage endpoint** (`main.py:776`)：**保留 endpoint 但改行为为 report-only**（返回 JSON 描述"多少条会通过 retriage"，不实际写库）；等 v3 稳定后再评估是否恢复自动写入（届时必走 kernel）。

**旧 `_promote_proposal` 函数**：改为 wrapper，仅供 backward-compat 的测试用（生产 4 个入口全部迁走）；加 `warnings.warn(DeprecationWarning)`。

**AST/callsite 闸门**（新增 test）：生产代码非 tests/ 目录不允许直接调 `_promote_proposal`（AST 扫函数调用）。

### C4【H3 修】完整 `PromotionPayload` schema

**`_build_promotion_payload(proposal: dict, target_mem: dict | None = None) -> dict`**：
- **纯数据变换**，不查 DB，不写 DB，不走 dedup（Codex 强调）
- 显式列出每个 `_ALL_COLUMNS` 字段的值来源
- AI alias canonicalization（`owner_ai` / `source_ai` 走 `AI_ALIASES.get(x, x)`）
- 与现有 create 路径 `remember(quick=True)` 生成的字段做 **golden 对比**（S3 单元测试）

**Schema 逐字段表**（对齐 `_ALL_COLUMNS`）：
```python
def _build_promotion_payload(proposal: dict, target_mem: dict | None = None) -> dict:
    """把 proposal 转成 memory dict，可直接喂 _set_memory_in_tx。
    无副作用；无 embedding 时留 None 由 _set_memory_in_tx.COALESCE 保留旧值或空。
    """
    from config import AI_ALIASES
    now = _now_iso()
    prop_id = proposal["id"]
    mem_id = f"mem_from_prop_{prop_id}"
    canonical_owner = AI_ALIASES.get(proposal.get("owner_ai", ""), proposal.get("owner_ai", ""))
    canonical_source = AI_ALIASES.get(proposal.get("proposer_ai_id", ""), proposal.get("proposer_ai_id", ""))

    # tags / linked_memories / supersedes / history / comments 归一为 canonical JSON string
    tags = proposal.get("tags") or "[]"
    if isinstance(tags, (list, dict)):
        tags = json.dumps(tags, ensure_ascii=False)

    supersedes = json.dumps([target_mem["id"]]) if (target_mem and target_mem.get("id")) else "[]"

    return {
        # identity
        "id": mem_id,
        "content": proposal["content"],
        # taxonomy
        "layer": proposal.get("layer", "shared"),
        "room": proposal.get("proposed_room", "living_room"),
        "category": proposal.get("category", ""),
        "owner_ai": canonical_owner,
        # scoring
        "importance": float(proposal.get("importance", 0.5)),
        "emotion_arousal": float(proposal.get("emotion_arousal", 0.3)),
        "valence": 0.5,           # neutral default — proposal 目前不带 valence
        "domain": "",             # analyzer 生成，proposal 阶段无
        "decay_score": 0.5,       # 初始
        "activation_count": 0,    # 初始
        "last_activated": "",     # 未激活
        # provenance
        "source_ai": canonical_source,
        "source_platform": proposal.get("source_platform", ""),
        "tags": tags,
        "linked_memories": "[]",
        "supersedes": supersedes,
        "superseded_by": "",
        "event_date": proposal.get("event_date", ""),
        "source_context": proposal.get("source_context", ""),
        "comments": "[]",
        # vec — embedding at proposal time 可能已缓存；无则 None 由 COALESCE
        "embedding": proposal.get("embedding"),
        # state
        "status": "active",
        "created_at": now,
        "updated_at": now,
        "history": json.dumps([{
            "at": now,
            "action": "promoted_from_proposal",
            "proposal_id": prop_id,
        }], ensure_ascii=False),
        "resolved": None,
        "anchored": None,
        "provenance_type": proposal.get("provenance_type", ""),
        "fact_confidence": proposal.get("confidence"),  # REAL or None
        "subject_id": proposal.get("subject_id", ""),
        "source_actor_id": proposal.get("source_actor_id", ""),
        "info_type": proposal.get("info_type", "fact"),
        # PR C skeleton fields — promotion 不参与 async skeleton，留空
        "client_request_id": "",
        "link_to_real_id": "",
        "finalize_claim_id": "",
        "finalize_claim_at": "",
    }
```

**Golden test**（S3）：
- `test_v3_payload_covers_all_ALL_COLUMNS` — 断言 `_build_promotion_payload(fake_prop)` 返回的 dict key 集合覆盖 `database._ALL_COLUMNS`（不多不少）
- `test_v3_payload_matches_normal_create_field_by_field` — 造一个走 `remember(quick=True, allow_direct_write=True)` 的对照组 memory + 一个走本 payload 的 memory，逐字段 assert（除 `id`、`created_at`、`updated_at` 允许不同）
- `test_v3_payload_ai_alias_canonicalized` — `proposer_ai_id='cloudy'` → payload `source_ai='claude'`（若配置里 alias 生效）
- `test_v3_payload_no_embedding_ok` — `proposal` 无 `embedding` key → payload `embedding is None` → `_set_memory_in_tx` 走 COALESCE 无 crash

**write_gate 复用**：write_gate 只在 `remember(quick=True)` 阶段（proposal 创建之前）跑 —— 已经验证过的 proposal 到 promotion 阶段不需要再跑。文档明确写死此复用假设，施工时不再 spike。

### C5【H4 修】人工 approve 与 auto 并发防护

`review_proposal(action='approve')` 改造：
```python
async def review_proposal(proposal_id, action, reviewed_by="user", reject_reason=""):
    proposal = database.get_proposal(proposal_id)
    if not proposal:
        return {"error": "not found"}
    if proposal["status"] != "pending":
        return {"error": f"already {proposal['status']}"}

    if action == "approve":
        # legacy exclusion
        if proposal.get("promotion_protocol_version", 0) != 2:
            return {"error": "legacy proposal — please use audit_stuck_proposals script"}

        # 人工 claim（可强制取代任何 stale auto claim）
        token = uuid.uuid4().hex
        won = database.try_claim_promotion(
            proposal_id, token,
            expected_current_claim="",  # 优先抢空 claim
            stale_after_minutes=0,      # human intent 有更高优先级：允许接管任何 auto claim
        )
        if not won:
            # 真的有一个 auto worker 正在执行（fresh claim < 0min）— 罕见
            return {"error": "concurrent auto promotion in flight, please retry"}

        try:
            mem_payload = _build_promotion_payload(proposal)
            database.commit_promotion_atomic(
                proposal_id, token, mem_payload,
                audit_row={
                    "action": "manual_approve",
                    "source_message_ids": proposal.get("source_message_ids", "[]"),
                    "decision_reason": f"manually approved by {reviewed_by}",
                    "state_before": json.dumps({
                        "proposal_status": "pending",
                        "triage_reason": proposal.get("triage_reason", ""),
                    }),
                    "state_after": json.dumps({
                        "memory_id": mem_payload['id'],
                        "proposal_status": "approved",
                    }),
                    "source_ai": proposal.get("proposer_ai_id", ""),
                },
                terminal_state="approved",
                reviewed_by=reviewed_by,
            )
            return {"proposal_id": proposal_id, "status": "approved",
                    "memory_id": mem_payload['id']}
        except PromotionClaimLost:
            return {"error": "claim lost during commit"}
        except Exception as e:
            database.mark_promotion_failed(proposal_id, token, str(e))
            return {"proposal_id": proposal_id, "status": "promotion_failed", "error": str(e)}

    elif action == "reject":
        # 不 claim（reject 不写 memory）；但要检查 protocol_version = 2 才能标 rejected
        # v0 也允许 reject（人工判断"不要"）— reject 无副作用
        database.update_proposal_status(proposal_id, "rejected", reviewed_by, reject_reason)
        return {"proposal_id": proposal_id, "status": "rejected", "reason": reject_reason}
```

**反例测试**（4 组之一）：
- `test_v3_c5_manual_and_auto_race_only_one_wins` — auto worker 拿 fresh claim，人工同时按 approve，人工 `try_claim_promotion` 遵循 `stale_after_minutes=0` 语义（允许接管）→ 断言恰好 1 条 memory 且 audit 恰好 1 条

### C6【H2 修 - retriage 分离】retriage 只 report

**新 `retriage_pending_proposals_report_only()` (memory_ops.py)** 替换现有函数：
```python
async def retriage_pending_proposals_report_only() -> dict:
    """v3: 不写库！只返回"如果按当前 rule 重跑 triage，多少条会 auto_approve"的报告。
    生产 endpoint (main.py:776) 仍暴露此函数，返回 JSON 供 Ceci 审查。
    真要执行 → 走 audit_stuck_proposals.py + Ceci 手动 review_proposal。"""
    all_pending = database.list_proposals(status="pending", limit=500, offset=0)
    would_approve, would_still_pending = [], []
    for prop in all_pending:
        # ... 重跑 triage 逻辑 ...
        if decision in ("auto_approve", "auto_approve_silent"):
            would_approve.append({
                "id": prop["id"], "content_preview": prop["content"][:80],
                "protocol_version": prop.get("promotion_protocol_version", 0),
                "triage_reason": decision,
            })
        else:
            would_still_pending.append({"id": prop["id"], "reason": decision})
    return {
        "note": "REPORT-ONLY — no writes performed",
        "would_approve": would_approve,
        "would_still_pending": would_still_pending,
        "total": len(all_pending),
    }
```

`main.py:776` endpoint 保持不变，只是内部函数换成 report-only 版本。

### C7【M1 修】删除 proposal `_preserve_on_empty` 描述

v2 说加 `_preserve_on_empty` — 这是 memories 表的 UPSERT 概念，proposals 表没有 UPSERT 需求。**删除**。

`promotion_claim_id / promotion_claim_at / promotion_protocol_version` 只通过专用 CAS helpers 修改（`try_claim_promotion` / `commit_promotion_atomic` / `mark_promotion_failed`），从不通过通用 `update_proposal_*` 更新。

### C8【M2 修】sweep 完整生命周期 + batch limit + shutdown event 契约

```python
# proposal_sweep.py
import asyncio, uuid, logging

_shutdown = asyncio.Event()
processed_count = 0

async def proposal_recovery_loop(interval_sec: int = 600, batch_size: int = 20):
    """
    每轮最多处理 batch_size 条，稳定 ORDER BY created_at ASC。
    _shutdown 触发时立刻退出 sleep（不等 interval）。
    """
    global processed_count
    while not _shutdown.is_set():
        try:
            summary = await _process_one_batch(batch_size)
            processed_count += summary["processed"]
            if summary["processed"] > 0:
                logger.info(
                    "proposal recovery: processed=%d stale_takeover=%d "
                    "orphan_claim=%d failed=%d",
                    summary["processed"], summary["stale"],
                    summary["orphan"], summary["failed"],
                )
        except Exception:
            logger.exception("proposal_recovery_loop iteration failed")
        # v3 M2: sleep with wake-on-shutdown
        try:
            await asyncio.wait_for(_shutdown.wait(), timeout=interval_sec)
        except asyncio.TimeoutError:
            pass

def prepare_for_startup() -> None:
    """main.py lifespan startup 前调；重置 shutdown event 支持 hot reload。"""
    global _shutdown, processed_count
    _shutdown = asyncio.Event()
    processed_count = 0
```

`main.py lifespan`：
```python
# startup
proposal_sweep.prepare_for_startup()
proposal_sweep_task = asyncio.create_task(proposal_sweep.proposal_recovery_loop())

# shutdown
proposal_sweep._shutdown.set()
try:
    await asyncio.wait_for(proposal_sweep_task, timeout=10)
except asyncio.TimeoutError:
    proposal_sweep_task.cancel()
    await asyncio.gather(proposal_sweep_task, return_exceptions=True)
logger.info("proposal recovery loop stopped, processed=%d", proposal_sweep.processed_count)
```

`_process_one_batch` 内 SQL 加 `ORDER BY created_at ASC LIMIT ?`。

### C9【M3 修】audit 保留完整证据链

- `source_message_ids` **必须**从 proposal 原样带入（不能空 `[]`）
- `state_before` 至少含：`{"proposal_status": ..., "triage_reason": ..., "claim_token": ...}`
- `state_after` 至少含：`{"memory_id": ..., "proposal_status": ...}`

**新增反例测试**（4 组之一）：
- `test_v3_c9_audit_failure_rollbacks_all` — mock audit INSERT 抛 sqlite3.IntegrityError → 断言 memory 未写、proposal 未 finalize、claim 未释放

### C10【L1 修】文档 LF line endings

用 `python -c "...replace(b'\r\n', b'\n')..."` 归一 md 文档（PR 提交前一次跑）。

### C11【L2 修】S6 验收标准修正

原：**"40 条池清空"**（跟 C 类留人工审矛盾）
改：**"旧 40 条全部完成分类，A/B 已处置，C 有明确待审清单"**

---

## Q4｜施工步骤 + 验收

| Step | 工作 | 验收 |
|---|---|---|
| **S0** | `audit_stuck_proposals.py --report` 存量分类 | 40 条 A/B/C 分类；`--execute` 主动拒绝 v0 数据 |
| S1 | migration (3 列幂等) + `_PROPOSAL_COLUMNS` + `PromotionClaimLost` + `_now_iso` shared helper | init_db 重跑幂等；旧行 v=0 / 新行 v=2 |
| S2 | `try_claim_promotion` / `mark_promotion_failed` / `list_promotion_candidates_for_recovery` / `commit_promotion_atomic` / `commit_maintenance_promotion_atomic` 5 helpers | 单元测试 5 条：claim / stale / mark_failed / candidates (v=2 filter + orphan/stale) / commit atomic (含 needs_review 防御) |
| S3 | `_build_promotion_payload` + golden 对比 3 条测试（`_ALL_COLUMNS` 覆盖、字段等价、无 embedding） | payload 与 remember(quick=True) 生成的 field-by-field 一致 |
| S4 | 4 入口全部改造（A / B / C — retriage 单独 S5） | 现有 22 条 proposal 测试全过；新增 12 条 v3 测试通过 |
| S5 | `retriage_pending_proposals_report_only` + main.py endpoint 换 | endpoint 返 JSON 报告，不写库；测试断言 DB 无变化 |
| S6 | `proposal_sweep.py` + main.py lifespan wire + shutdown hot reload | sweep 独立起停；`_shutdown.wait()` 唤醒；hot reload 重置 event；批量 limit 20 |
| S7 | AST 闸门：生产代码非 tests/ 不允许直接调 `_promote_proposal` | 静态断言通过 |
| S8 | 5 组关键反例（Codex 底线）| 全通过 |
| S9 | VPS 部署 + Ceci 人工 review 存量 A 类 | 无新增 v=2 卡池；A/B 处理完；C 有清单 |

**5 组关键反例**（Codex 底线）：
1. `test_v3_c1_legacy_orphan_never_recovered` — 旧 v=0 orphan **不进入** recovery candidates
2. `test_v3_c2_maintenance_crash_all_or_nothing` — commit_maintenance_promotion_atomic 中断，旧 memory 未 supersede + 新 memory 未 insert（回滚干净）
3. `test_v3_c5_manual_and_auto_race_only_one_wins` — 人工 approve 与 auto worker 并发，恰好 1 条 memory
4. `test_v3_c9_audit_failure_rollbacks_all` — audit INSERT 失败，memory / proposal / claim 全回滚
5. `test_v3_needs_review_never_auto_promoted` — needs_review triage 硬拒 auto_approved

**总估时**：4 天（v2 是 3 → v3 是 4 —— 多的 1 天是 maintenance entry B 的完整原子 helper 重构）

---

## Q5｜风险

### 高

1. **`_build_promotion_payload` 与 `remember(quick=True)` 字段 golden 对比**
   `remember(quick=True)` 内部有多个副作用（write_gate 校验 / analyzer.analyze 生成 domain / …）。golden 对比要**降级到"字段存在性 + 类型正确性"**，而不是完全字段值等价。valence/domain 允许不同（proposal 阶段本来就没算这些）。
   **缓解**：test 里明确 skip 字段清单，document 每个 skip 的原因。

2. **maintenance entry B 崩溃后 proposal 卡 pending**
   `commit_maintenance_promotion_atomic` 若在 supersede + set_memory 后但 audit 前崩，全回滚，proposal 保持 pending。recovery 会重试 —— **但 recovery 时 target_mem 已被之前的 supersede 检查判过时**（另一 tx 可能已改）→ drift → 循环 fail。
   **缓解**：recovery 时用 `_build_promotion_payload(prop, target_mem=None)` 走"只 insert 不 supersede"降级路径；audit 记 `promotion_committed_without_supersede`；下次 Ceci 见到会警觉。

### 中

3. **`try_claim_promotion` 的 stale_after_minutes=0 语义**
   人工 approve 传 0 意味着"允许接管任何非空 claim" —— 若 auto worker 正在 in-flight 提交，会被抢占 → auto worker 的 fencing gate 会返 rowcount=0 → PromotionClaimLost → 无副作用。但人工同时会成功。**这是正确行为**（人工优先）。

4. **retriage 长期 report-only 是否够用**
   Ceci 场景：旧 40 条 report-only 已够；未来若积累新 v=2 卡池（不该发生但保险），可以从 report 手动 review。若真需要自动化，v4 再评估把 retriage 接回 kernel。

### 低

5. **`_ALL_COLUMNS` 未来加列，payload 会漏**
   `test_v3_payload_covers_all_ALL_COLUMNS` 会立刻红 → forcing 约束。

---

## 与 PR1 Step 0-A 的关系

- **硬前置**：Step 0-A 已合入 main (`da88279`)。**不可 fallback 到旧模式再 rebase**（事务边界 helper 所有权变了）
- `commit_promotion_atomic` / `commit_maintenance_promotion_atomic` 复用 `_write_transaction()` + `_set_memory_in_tx`
- AST 闸门自动覆盖新 helpers
- 无新增 `_IN_TX_HELPERS`（两个 public wrapper 都自持锁；内部只调 `_set_memory_in_tx` 一处）

---

## 交付流程

1. 本方案 v3 推分支（doc-only）
2. Claude + Codex 都审 v3
3. 都过 → 开开工分支 `phase20/proposal-pending-fix`
4. 分批施工（S0 → S9），每批 Codex 复审
5. 全套测试通过 → 开 PR → Codex ultra 复审 → 合入
6. 合入后：
   - S9 部署 VPS
   - S0 `--report` 输出 40 条分类给 Ceci
   - Ceci 手动 approve A 类（走新 fenced kernel）
- 1 周观察：无新增 v=2 卡池 + 40 条 v=0 全部完成分类 → 修复确认

---

## 附：Codex 转发的根因 + 5 条约束（v1 存档，不再重复）

根因（4 层）+ 约束（5 条）+ 应急期禁止动作详见 v1 附录（本次不重印，见 git 历史 `779a2c3`）。
