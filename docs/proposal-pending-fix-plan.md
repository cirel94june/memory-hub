# Proposal Pending Bug 修复方案 v4

> 分支：`phase20/proposal-pending-fix-plan-v2` (v4 pass 后 open `phase20/proposal-pending-fix`)
> 硬前置：Step 0-A 已合并 main `da88279`
> 对话式 Proposal 审批 UX 排后续 PR
>
> **版本历史**：
> - v1（作废）：`remember(existing_id=...)` 假装原子；只清 claim 不重跑；无 fencing
> - v2（作废）：拆两阶段但只覆盖 1 入口；旧 40 条会被 orphan 自动扫走
> - v3（作废）：protocol_version 隔离通过，但**把人工审批旧数据的路封死**；maintenance recovery 3 漏洞；reject 无 CAS；golden 路径不可行
> - **v4**（本文）：Codex 4H+3M+3L 全部收敛；6 项最小闭环写死

---

## v4 六项最小闭环（Codex 底线）

1. **人工 adopt legacy 原子路径** — `adopt_legacy_proposal_atomic()`（v0→v2 hash 校验单事务）
2. **Maintenance target snapshot 持久化** — proposal 新列 `target_snapshot_json`；drift 后**必进 promotion_failed，禁止降级 create**
3. **Reject CAS** — `reject_proposal_atomic()` `WHERE status='pending'` + rowcount==1
4. **共享 payload builder + in-tx kernel** — `_build_new_memory_payload` 正常 create 和 promotion 共用；`_commit_promotion_in_tx` 进入 `_IN_TX_HELPERS`
5. **State_before 由 kernel 事务内构造** — caller 不再伪造
6. **7 组反例** — legacy adopt / recovery legacy exclude / maintenance restart recovery / maintenance drift no-downgrade / reject vs auto race / audit rollback / needs_review 硬拒

---

## Q1｜要解决什么问题

- **存量**：40 条 auto_approve pending 卡池
- **结构**：async 中断 / 崩溃 / 重启后自动恢复；两 worker 不重复晋升；同一 proposal 重放不多创建；`needs_review` 严格排除；**旧数据 recovery 不动，但人工审可 adopt**

## Q2｜现状（4 promotion 入口）

| 入口 | 位置 | 现状 | v4 处理 |
|---|---|---|---|
| **A. 普通 auto** | `memory_ops.py:1105-1139` (`_create_proposal` auto_approve 分支) | 3 步非原子 | 走 `commit_promotion_atomic` (auto) |
| **B. maintenance auto** | `memory_ops.py:1063-1078` (`_execute_maintenance_action` update/supersede) | 旧 memory 先 supersede + 独立 tx 再 promote | **重构**：triage=`auto_approve_maintenance`；proposal 存 `target_snapshot`；走 `commit_maintenance_promotion_atomic` 单事务 |
| **C. 人工 approve v=2** | `memory_ops.py:1194-1222` (`review_proposal(action='approve')`) | 走老 `_promote_proposal` | 走 `commit_promotion_atomic` (manual) |
| **C'. 人工 adopt v=0** | 同上 | v3 直接返 legacy error 拒绝 | **新路径**：`adopt_legacy_proposal_atomic(prop_id, expected_content_hash, reviewed_by)` |
| **D. bulk retriage** | `memory_ops.py:1225-1262` + `main.py:776-780` | 遍历 pending 直接 `_promote_proposal` 写库 | **改 report-only**（不写库）|

---

## Q3｜设计（按 Codex 4H + 3M + 3L 逐条对应）

### C1【H1 修】人工 adopt legacy 安全路径

**问题**：v3 把 v=0 数据在三层全拒绝（kernel / review_proposal / audit script），S9 又要求 Ceci 人工 approve A 类 → 死循环。

