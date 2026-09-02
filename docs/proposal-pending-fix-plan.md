# Proposal Pending Bug 修复方案 v5

> 分支：`phase20/proposal-pending-fix-plan-v2` (v5 pass 后 open `phase20/proposal-pending-fix`)
> 硬前置：Step 0-A 已合并 main `da88279`
> 对话式 Proposal 审批 UX（含真正的 actor auth）排后续 PR
>
> **版本历史**：
> - v1（作废）：`remember(existing_id=...)` 不 deterministic；只清 claim 不重跑；无 fencing
> - v2（作废）：拆两阶段但只覆盖 1 入口；旧 40 条会被 orphan 自动扫走
> - v3（作废）：protocol_version 隔离通过，但把人工审批 v=0 数据的路封死；maintenance recovery 3 漏洞
> - v4（作废）：把 legacy adopt 暴露为 MCP action → **任何 AI 可冒充 Ceci**（Critical）；只 hash content 不 hash 完整 fingerprint；maintenance triage 与 wrapper 未强绑定；builder 语义回归
> - **v5**（本文）：Codex v4 review 的 1C+4H+3M+3L 全部收敛

---

## v5 四项最小闭环（Codex v4 Critical + 4 High）

1. **Legacy adoption operator-only** — 只暴露为服务器 CLI (`audit_stuck_proposals.py --adopt-plan reviewed-plan.json`)，**不进 MCP/REST**。理由：MCP 层无 actor-auth，`reviewed_by='Ceci'` 是任何 AI 可写字符串
2. **Canonical fingerprint** — hash 覆盖所有决定正式 memory 的字段 + maintenance snapshot，不再只 hash content
3. **Maintenance triage ↔ wrapper 强绑定** — 普通 wrapper 拒 maintenance triage；maintenance wrapper 拒普通 triage；`_commit_promotion_in_tx` 接收不可伪造的 `allowed_triages` 白名单
4. **Payload builder 挪到中立模块** `memory_payload.py` — `database.py` 和 `memory_ops.py` 都只 import 它；补 linked_memories / domain 规范化 / info_type 空值退化 / supersede comment+history 保留

---

## Q1｜要解决什么问题

- **存量**：40 条 auto_approve pending 卡池
- **结构**：async 中断 / 崩溃 / 重启后自动恢复；两 worker 不重复晋升；同一 proposal 重放不多创建；`needs_review` 严格排除
- **旧数据人工审批**：v0 → v2 adoption 只走 operator CLI，禁止 MCP 暴露

## Q2｜现状（4 promotion 入口 + operator CLI）

| 入口 | 位置 | 现状 | v5 处理 |
|---|---|---|---|
| **A. 普通 auto** | `memory_ops.py:1105-1139` (`_create_proposal` auto_approve 分支) | 3 步非原子 | `commit_promotion_atomic` (allowed_triages={auto_approve, auto_approve_silent}) |
| **B. maintenance auto** | `memory_ops.py:1063-1078` (`_execute_maintenance_action`) | 旧 memory 先 supersede + 独立 tx 再 promote | 重构：triage=`auto_approve_maintenance`；proposal 存 `target_snapshot`；走 `commit_maintenance_promotion_atomic` (allowed_triages={auto_approve_maintenance}) |
| **C. 人工 approve v=2** | `memory_ops.py:1194-1222` (`review_proposal(action='approve')`) | 走老 `_promote_proposal` | 按 maintenance_action 分流：非空 → `commit_maintenance_promotion_atomic`；空 → `commit_promotion_atomic` |
| **D. bulk retriage** | `memory_ops.py:1225-1262` + `main.py:776-780` | 遍历 pending 直接写库 | 改 report-only（不写库）|
| **E. Operator adopt legacy v=0**（CLI） | 新 `tools/audit_stuck_proposals.py --adopt-plan` | v4 曾计划为 MCP action | **仅本机 CLI**；plan JSON immutable + hash 校验；每条 proposal 走 atomic helper；MCP 层不暴露 |