**新 helper** `database.adopt_legacy_proposal_atomic(prop_id, expected_content_hash, reviewed_by, review_plan_id="")`：
```python
def adopt_legacy_proposal_atomic(
    prop_id: str,
    expected_content_hash: str,       # sha256(proposal.content) at report time
    reviewed_by: str,                  # 必填，人工审用户名
    review_plan_id: str = "",          # 可选：audit script 生成的 report ID
) -> dict:
    """人工审批专用路径。单事务内：
      1. 校验 status='pending' AND promotion_protocol_version = 0
      2. 校验 content hash 未漂移（防 report 生成到 approve 之间 proposal 被改）
      3. v0 → v2（升级 protocol）
      4. 人工 claim（一步到位 fencing）
      5. 走同一 _commit_promotion_in_tx（同 in-tx kernel）
    Recovery/retriage 严格禁止调用此函数（AST 闸门 + runtime assertion）。
    """
    caller_frame = inspect.stack()[1].function
    if caller_frame in {"proposal_recovery_loop", "_process_one_batch",
                        "retriage_pending_proposals_report_only",
                        "commit_promotion_atomic"}:
        raise RuntimeError(
            f"adopt_legacy_proposal_atomic must not be called from {caller_frame!r}"
        )
    if not reviewed_by or not reviewed_by.strip():
        raise ValueError("reviewed_by required for legacy adoption")
    now = _now_iso()
    with _write_transaction() as conn:
        row = conn.execute(
            "SELECT content, status, promotion_protocol_version "
            "FROM proposals WHERE id=?", (prop_id,)
        ).fetchone()
        if not row:
            raise ValueError(f"proposal {prop_id} not found")
        if row[1] != 'pending':
            raise ValueError(f"proposal {prop_id} status={row[1]!r}, not pending")
        if row[2] != 0:
            raise ValueError(
                f"proposal {prop_id} already v={row[2]}, use review_proposal instead"
            )
        current_hash = hashlib.sha256(row[0].encode('utf-8')).hexdigest()
        if current_hash != expected_content_hash:
            raise LegacyContentDrift(
                f"proposal {prop_id} content changed since report "
                f"(expected {expected_content_hash[:12]}..., got {current_hash[:12]}...)"
            )
        # 升级 protocol + 分配人工 claim（一步 CAS）
        token = uuid.uuid4().hex
        cur = conn.execute(
            "UPDATE proposals SET promotion_protocol_version=2, "
            "promotion_claim_id=?, promotion_claim_at=? "
            "WHERE id=? AND status='pending' AND promotion_protocol_version=0",
            (token, now, prop_id)
        )
        if cur.rowcount != 1:
            raise PromotionClaimLost(f"legacy adoption race on {prop_id}")
        # 重新读一致的 proposal + 走共享 in-tx kernel
        proposal = _row_to_proposal_dict(
            conn.execute("SELECT * FROM proposals WHERE id=?", (prop_id,)).fetchone()
        )
        mem_payload = _promotion_payload_from_proposal(
            proposal, origin='legacy_adoption',
        )
        return _commit_promotion_in_tx(
            conn=conn, prop_id=prop_id, claim_token=token,
            mem_payload=mem_payload, terminal_state='approved',
            reviewed_by=reviewed_by,
            audit_extra={
                "action": "legacy_adopt",
                "review_plan_id": review_plan_id,
            },
        )
```

**新异常** `LegacyContentDrift` — 人工看到后应重新出 audit report 再决定。

**caller frame 断言** 是 belt-and-suspenders；主约束是 AST 闸门：
- `test_v4_ast_gate_adopt_only_from_review_proposal` — AST 扫全生产代码，`adopt_legacy_proposal_atomic` 的调用者必须在 `memory_ops.review_proposal` 或未来的 `mcp_server.review_proposal_tool` 内

**`review_proposal(action='approve')` 分流**：
```python
if action == "approve":
    if proposal.get("promotion_protocol_version", 0) == 0:
        return {"error": "legacy_needs_adopt",
                "hint": "call review_proposal(action='adopt_legacy', expected_content_hash=...)"}
    # v=2 走 commit_promotion_atomic（既有 manual 分支）
elif action == "adopt_legacy":  # v4 新增 action
    return await _adopt_legacy_via_review(
        proposal_id, expected_content_hash, reviewed_by,
    )
elif action == "reject":
    return database.reject_proposal_atomic(proposal_id, reviewed_by, reject_reason)
```

**反例测试**（7 组之一）:
- `test_v4_h1_adopt_legacy_hash_drift_rejected` — content 被改后 adopt 失败，DB 无变化
- `test_v4_h1_adopt_legacy_from_recovery_raises` — 从 `proposal_recovery_loop` 调 → RuntimeError

### C2【H2 修】Maintenance recovery 三漏洞

**漏洞 1：Entry B 没 triage_reason='auto_approve'** → recovery SQL 捡不到
- v4 引入新 triage 值 `auto_approve_maintenance`
- Recovery SQL 白名单：`triage_reason IN ('auto_approve', 'auto_approve_silent', 'auto_approve_maintenance')`
- `_commit_promotion_in_tx` 的 needs_review 防御白名单同步

**漏洞 2：target snapshot 未持久化，重启后无法重建 drift gate**
- Proposals 表新增列（migration 幂等）：`target_snapshot_json TEXT NOT NULL DEFAULT ''`
- Entry B 创建 proposal 时写入：
```json
{
  "target_id": "mem_xxx",
  "expected_status": "active",
  "expected_updated_at": "2026-08-27T10:00:00+00:00",
  "relation": "supersede",
  "reason": "contradicts existing"
}
```
- Recovery 拿到 v=2 + auto_approve_maintenance proposal → 从 `target_snapshot_json` 重建 drift gate

**漏洞 3：drift 时降级 create 会静默改决策 → 2 条 active 冲突**
- v4 硬约束：drift → `mark_promotion_failed(reason='target_drifted')`，proposal 状态变 `promotion_failed`
- **绝不降级为 insert-only**
- Ceci/AI 后续可以看到 `promotion_failed` proposal，重新出决策

**修正**：v3 说"drift 保 pending，下轮 recovery 重试"矛盾于 `mark_promotion_failed` 的语义 —— v4 明确 drift = `promotion_failed` 终态，需要人工重新出 proposal。

**`commit_maintenance_promotion_atomic` v4**：
```python
def commit_maintenance_promotion_atomic(
    prop_id, claim_token, mem_payload,
    target_snapshot,     # {'target_id', 'expected_status', 'expected_updated_at', 'relation'}
    reviewed_by,
) -> dict:
    """单事务：drift 校验 + supersede + insert new + finalize proposal + audit。
    drift 触发 → MaintenanceDrift 抛出（caller mark_promotion_failed）。绝不降级。"""
    with _write_transaction() as conn:
        # 1. drift gate
        tgt = target_snapshot
        cur_row = conn.execute(
            "SELECT status, updated_at FROM memories WHERE id=?", (tgt['target_id'],)
        ).fetchone()
        if not cur_row:
            raise MaintenanceDrift(f"target {tgt['target_id']} not found")
        if cur_row[0] != tgt['expected_status'] or cur_row[1] != tgt['expected_updated_at']:
            raise MaintenanceDrift(
                f"target {tgt['target_id']} drifted: expected "
                f"({tgt['expected_status']}, {tgt['expected_updated_at']}), "
                f"got ({cur_row[0]}, {cur_row[1]})"
            )
        # 2. supersede old（in-tx，UPSERT via _set_memory_in_tx 保留 preserve）
        old_mem = _fetch_full_mem(conn, tgt['target_id'])
        old_mem['status'] = 'superseded'
        old_mem['superseded_by'] = mem_payload['id']
        old_mem['updated_at'] = _now_iso()
        _set_memory_in_tx(conn, old_mem)
        # 3. insert new + finalize proposal + audit —— 共享 in-tx kernel
        return _commit_promotion_in_tx(
            conn=conn, prop_id=prop_id, claim_token=claim_token,
            mem_payload=mem_payload, terminal_state='auto_approved',
            reviewed_by=reviewed_by,
            audit_extra={
                "action": "promotion_with_supersede",
                "superseded_target": tgt['target_id'],
                "relation": tgt.get('relation', ''),
            },
        )
```