**MCP `review_proposal` 只保留** `approve` (v=2 only) 和 `reject`。`action='adopt_legacy'` 被移除，返回 `error='legacy_operator_only'` + hint 指向 CLI。

---

## Q3｜设计（按 Codex v4 逐条对应）

### C1【Critical 修】Legacy adoption operator-only CLI

**核心原则**：MCP 层 `reviewed_by` 是调用方自填字符串（`mcp_server.py:781`），无身份凭据；任何 AI 都能传 `reviewed_by='Ceci'`。所以 v0→v2 adoption **不能通过 MCP 走**。

**运行时保障**（非仅测试）：
1. **函数不 public 暴露**：`adopt_legacy_proposal_atomic()` 只由 `tools/audit_stuck_proposals.py` import
2. **MCP `review_proposal` 显式拒绝**：
   ```python
   if action == "adopt_legacy":
       return {"error": "legacy_operator_only",
               "hint": "run: python tools/audit_stuck_proposals.py --adopt-plan <plan.json> on the server"}
   ```
3. **CLI operator gate**：`audit_stuck_proposals.py --adopt-plan` 检查：
   - 环境变量 `HUB_OPERATOR_MODE=1`（VPS 上 root shell 手动 export）
   - stdin 是 TTY（拒绝 pipe/redirect 传入 plan）—— 可用 `--force-non-tty` 关闭但需附 `--i-am-operator` 双确认
   - Plan 文件必须在 `/etc/memhub/operator-plans/` 或 `~/.memhub/operator-plans/`（不能是 `/tmp/`）
4. **AST 闸门 + import 拒绝**：`tests/test_v5_ast_adopt_legacy.py` 断言：
   - `database.adopt_legacy_proposal_atomic` 只被 `tools/audit_stuck_proposals.py` 引用
   - `mcp_server.py` 与 `main.py` 任何一处 import 或调用 → 测试失败

**Plan JSON schema**：
```json
{
  "plan_id": "op-plan-2026-09-01-001",
  "created_at": "2026-09-01T10:00:00+00:00",
  "created_by": "operator@vps-hostname",
  "report_sha256": "abc...",
  "items": [
    {
      "proposal_id": "prop_xxx",
      "expected_fingerprint": "sha256(canonical_payload)",
      "operator_note": "已确认 A 类，人格证据 L4",
      "operator_decision": "adopt_as_active"
    },
    ...
  ]
}
```

**Plan 本身自校验**：CLI 计算 `plan_sha256 = sha256(plan without plan_sha256 field)`，与 plan.plan_sha256 比对；不一致拒绝执行。适用于场景：Ceci 通过 report 生成 plan → 复核后签名 → operator 执行 → 不可中途篡改。

**移除 v4 的 caller_frame 断言**（Codex Low）：`inspect.stack()` 慢且脆弱，且不提供身份认证。以上 4 层运行时保障已足够。

### C2【High 1 修】Canonical fingerprint 覆盖完整 payload

**问题**：v4 只 hash `proposal.content`；layer / room / owner_ai / category / tags / importance / emotion_arousal / provenance_type / fact_confidence / subject_id / source_actor_id / info_type / source_context / source_platform / maintenance_action / target_snapshot 都可以在 report 生成后被改动而 hash 依然通过。