**反例测试**（7 组之一）:
- `test_v4_h2_maintenance_restart_recovery_from_snapshot` — 造一条 v=2+auto_approve_maintenance proposal 带 snapshot；重启进程；recovery 拿到 → 走完整 supersede+insert
- `test_v4_h2_maintenance_drift_marks_failed_no_downgrade` — target 在 recovery 前被改；断言 proposal 变 promotion_failed，新 memory **未** insert，旧 memory **未** supersede

### C3【H3 修】Reject CAS

**新 helper** `database.reject_proposal_atomic(prop_id, reviewed_by, reject_reason)`：
```python
def reject_proposal_atomic(prop_id: str, reviewed_by: str, reject_reason: str) -> dict:
    """条件 UPDATE：只在 status='pending' 且 claim 未持有时 reject。
    若已 auto_approved（auto worker 抢先了）→ 返回 already_finalized 不覆盖。
    若被 auto worker 持有 claim → 返回 in_flight（人工可稍后再试）。
    """
    now = _now_iso()
    with _write_transaction() as conn:
        # 先看当前状态（read）
        row = conn.execute(
            "SELECT status, promotion_claim_id, applied_memory_id "
            "FROM proposals WHERE id=?", (prop_id,)
        ).fetchone()
        if not row:
            return {"error": "not_found"}
        if row[0] != 'pending':
            return {"status": row[0], "note": "already_finalized",
                    "applied_memory_id": row[2] or ""}
        if row[1]:  # claim held by auto worker
            return {"status": "in_flight", "note": "auto worker holds claim, retry later"}
        # CAS reject
        cur = conn.execute(
            "UPDATE proposals SET status='rejected', reviewed_by=?, "
            "reviewed_at=?, reject_reason=?, "
            "promotion_claim_id='', promotion_claim_at='' "
            "WHERE id=? AND status='pending' AND promotion_claim_id=''",
            (reviewed_by, now, reject_reason, prop_id)
        )
        if cur.rowcount != 1:
            return {"status": "in_flight", "note": "claim taken between read and update"}
        return {"status": "rejected", "reviewed_by": reviewed_by}
```

**`review_proposal(action='reject')` 走此新 helper**（memory_ops 侧只 wrap）。

**反例测试**（7 组之一）:
- `test_v4_h3_reject_vs_auto_race` — 双线程：thread_A auto worker 拿 claim + 提交；thread_B 人工 reject。断言最终恰好一种终态（`auto_approved+1 memory` 或 `rejected+0 memory`），不出现 `rejected+1 memory` 矛盾

### C4【H4 修】共享 payload builder + in-tx kernel

**问题**：v3 的 `_build_promotion_payload` 生成字段跟正常 create 路径不等价（decay_score / domain / history schema），golden 测试无对照标准。

**v4 新架构**：抽取共享**纯函数** `memory_ops._build_new_memory_payload`（不查 DB、不写 DB、不 mutate 输入）：