**新 helper** `memory_payload.canonical_proposal_fingerprint(proposal_row, target_snapshot=None) -> str`：
```python
CANONICAL_FIELDS = (
    # identity content
    "content",
    # taxonomy
    "layer", "proposed_room", "category", "owner_ai",
    # provenance
    "proposer_ai_id", "source_platform", "source_context",
    "subject_id", "source_actor_id", "info_type",
    # tags & scoring
    "tags",              # normalized to sorted list
    "importance", "emotion_arousal",
    # provenance quality
    "provenance_type", "confidence",
    # event
    "event_date",
    # maintenance
    "maintenance_action",  # '' | 'update' | 'supersede'
)

def canonical_proposal_fingerprint(proposal: dict, target_snapshot: dict | None = None) -> str:
    canonical = {}
    for k in CANONICAL_FIELDS:
        v = proposal.get(k, "")
        if k == "tags":
            v = _normalize_tags(v)   # str/list → sorted list
        elif k in ("importance", "emotion_arousal", "confidence"):
            v = round(float(v), 6) if v not in ("", None) else None
        canonical[k] = v
    if proposal.get("maintenance_action"):
        # snapshot 必须完整参与 fingerprint
        assert target_snapshot is not None, "maintenance proposal requires snapshot"
        canonical["_target_snapshot"] = {
            "target_id": target_snapshot["target_id"],
            "expected_status": target_snapshot["expected_status"],
            "expected_updated_at": target_snapshot["expected_updated_at"],
            "relation": target_snapshot["relation"],
        }
    payload = json.dumps(canonical, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()
```

**Report 时生成 fingerprint**：`audit_stuck_proposals.py --report` 对每条 proposal 计算 fingerprint 并写入 report + plan template。

**Adopt 事务内重新 canonicalize + 比较**：
```python
def adopt_legacy_proposal_atomic(prop_id, expected_fingerprint, reviewed_by, plan_id):
    with _write_transaction() as conn:
        prop_row = _fetch_proposal_full(conn, prop_id)
        snap = _parse_target_snapshot(prop_row)  # None if not maintenance
        current_fp = canonical_proposal_fingerprint(prop_row, snap)
        if current_fp != expected_fingerprint:
            raise LegacyContentDrift(
                f"proposal {prop_id} fingerprint drift: "
                f"expected {expected_fingerprint[:12]}..., got {current_fp[:12]}..."
            )
        ...
```

反例：`test_v5_c2_fingerprint_covers_all_canonical_fields` — 报告后修改 tags/importance/owner_ai/subject_id 中任意一个 → adopt 拒绝。

### C3【High 2 修】Maintenance triage ↔ wrapper 强绑定

**问题**：v4 把 `auto_approve_maintenance` 加进 kernel 白名单后，caller 若对 maintenance proposal 错误地调用 `commit_promotion_atomic()`（普通 wrapper），kernel 会：过 triage 防御 → 创建新 memory → **不 supersede target** → 两条 active 冲突。

**v5 修复**：wrapper 传入不可伪造的 `allowed_triages` 白名单，kernel 校验；两个白名单**互斥**。

```python
# database._commit_promotion_in_tx
NORMAL_AUTO_TRIAGES   = frozenset({'auto_approve', 'auto_approve_silent'})
MAINT_AUTO_TRIAGES    = frozenset({'auto_approve_maintenance'})
MANUAL_TRIAGES        = frozenset({'needs_review'})  # manual approve after v=2

def _commit_promotion_in_tx(
    conn, prop_id, claim_token, mem_payload,
    terminal_state, reviewed_by,
    allowed_triages: frozenset[str],   # ← 新增，wrapper 强制传入
    audit_extra=None,
):
    prop = _fetch_promotion_row(conn, prop_id)
    if prop['triage_reason'] not in allowed_triages:
        raise WrongWrapper(
            f"proposal {prop_id} triage={prop['triage_reason']!r} "
            f"not in allowed_triages={sorted(allowed_triages)}"
        )
    if terminal_state == 'auto_approved' and prop['triage_reason'] not in (
        NORMAL_AUTO_TRIAGES | MAINT_AUTO_TRIAGES
    ):
        raise ValueError("auto_approved requires auto triage")
    ...
```

**3 个 wrapper 固定传入**（wrapper 参数 hardcode，不接受外部 override）：

| Wrapper | 用途 | `allowed_triages` |
|---|---|---|
| `commit_promotion_atomic` | 普通 auto + 普通 manual approve | `NORMAL_AUTO_TRIAGES \| MANUAL_TRIAGES` |
| `commit_maintenance_promotion_atomic` | maintenance auto + maintenance manual approve | `MAINT_AUTO_TRIAGES \| MANUAL_TRIAGES` |
| `adopt_legacy_proposal_atomic`（operator CLI） | v=0 → v=2 | 由 CLI 根据 proposal 有无 maintenance_action 分流到上面两个 wrapper 之一，不引入第三个白名单 |

**Manual approve 分流（v=2）**：
```python
async def review_proposal(proposal_id, action, reviewed_by, ...):
    prop = await get_proposal(proposal_id)
    if action == "approve":
        if prop["promotion_protocol_version"] == 0:
            return {"error": "legacy_operator_only"}
        if prop.get("maintenance_action"):
            # 从 target_snapshot_json 重建 snapshot（proposal 创建时已持久化）
            snap = json.loads(prop["target_snapshot_json"])
            return database.commit_maintenance_promotion_atomic(
                proposal_id, claim_token, mem_payload, snap, reviewed_by
            )
        return database.commit_promotion_atomic(
            proposal_id, claim_token, mem_payload, reviewed_by
        )
    if action == "reject":
        return database.reject_proposal_atomic(proposal_id, reviewed_by, reject_reason)
```

**Legacy maintenance adoption**：Codex Medium 1 建议"旧 maintenance 一律归 C 类禁止 generic adopt"。v5 采纳：
- Operator CLI 读到 plan 里某条 proposal `maintenance_action != ''` 且 v=0 且**无 target_snapshot_json**（旧数据没有）→ CLI 拒绝：`"proposal <id> is legacy maintenance without snapshot; operator must create a fresh v=2 maintenance proposal instead of adopting"`
- CLI 输出一条建议：`"delete-and-recreate: reject <id>, then run maintenance again to produce fresh proposal"`

**反例（新增）**：
- `test_v5_c3_wrong_wrapper_maintenance_via_normal_fails` — auto_approve_maintenance proposal 调普通 wrapper → WrongWrapper；memory 未创建
- `test_v5_c3_wrong_wrapper_normal_via_maintenance_fails` — auto_approve proposal 调 maintenance wrapper → WrongWrapper
- `test_v5_c3_legacy_maintenance_adopt_rejected_by_cli` — v=0 且 maintenance_action='supersede' 无 snapshot → CLI 拒绝

### C4【High 3 + High 4 修】Builder 挪中立模块 + 语义回归修