```python
def _build_new_memory_payload(
    *,
    content: str,
    layer: str = "shared",
    room: str = "living_room",
    category: str = "",
    owner_ai: str = "",
    source_ai: str = "",
    source_platform: str = "",
    importance: float = 0.5,
    emotion_arousal: float = 0.3,
    valence: float = 0.5,
    domain: str = "[]",
    tags: list[str] | str = None,
    subject_id: str = "",
    source_actor_id: str = "",
    info_type: str = "fact",
    event_date: str = "",
    source_context: str = "",
    provenance_type: str = "",
    fact_confidence: float | None = None,
    embedding: bytes | None = None,
    # promotion-specific:
    override_id: str | None = None,           # deterministic mem_from_prop_{id}
    override_created_at: str | None = None,   # 保留 proposal 时间
    origin: str = "normal_create",            # 'normal_create' / 'promotion' / 'legacy_adoption'
    supersedes: list[str] | None = None,      # maintenance path 才有
    proposal_id: str = "",                    # audit trail
) -> dict:
    """纯函数：产出可直接喂 _set_memory_in_tx 的 memory dict。
    正常 create 和所有 promotion 入口都调此。ID/time/embedding/supersedes 参数化。"""
    from config import AI_ALIASES
    now = override_created_at or _now_iso()
    mem_id = override_id or _gen_id()
    tags_json = json.dumps(tags or [], ensure_ascii=False) if not isinstance(tags, str) else tags
    return {
        # identity
        "id": mem_id, "content": content,
        # taxonomy
        "layer": layer, "room": room, "category": category,
        "owner_ai": AI_ALIASES.get(owner_ai, owner_ai),
        # scoring — 与现有 memory_ops.remember normal create 保持一致
        "importance": float(importance), "emotion_arousal": float(emotion_arousal),
        "valence": float(valence), "domain": domain,
        "decay_score": 1.0,           # ← 与正常 create 一致（v3 写 0.5 是错的）
        "activation_count": 0, "last_activated": "",
        # provenance
        "source_ai": AI_ALIASES.get(source_ai, source_ai),
        "source_platform": source_platform,
        "tags": tags_json, "linked_memories": "[]",
        "supersedes": json.dumps(supersedes or [], ensure_ascii=False),
        "superseded_by": "",
        "event_date": event_date, "source_context": source_context,
        "comments": "[]", "embedding": embedding,
        # state
        "status": "active", "created_at": now, "updated_at": now,
        # history — 与正常 create schema 对齐 {v, content, date, by}
        "history": json.dumps([{
            "v": 1, "content": content, "date": now,
            "by": AI_ALIASES.get(source_ai, source_ai) or "system",
            "origin": origin,
            **({"proposal_id": proposal_id} if proposal_id else {}),
        }], ensure_ascii=False),
        "resolved": None, "anchored": None,
        "provenance_type": provenance_type, "fact_confidence": fact_confidence,
        "subject_id": subject_id, "source_actor_id": source_actor_id,
        "info_type": info_type,
        # PR C skeleton fields — promotion 不参与
        "client_request_id": "", "link_to_real_id": "",
        "finalize_claim_id": "", "finalize_claim_at": "",
    }
```

**正常 create 路径迁移**：`memory_ops.remember` 内所有构造 memory dict 的地方改调 `_build_new_memory_payload(...)`。这是 v4 施工内的最大改动 —— 消除"两份近似字典"。

**Promotion payload wrapper**：
```python
def _promotion_payload_from_proposal(
    proposal: dict, *, origin: str = "promotion", supersedes: list[str] | None = None,
) -> dict:
    return _build_new_memory_payload(
        content=proposal["content"],
        layer=proposal.get("layer", "shared"),
        room=proposal.get("proposed_room", "living_room"),
        category=proposal.get("category", ""),
        owner_ai=proposal.get("owner_ai", ""),
        source_ai=proposal.get("proposer_ai_id", ""),
        source_platform=proposal.get("source_platform", ""),
        importance=float(proposal.get("importance", 0.5)),
        emotion_arousal=float(proposal.get("emotion_arousal", 0.3)),
        tags=proposal.get("tags"),
        subject_id=proposal.get("subject_id", ""),
        source_actor_id=proposal.get("source_actor_id", ""),
        info_type=proposal.get("info_type", "fact"),
        event_date=proposal.get("event_date", ""),
        source_context=proposal.get("source_context", ""),
        provenance_type=proposal.get("provenance_type", ""),
        fact_confidence=proposal.get("confidence"),
        embedding=proposal.get("embedding"),  # None if not cached
        override_id=f"mem_from_prop_{proposal['id']}",
        override_created_at=proposal.get("created_at"),
        origin=origin,
        supersedes=supersedes,
        proposal_id=proposal["id"],
    )
```

**Golden 测试**（Codex 要求真正等价）:
- `test_v4_h4_normal_create_and_promotion_share_builder` — 相同输入调 `_build_new_memory_payload(**args)` + `_build_new_memory_payload(**args, override_id=X, origin='promotion')`，除 `id / created_at / updated_at / history[0].origin / history[0].proposal_id` 外**完全等价**
- `test_v4_h4_payload_covers_all_ALL_COLUMNS` — dict key 集合 = `_ALL_COLUMNS`（不多不少）
- `test_v4_h4_no_dupe_payload_construction` — AST 断言 `memory_ops.py` / `database.py` 只有 `_build_new_memory_payload` 一处构造完整 memory dict

### C5【M1 修】共享 in-tx kernel（加入 `_IN_TX_HELPERS`）

**新增** `database._commit_promotion_in_tx(conn, prop_id, claim_token, mem_payload, terminal_state, reviewed_by, audit_extra)`：
```python
def _commit_promotion_in_tx(
    conn, prop_id, claim_token, mem_payload,
    terminal_state, reviewed_by, audit_extra=None,
):
    """In-tx helper: caller 已开 _write_transaction。
    ✓ 加入 _IN_TX_HELPERS；仅由 database.py 内 3 个 public wrapper 调用。

    步骤（缺一不可）：
      1. legacy-exclusion 二次防御（v=2 强制）
      2. needs_review 防御（terminal_state='auto_approved' 时 triage 白名单）
      3. Fencing gate: UPDATE proposals ... WHERE id=? AND status='pending'
                        AND promotion_claim_id=?  → rowcount==1 or raise
      4. _set_memory_in_tx(conn, mem_payload)
      5. state_before / state_after 由 kernel 事务内构造（不信 caller）
      6. audit INSERT
    """
    if terminal_state not in ('auto_approved', 'approved'):
        raise ValueError(f"invalid terminal_state: {terminal_state!r}")
    now = _now_iso()
    # 1 & 2 & 3: 读 proposal 完整 row 一次，作 state_before 源
    prop = conn.execute(
        "SELECT id, status, triage_reason, promotion_protocol_version, "
        "       promotion_claim_id, source_message_ids "
        "FROM proposals WHERE id=?", (prop_id,)
    ).fetchone()
    if not prop:
        raise PromotionClaimLost(f"proposal {prop_id} not found")
    if prop['promotion_protocol_version'] != 2:
        raise ValueError(f"refuse to promote v={prop['promotion_protocol_version']}")
    AUTO_TRIAGES = ('auto_approve', 'auto_approve_silent', 'auto_approve_maintenance')
    if terminal_state == 'auto_approved' and prop['triage_reason'] not in AUTO_TRIAGES:
        raise ValueError(
            f"auto_approved requires triage_reason in {AUTO_TRIAGES}, "
            f"got {prop['triage_reason']!r}"
        )
    # 3. fencing CAS
    cur = conn.execute(
        "UPDATE proposals SET status=?, applied_memory_id=?, reviewed_at=?, "
        "reviewed_by=?, promotion_claim_id='', promotion_claim_at='' "
        "WHERE id=? AND status='pending' AND promotion_claim_id=?",
        (terminal_state, mem_payload['id'], now, reviewed_by, prop_id, claim_token)
    )
    if cur.rowcount != 1:
        raise PromotionClaimLost(
            f"proposal {prop_id} claim {claim_token[:8]}... no longer valid"
        )
    # 4. memory
    _set_memory_in_tx(conn, mem_payload)
    # 5. state_before / state_after 事务内构造（不信 caller）
    state_before = {
        "proposal_status": "pending",
        "triage_reason": prop["triage_reason"],
        "protocol_version": prop["promotion_protocol_version"],
        "claim_token_prefix": claim_token[:8],
    }
    state_after = {
        "proposal_status": terminal_state,
        "applied_memory_id": mem_payload['id'],
        "reviewed_by": reviewed_by,
    }
    # 6. audit — action/reason/metadata from caller; state_* from kernel
    extra = audit_extra or {}
    conn.execute(
        "INSERT INTO maintenance_audit "
        "(action, target_id, new_content, source_message_ids, "
        " decision_reason, state_before, state_after, model_id, "
        " source_ai, auto_executed, prompt_version, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            extra.get("action", "promotion_committed"),
            mem_payload['id'],
            mem_payload.get("content", ""),
            prop["source_message_ids"] or "[]",          # ← 从 proposal 事务内读
            extra.get("decision_reason", ""),
            json.dumps(state_before, ensure_ascii=False),
            json.dumps({**state_after, **{k: v for k, v in extra.items()
                                         if k not in ("action", "decision_reason")}},
                       ensure_ascii=False),
            extra.get("model_id", ""),
            mem_payload.get("source_ai", ""),
            1 if terminal_state == 'auto_approved' else 0,
            extra.get("prompt_version", ""),
            now,
        )
    )
    return {
        "memory_id": mem_payload['id'],
        "proposal_id": prop_id,
        "terminal_state": terminal_state,
        "reviewed_by": reviewed_by,
    }
```