**新模块** `memory_payload.py`（无 DB 依赖）：
```python
"""Pure memory payload builder & fingerprint helpers.
Both database.py and memory_ops.py import from here; no back-references.
"""

from datetime import datetime, timezone
import json, hashlib, uuid

def _normalize_tags(v) -> list:
    if v in ("", None): return []
    if isinstance(v, str):
        try: parsed = json.loads(v)
        except json.JSONDecodeError: return [v]
        return sorted(parsed) if isinstance(parsed, list) else [str(parsed)]
    return sorted(list(v))

def _normalize_domain(v) -> str:
    """analyzer 输出 list；DB 存 JSON 字符串。空值→'[]'。"""
    if v in ("", None, [], "[]"): return "[]"
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
            return json.dumps(parsed, ensure_ascii=False, sort_keys=True)
        except json.JSONDecodeError:
            return json.dumps([v], ensure_ascii=False)
    return json.dumps(v, ensure_ascii=False, sort_keys=True)

def build_new_memory_payload(
    *,
    # required semantics
    content: str,
    # taxonomy
    layer: str = "shared", room: str = "living_room",
    category: str = "", owner_ai: str = "",
    # provenance
    source_ai: str = "", source_platform: str = "",
    subject_id: str = "", source_actor_id: str = "",
    info_type: str = "",           # ← 空→"fact" 由 caller 之前 or 这里做
    event_date: str = "", source_context: str = "",
    provenance_type: str = "", fact_confidence: float | None = None,
    # scoring
    importance: float = 0.5, emotion_arousal: float = 0.3, valence: float = 0.5,
    domain=None, tags=None,
    # relations
    linked_memories=None,           # ← v5 新增：list of mem_id
    supersedes: list[str] | None = None,
    # embedding
    embedding: bytes | None = None,
    # injection (Codex Low 3: 使 builder 完全 pure)
    override_id: str | None = None,
    override_now: str | None = None,
    # provenance meta
    origin: str = "normal_create",
    proposal_id: str = "",
) -> dict:
    """Pure. No DB, no clock (if override_now provided), no RNG (if override_id provided)."""
    now = override_now or datetime.now(timezone.utc).isoformat()
    mem_id = override_id or f"mem_{uuid.uuid4().hex[:12]}"
    resolved_info_type = info_type or "fact"          # ← 空退化保持原 remember() 语义
    canonical_domain = _normalize_domain(domain)
    canonical_tags = json.dumps(_normalize_tags(tags), ensure_ascii=False)
    canonical_linked = json.dumps(_normalize_tags(linked_memories), ensure_ascii=False)
    canonical_supersedes = json.dumps(list(supersedes or []), ensure_ascii=False)
    # history: 正常 create 保持现状 {v, content, date, by}；promotion 附加通过参数显式追加
    history_entry = {"v": 1, "content": content, "date": now, "by": source_ai or "system"}
    if origin != "normal_create":
        history_entry["origin"] = origin
        if proposal_id:
            history_entry["proposal_id"] = proposal_id
    return {
        "id": mem_id, "content": content,
        "layer": layer, "room": room, "category": category, "owner_ai": owner_ai,
        "importance": float(importance), "emotion_arousal": float(emotion_arousal),
        "valence": float(valence), "domain": canonical_domain,
        "decay_score": 1.0, "activation_count": 0, "last_activated": "",
        "source_ai": source_ai, "source_platform": source_platform,
        "tags": canonical_tags,
        "linked_memories": canonical_linked,
        "supersedes": canonical_supersedes, "superseded_by": "",
        "event_date": event_date, "source_context": source_context,
        "comments": "[]", "embedding": embedding,
        "status": "active", "created_at": now, "updated_at": now,
        "history": json.dumps([history_entry], ensure_ascii=False),
        "resolved": None, "anchored": None,
        "provenance_type": provenance_type, "fact_confidence": fact_confidence,
        "subject_id": subject_id, "source_actor_id": source_actor_id,
        "info_type": resolved_info_type,
        # PR C skeleton
        "client_request_id": "", "link_to_real_id": "",
        "finalize_claim_id": "", "finalize_claim_at": "",
    }

def promotion_payload_from_proposal(
    proposal: dict, *,
    origin: str = "promotion",
    supersedes: list[str] | None = None,
    linked_memories=None,
    override_now: str | None = None,
) -> dict:
    return build_new_memory_payload(
        content=proposal["content"],
        layer=proposal.get("layer", "shared"),
        room=proposal.get("proposed_room", "living_room"),
        category=proposal.get("category", ""),
        owner_ai=proposal.get("owner_ai", ""),
        source_ai=proposal.get("proposer_ai_id", ""),
        source_platform=proposal.get("source_platform", ""),
        subject_id=proposal.get("subject_id", ""),
        source_actor_id=proposal.get("source_actor_id", ""),
        info_type=proposal.get("info_type", ""),
        event_date=proposal.get("event_date", ""),
        source_context=proposal.get("source_context", ""),
        provenance_type=proposal.get("provenance_type", ""),
        fact_confidence=proposal.get("confidence"),
        importance=float(proposal.get("importance", 0.5)),
        emotion_arousal=float(proposal.get("emotion_arousal", 0.3)),
        tags=proposal.get("tags"),
        domain=proposal.get("domain"),
        embedding=proposal.get("embedding"),
        linked_memories=linked_memories,
        supersedes=supersedes,
        override_id=f"mem_from_prop_{proposal['id']}",
        override_now=proposal.get("created_at") or override_now,
        origin=origin,
        proposal_id=proposal["id"],
    )

# canonical_proposal_fingerprint(...) — 见 C2
```