**3 个 public wrapper（都自己开一次 `_write_transaction`）**：
1. `commit_promotion_atomic` — auto & manual v=2（无 supersede）
2. `commit_maintenance_promotion_atomic` — auto & manual v=2 带 supersede
3. `adopt_legacy_proposal_atomic` — v=0→v=2 人工专用

三者都在自身 tx 内调 `_commit_promotion_in_tx(conn, ...)`。

**加入 `_IN_TX_HELPERS`**：`_commit_promotion_in_tx` 加入清单；对应 AST 闸门自动生效（其他生产模块禁引用）。

### C6【M2 修】state_before 由 kernel 构造（见 C5 已实现）

在 C5 的 `_commit_promotion_in_tx` 内，`state_before` / `source_message_ids` / claim_token 由事务内 SELECT proposal row 构造。caller 只提供 action / decision_reason / model_id / prompt_version 等 metadata。

### C7【M3 修】补恢复反例（已在 7 组反例内覆盖，见 Q4）

### C8【L1 修】删 `retriaged_approved` state

v3 提到但没 caller。v4 移除。retriage 只 report。

### C9【L2 修】反例数量对齐

7 组关键反例（v3 说 4 后来列 5 —— 已统一）。

### C10【L3 修】行号更新

Q2 Entry A 位置：`memory_ops.py:1105-1139`（v3 写 `1301-1327` 已过期）。

---

## Q4｜施工步骤 + 验收

| Step | 工作 | 验收 |
|---|---|---|
| **S0** | `audit_stuck_proposals.py --report` 存量 40 条分类，输出 content sha256 表 | Ceci 拿到 A/B/C 分类 + hash 表，用于 adopt |
| S1 | Migration (4 列幂等：claim_id/at/protocol_version/target_snapshot_json) + `_PROPOSAL_COLUMNS` + 异常类 | init_db 重跑幂等；旧行 v=0/snapshot=''/claim='' |
| S2 | `_build_new_memory_payload` 抽取 + 正常 create 路径迁移 | 现有全部 remember 测试通过 + 新增 payload 覆盖测试通过 |
| S3 | `_commit_promotion_in_tx` in-tx kernel + `_IN_TX_HELPERS` 更新 + AST 闸门 | in-tx helper 合规；生产代码无外部 `_commit_promotion_in_tx` 调用 |
| S4 | 3 public wrappers: `commit_promotion_atomic` / `commit_maintenance_promotion_atomic` / `adopt_legacy_proposal_atomic` + `reject_proposal_atomic` + `try_claim_promotion` / `mark_promotion_failed` | 单元测试 8 条覆盖 |
| S5 | 4 入口改造：A auto / B maintenance auto (含 snapshot 持久化 + 新 triage `auto_approve_maintenance`) / C manual approve v=2 / C' adopt v=0；`_promote_proposal` 加 DeprecationWarning | 现有 22 条 proposal 测试通过 + 新增 18 条 v4 测试通过 |
| S6 | `retriage_pending_proposals_report_only` + main.py endpoint 换 | endpoint 返 JSON，不写库 |
| S7 | `proposal_sweep.py` + main.py lifespan (`_shutdown` event + batch 20 + ORDER BY + wait_for + hot reload) | sweep 独立起停 |
| S8 | `audit_stuck_proposals.py`：`--report` 输出 content hash + A/B/C 分类；`--adopt <id> --hash <sha>` 走 review_proposal(adopt_legacy) | dry-run 正确；`--adopt` 单条成功；hash mismatch 拒 |
| S9 | 7 组关键反例全部通过 | 见下 |
| S10 | VPS 部署 + Ceci 人工 adopt A 类 | 无新增 v=2 卡池；A 全 adopt；C 有清单 |