**Import 拓扑**：
- `memory_payload.py`：无内部依赖
- `database.py` → import `memory_payload` （用于 fingerprint 校验 + 事务内构造 mem dict）
- `memory_ops.py` → import `memory_payload` + import `database`
- 无循环。

**正常 create 迁移**（S2 施工）：所有 `memory_ops.remember()` 里构造 memory dict 的两个位置（普通 create + relation supersede create）改调 `build_new_memory_payload(**kwargs, linked_memories=..., supersedes=...)`。

**Golden 测试（Codex 要求扩展至 5 场景）**：
- `test_v5_h3_builder_normal_no_relation` — 现有 remember() 无 relation 输入 → builder 输出 = 现有实际 memory dict（除 id/time）
- `test_v5_h3_builder_supplements_linked` — 有 linked_memories → 保留
- `test_v5_h3_builder_same_topic_linked` — 同 topic 多 linked_memories
- `test_v5_h3_builder_supersede` — supersedes 非空
- `test_v5_h3_builder_empty_info_type_falls_back_to_fact`
- `test_v5_h3_builder_analyzer_domain_list_becomes_canonical_json`
- `test_v5_h3_no_dupe_construction` — AST 断言：全代码库只有 `memory_payload.build_new_memory_payload` 一处构造完整 mem dict

### C5【Medium 2 修】`target_snapshot_json` 严格 schema

用 Pydantic v2 model（extra='forbid'），解析失败 → `promotion_failed` + report 到 needs_review 队列，绝不进 generic create。

```python
# memory_payload.py
from pydantic import BaseModel, ConfigDict, field_validator

class TargetSnapshot(BaseModel):
    model_config = ConfigDict(extra='forbid')
    target_id: str
    expected_status: str  # 见 validator
    expected_updated_at: str  # ISO8601
    relation: str  # 'update' | 'supersede'

    @field_validator("target_id")
    def _tid_nonempty(cls, v):
        if not v.strip(): raise ValueError("target_id empty")
        return v

    @field_validator("expected_status")
    def _status_allowed(cls, v):
        if v not in ("active", "archived", "superseded"):
            raise ValueError(f"invalid status {v!r}")
        return v

    @field_validator("expected_updated_at")
    def _iso_time(cls, v):
        datetime.fromisoformat(v)  # raises if invalid
        return v

    @field_validator("relation")
    def _relation_allowed(cls, v):
        if v not in ("update", "supersede"):
            raise ValueError(f"invalid relation {v!r}")
        return v

def parse_target_snapshot(raw: str) -> TargetSnapshot:
    return TargetSnapshot.model_validate_json(raw)
```

`commit_maintenance_promotion_atomic` 事务开头调 `parse_target_snapshot(prop_row['target_snapshot_json'])`；异常 → `mark_promotion_failed(reason='snapshot_invalid')`。

反例：
- `test_v5_m2_snapshot_extra_field_rejected`
- `test_v5_m2_snapshot_invalid_status_rejected`
- `test_v5_m2_snapshot_invalid_time_rejected`

### C6【Medium 3 修】Supersede 保留 comment/history

复用现有 `_execute_maintenance_action` 里给旧 memory 追加 supersede note 的逻辑，抽成 `memory_payload.append_supersede_note(old_mem, new_mem_id, reason, now) -> dict`（pure）：
```python
def append_supersede_note(old_mem: dict, new_mem_id: str, reason: str, now: str) -> dict:
    """Return updated old_mem dict with:
      - status='superseded', superseded_by, updated_at
      - comments: append {"date", "by":"system", "text": f"superseded by {new_mem_id}: {reason}"}
      - history: append {"v": last_v+1, "content": ..., "date": now, "by":"system", "op":"supersede", "superseded_by": new_mem_id, "reason": reason}
    """
    ...
```

`commit_maintenance_promotion_atomic` 事务内：
```python
old_mem_updated = append_supersede_note(old_mem, mem_payload['id'], reason, now)
_set_memory_in_tx(conn, old_mem_updated)
```

Audit `state_after` 同时包含新 memory + 旧 target 最终状态（`superseded_target_final_status`）。

### C7【Low 修】杂项

- **删除 `inspect.stack` caller_frame 断言**（C1 已交给 CLI 运行时 gate + AST + MCP 层拒绝）
- **AST 规则纠正**：`test_v5_ast_adopt_legacy` 允许 `adopt_legacy_proposal_atomic` 的调用者为 `tools/audit_stuck_proposals.py` 里的 `_apply_plan_item` 函数（不是 `review_proposal`；v5 已彻底移除 MCP action）
- **Builder 真正 pure**：`override_id / override_now` 参数已加，golden test 传入固定值即可完全确定

---

## Q4｜施工步骤 + 验收

| Step | 工作 | 天数 | 验收 |
|---|---|---|---|
| **S0** | `audit_stuck_proposals.py --report` 存量 40 条分类 + canonical fingerprint | 0.5 | Ceci 拿到 A/B/C 分类 + fingerprint 表 |
| S1 | Migration (4 列幂等：claim_id/at/protocol_version/target_snapshot_json) + `_PROPOSAL_COLUMNS` + 异常类 (LegacyContentDrift/WrongWrapper/PromotionClaimLost/MaintenanceDrift) | 0.5 | init_db 重跑幂等；旧行 v=0 |
| **S2** | 新建 `memory_payload.py`（build_new_memory_payload / promotion_payload_from_proposal / canonical_proposal_fingerprint / parse_target_snapshot / append_supersede_note）+ 正常 create 路径迁移 + 全套 remember 测试重跑 | **1.5** | 无回归 + 6 组 builder golden 通过 |
| S3 | `_commit_promotion_in_tx` in-tx kernel（含 allowed_triages 参数）+ `_IN_TX_HELPERS` 更新 + AST 闸门 | 0.5 | in-tx helper 合规 |
| S4 | 2 public wrappers (`commit_promotion_atomic` / `commit_maintenance_promotion_atomic`) + `reject_proposal_atomic` + `try_claim_promotion` + `mark_promotion_failed` + `adopt_legacy_proposal_atomic` (module-private) | 1 | 单元测试 10 条覆盖 |
| S5 | 4 入口改造：A auto / B maintenance auto (snapshot 持久化 + 新 triage) / C manual approve v=2 (按 maintenance_action 分流) / D retriage → report-only；MCP `review_proposal` 拒 `adopt_legacy` | 1 | 全部 proposal 测试通过 |
| S6 | `tools/audit_stuck_proposals.py`：`--report`（含 canonical fingerprint）/ `--adopt-plan <plan.json>`（含 4 层 operator gate + plan_sha256 自校验 + legacy maintenance 拒绝）| 1 | dry-run 正确；单条 adopt 成功；hash mismatch/legacy maint/非 TTY 全拒 |
| S7 | `proposal_sweep.py` + main.py lifespan | 0.5 | sweep 独立起停 |
| S8 | 12 组关键反例 + VPS 部署 + Ceci 用 report 出 plan → operator 执行 | 0.5 | 全绿 |

**总估时**：6.5 天（v4 是 5，v5 多 1.5：新增中立模块 + operator CLI gate + fingerprint + snapshot pydantic + supersede note helper）

**12 组关键反例**：