**7 组关键反例**（Codex 底线）：
1. `test_v4_h1_adopt_legacy_hash_drift_rejected` — content hash 漂移 → LegacyContentDrift；DB 无变化
2. `test_v4_h1_adopt_legacy_from_recovery_raises` — recovery loop 调 adopt → RuntimeError
3. `test_v4_c1_legacy_orphan_never_recovered_by_sweep` — v=0 orphan 不进 candidates (recovery SQL 硬约束 v=2)
4. `test_v4_h2_maintenance_restart_recovery_from_snapshot` — v=2+auto_approve_maintenance+target_snapshot proposal 重启后被 sweep 完整处理
5. `test_v4_h2_maintenance_drift_marks_failed_no_downgrade` — target 漂移 → promotion_failed，new memory 未 insert，旧 memory 未 supersede
6. `test_v4_h3_reject_vs_auto_race` — 双线程 auto+reject，最终 `auto_approved+1 mem` 或 `rejected+0 mem`，绝无 `rejected+1 mem`
7. `test_v4_h4_normal_create_and_promotion_share_builder` — 除 id/time/history[origin/proposal_id]，字段完全等价（decay_score=1.0 / domain=[] / history schema 一致）

外加防御：
- `test_v4_audit_rollback_on_insert_failure`
- `test_v4_needs_review_never_auto_promoted`

**总估时**：5 天（v3 是 4，v4 多 1 天：S2 正常 create 路径迁 `_build_new_memory_payload` 是最大改动，需要全套 remember 测试重跑）

---

## Q5｜风险

### 高

1. **S2 正常 create 路径迁移的兼容性**
   `remember(quick=False)` 现有代码构造 memory dict 的地方可能有隐藏依赖（例如某个 caller 传 `decay_score=0.9`）。抽 `_build_new_memory_payload` 后所有 remember 测试全跑一遍是唯一保障。若发现某处需要 override，加参数扩展（不 fork）。

2. **maintenance 现有 pending 都是"没 target_snapshot"的旧数据**
   Migration 后新建的 maintenance proposal 才有 snapshot；旧的 v=0 走 adopt legacy 路径（人工提供 target 决策）。若 40 条里有 maintenance 类型的 v=0，adopt 时 reviewed_by 必须显式判断 supersede 决策是否仍合理（不能盲目 adopt）。
   **缓解**：audit_stuck_proposals.py --report 明确标出 v=0 里 maintenance_action != '' 的 proposal，Ceci 单独人查。

### 中

3. **`caller_frame` 断言脆弱**（inspect.stack 慢 + 依赖调用栈名称）
   AST 闸门是主约束；caller_frame 只当作 defense-in-depth。若测试环境 pytest 装 wrapper 层，可能误伤。
   **缓解**：断言列表可通过环境变量 `HUB_ADOPT_LEGACY_ALLOW_FRAMES` 扩展。

### 低

4. sweep interval / batch 参数（600s / 20）观察后可调

---

## 与 PR1 Step 0-A 的关系

- 硬前置：已合入 main
- `_commit_promotion_in_tx` 加入 `_IN_TX_HELPERS`；跨文件 AST 闸门自动覆盖
- `_build_new_memory_payload` 是纯函数，无事务依赖
- 3 wrappers 都走 `_write_transaction` + `_set_memory_in_tx`

---

## 交付流程

1. v4 推分支（doc-only）
2. Claude + Codex 都审
3. 都过 → 开工分支 `phase20/proposal-pending-fix`
4. 分批施工 S0 → S10，每批 Codex 复审
5. 全套测试通过 → 开 PR → Codex ultra 复审 → 合入
6. 合入后 S10 部署 VPS + Ceci 人工 adopt

---

## 附：Codex 转发的根因 + 5 条约束（v1 存档）

详见 git 历史 `779a2c3`（v1 附录）