Critical:
1. `test_v5_c1_mcp_adopt_legacy_returns_operator_only_error`
2. `test_v5_c1_ast_adopt_legacy_import_restricted_to_cli`
3. `test_v5_c1_cli_rejects_non_tty_without_double_flag`
4. `test_v5_c1_cli_rejects_plan_sha256_mismatch`

High:
5. `test_v5_c2_fingerprint_covers_all_canonical_fields` — 改 tags/importance/owner_ai 任一 → 拒
6. `test_v5_c2_fingerprint_includes_target_snapshot_for_maintenance`
7. `test_v5_c3_wrong_wrapper_maintenance_via_normal_fails`
8. `test_v5_c3_wrong_wrapper_normal_via_maintenance_fails`
9. `test_v5_c3_legacy_maintenance_adopt_rejected_by_cli`
10. `test_v5_h3_builder_normal_equivalence_across_5_scenarios`

Medium:
11. `test_v5_m2_snapshot_pydantic_extra_forbid`
12. `test_v5_m3_supersede_note_preserves_old_comment_history`

外加历史反例继续保留：
- `test_v5_h2_maintenance_restart_recovery_from_snapshot`
- `test_v5_h2_maintenance_drift_marks_failed_no_downgrade`
- `test_v5_h3_reject_vs_auto_race`
- `test_v5_audit_rollback_on_insert_failure`
- `test_v5_needs_review_never_auto_promoted`
- `test_v5_c1_legacy_orphan_never_recovered_by_sweep`

---

## Q5｜风险

### 高

1. **正常 create 路径迁移（S2）的兼容性**
   `memory_ops.remember(quick=False)` 现有 2 处构造点：普通 create + relation supersede create。改调 `build_new_memory_payload` 后所有 remember 测试全跑。若发现某处需 override（例如 quick=True 走的路径写 decay_score=0.5），加参数扩展；不 fork。

2. **旧 40 条 maintenance 类 proposal 无 target_snapshot 全部归 C 类**
   CLI 拒 adopt，Ceci 需人工新建 v=2 maintenance proposal 复现决策。若某个决策"target 已被别的原因改动，supersede 不再合理"—— 反而正确暴露了这个漏洞（v4 会盲目执行）。

### 中

3. **Operator CLI 使用门槛**：Ceci 不上 VPS。首次使用时我需要引导，或让 SSH root 手动执行；后续对话式审批 PR 会补充 web/MCP 端的真正 actor-auth。

4. **Pydantic v2 依赖**：项目 requirements.txt 若未固定 pydantic v2，需加。

### 低

5. Sweep interval / batch 参数（600s / 20）观察后可调

---

## 与 Step 0-A 的关系

- 硬前置：已合入 main `da88279`
- `_commit_promotion_in_tx` 加入 `_IN_TX_HELPERS`；跨文件 AST 闸门自动覆盖
- `memory_payload.py` 是无 DB 依赖模块；`database.py` + `memory_ops.py` 都 import 它，无循环
- 3 wrappers 都走 `_write_transaction` + `_set_memory_in_tx`

---

## 与后续 PR 的关系

- **对话式 Proposal 审批 UX（后续 PR）** 需先落地真正的 actor-auth：MCP/HTTP 层区分调用主体是 AI 还是 Ceci 本人（cookie / session / signed token）。actor-auth 落地后，可以把 `adopt_legacy` 迁移为 MCP action + `reviewed_by=ctx.human_actor`。**本 PR 不涉及**。

---

## 交付流程

1. v5 推分支（doc-only）
2. Claude + Codex 都审
3. 都过 → 开工分支 `phase20/proposal-pending-fix`
4. 分批施工 S0 → S8，每批 Codex 复审
5. 全套 30+ 测试通过 → 开 PR → Codex ultra 复审 → 合入
6. 合入后：S6 CLI 部署到 VPS；Ceci 生成 report → 复核 → 我在 SSH root 上跑 `--adopt-plan`
