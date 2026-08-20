# Phase 2.0 PR1 施工方案 v2.1 · Data Health

> 分支：`phase20/pr1-data-health`
> 依赖：Phase 1.7 全部合并（main 至 `604bc72`）
>
> **版本历史**：
> - v1（作废）：分析假设错误 4 处
> - v2：Codex 审后收敛 2C+6H+6M
> - **v2.1**（本文）：user 审后修 2 阻塞点（H3 锁未闭环 + H1 独白判定过宽）

---

## v2 → v2.1 变更（2 阻塞项）

### 阻塞 A：H3 `_WRITE_LOCK` 闭环

**问题**（reviewer 确认）：v2 说"复用 `memory_ops._WRITE_LOCK`"但没定明确契约。现状：
- `memory_ops.py:719` 有 `_WRITE_LOCK = threading.Lock()`
- 只有 `_touch_recalled_memories`（line 737）+ `_check_auto_resolve`（line 847）持有
- **`database.commit_maintenance_atomic()` 内部 `BEGIN IMMEDIATE` 但不持锁**（这是 Phase 1.7 遗留 gap）
- 单让 backfill 拿锁 → 挡不住其他维护路径并发进入同一 conn

**v2.1 方案**（走 reviewer 推荐路径 A：锁下沉到 database.py）：

**Step 0 (PR1 起手第一件事)：`_WRITE_LOCK` 从 `memory_ops.py` 迁到 `database.py`**

```python
# database.py 顶部新增（PR1 Step 0）
import threading
_WRITE_LOCK = threading.Lock()
"""Module-level lock serializing all BEGIN IMMEDIATE on the shared
sqlite connection. All commit_* helpers acquire it internally.
Callers MUST NOT wrap the helpers in this lock (nested acquire deadlocks
via threading.Lock which is non-reentrant).
"""
```

**`memory_ops.py` 保留 backward-compat 引用**：
```python
# memory_ops.py:719 改成
from database import _WRITE_LOCK  # re-export for backcompat
```

**`commit_maintenance_atomic` v2.1 内部持锁**：
```python
def commit_maintenance_atomic(...):
    with _WRITE_LOCK:  # v2.1 内部持锁，caller 不再包
        conn = _get_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            ...
            conn.commit()
        except:
            conn.execute("ROLLBACK")
            raise
```

**同一提交更新其他共享连接 `BEGIN IMMEDIATE` 路径**：
- `commit_finalize_atomic`（Phase 1.7 PR C）→ 也移到内部持锁（对齐契约）
- `_check_auto_resolve` → 从外层 with 改为调用内部持锁的 helper
- `_touch_recalled_memories` → 同上
- 全部现有和新增的 `commit_*` 函数：**契约变成"caller 绝对不能包锁"**（threading.Lock 非重入，包了会死锁）

**测试证明契约生效**（v2.1 新增，非常关键）：
1. `test_h3_backfill_and_finalize_concurrent_no_deadlock`：backfill execute + async finalize 并行，无死锁
2. `test_h3_backfill_and_touch_concurrent_serialized`：backfill + recall touch 并行，观察最终状态一致（都成功，非交错破坏）
3. `test_h3_nested_acquire_would_deadlock_guard`：模拟 caller 意外包锁 → 用 wait_for 超时兜底断言（防未来 caller 忘 contract）

**Step 0 独立提交**（不与 D-1/D-2 混），Codex 复审 pass 后再往上加 PR1 主体。

### 阻塞 B：H1 独白判定 `OR source_actor_id` 过宽

**问题**（reviewer 反例）：
```
source_ai         = 'cloudy'  # 说话的 AI
source_actor_id   = 'cloudy'  # 谁说
subject_id        = 'user'    # 关于谁
content           = 'Ceci 喜欢喝茶'
```
这是 Cloudy 陈述 **关于用户** 的信息，不是 Cloudy 独白。v2 的 `subj == source_ai OR actor == source_ai` 会误命中。

**v2.1 方案**：**主判据只用 `subject_id`**，`source_actor_id` 只作辅助验证（不能脱离 subject_id 单独判）。

```python
def is_ai_soliloquy_structured(mem, source_ai):
    """AI 独白 = 关于 AI 自己的记忆（by subject_id）。
    source_actor_id 只表示"谁说的"，不是"关于谁"，不能单独判定独白。"""
    if not source_ai:
        return False
    aliased_ai = _AI_ALIASES.get(source_ai, source_ai)
    subj = (mem.get('subject_id') or '').strip()
    if not subj:
        return False  # 未知 subject → 保守视为非独白
    aliased_subj = _AI_ALIASES.get(subj, subj)
    return aliased_subj == aliased_ai
```

**关键决策**：
- **完全放弃 `source_actor_id` 主判据**（reviewer 反例证明它单独用会误伤"AI 关于用户"记忆）
- **subject_id 空 → 保守视为非独白**（不当独白就少改一层，safe default）
- **subject_id 用别名归一**（`cloudy == claude` 场景）

**必须新增反例测试**（v2.1）：
- `test_soliloquy_ai_about_user_not_flagged` — reviewer 原反例：`subject_id=user + source_actor_id=cloudy + source_ai=cloudy` → **False**
- `test_soliloquy_source_actor_id_alone_insufficient` — `subject_id='' + source_actor_id=cloudy + source_ai=cloudy` → **False**
- `test_soliloquy_subject_ai_via_alias` — `subject_id=cloudy + source_ai=claude` → **True**（别名归一）
- `test_soliloquy_subject_other_ai_not_flagged` — `subject_id=jasper + source_ai=claude` → **False**

---

## Q1｜要解决什么问题？

摸底后确认 7 类 write 污染：

| 类 | 现象 | 位置 |
|---|---|---|
| **越界值** | `importance=9.0` / NaN / inf 能写进 DB | 所有 create 路径无 clamp / 无类型校验 |
| **`relationships` 复数房间**（v2 澄清）| 有的 AI 写进 `relationship`（AI 私有），有的写进 `relationships`（共享人物），**该分开的时候合并了、该合并的时候分开了** | 提取器和 conversation_capture 混用 |
| **owner_ai 空** | AI 独白进 `dreams` 但 `owner_ai=''` | `memory_ops.remember()` 未按 subject_id 自动补 |
| **prefix 错配**（v2 降级）| `[用户] 我梦见...` 明显是 AI 独白 | 存量情况复杂，改**report-only 人工审** |
| **event 时间感缺失** | 30 天前事件读出来 AI 以为"最近" | recall/corridor/smart_context/gateway/dream_recall 输出层未标注 |
| **State 无时效** | "最近很烦躁" 三天前的今天当"当前状态" | 无 valid_from/until，daemon 重写不检查 |
| **Context isolation 缺失**（Lucien 硬约束 1）| game 内容进 living_room、roleplay 进 personality | 无跨房间禁入清单；需要**新加 `context_kind` 字段**承载分类 |

**Fix-completeness 三问**：修生成器（write validation）+ 修存量（backfill 走 plan/execute）+ 修消费路径（4 入口输出层注入）。

---

## Q2｜现在 Hub 是什么样？

### CREATE 点精确到 6 个（v2 修正保留）

| # | 位置 | create 场景 |
|---|---|---|
| 1 | `memory_ops.remember()` line 484 | create-no-relation 分支（默认路径）|
| 2 | `memory_ops.remember()` line 424 | create-with-supersede 分支 |
| 3 | `database.insert_pending_memory()` | MCP async remember 骨架 |
| 4 | `daemon.py::compress_diaries()` line 219 | weekly 周报合成 |
| 5 | `daemon.py::distill_psychology()` line 292 | career_mem 合成 |
| 6 | `daemon.py::tidy_living_room()` line 467 | chapter_mem 合成 |

`_promote_proposal` **不算独立点**——docstring 明写 "via remember(quick=False)"，validation 由 remember 覆盖。

### `relationships` vs `relationship`（v2 澄清保留）

`corridor.py:209` `relationship`（AI 私有关系）vs `corridor.py:217` `relationships`（共享人物索引）— 两个不同房间，不能 alias。

### `source_context` 是原始文本（v2 澄清保留）

`memory_ops.py:1158` 里 `evidence_excerpt = source_context[:500]` — free text。必须新增 categorical `context_kind` 字段。

### `_WRITE_LOCK` 目前 gap（v2.1 阻塞 A 详细）

现在只 `memory_ops:719` 有，`commit_maintenance_atomic` 不持有。**v2.1 Step 0 下沉到 database.py 内部持锁**。

### `subject_id / source_actor_id` 已可用

主判据只用 `subject_id`（v2.1 阻塞 B）。

---

## Q3｜打算改成什么？

### Step 0（v2.1 新增前置）：`_WRITE_LOCK` 迁移 + `commit_*` 内部持锁

- 从 `memory_ops.py:719` 迁到 `database.py` 顶部
- `commit_maintenance_atomic` / `commit_finalize_atomic` 内部持锁
- `_check_auto_resolve` / `_touch_recalled_memories` 现有外层 with 改为调用内部持锁的 helper
- `memory_ops` 保留 `from database import _WRITE_LOCK` re-export（backcompat + 让 test 能直接 import）
- **契约**：所有 `commit_*` 内部持锁，caller 绝对不能包（threading.Lock 非重入）
- **3 条测试**证明契约生效（backfill+finalize 并发、backfill+touch 并发、nested guard timeout）
- **独立提交**，Codex 复审 pass 后再进 D-1/D-2

### D-0：`context_kind` 字段贯穿全链路

**新列**（`memories` + `proposals`）：
```sql
ALTER TABLE memories  ADD COLUMN context_kind TEXT NOT NULL DEFAULT '';
ALTER TABLE proposals ADD COLUMN context_kind TEXT NOT NULL DEFAULT '';
```
值域：`''`（未标）/ `'game'` / `'dream'` / `'roleplay'` / `'joke'` / `'chat'` / `'system'`

链路：extractor → proposal → remember → pending → finalize → daemon 全串。

**Validation 依据**：`validate_context_isolation` 用 `mem.get('context_kind')`。

### D-1：`memory_validation.py`（约 260 行）

**导出符号**：
```python
ROOM_ALIASES: dict[str, str]
PER_AI_ROOMS: frozenset[str]
CONTEXT_PRIMARY_ROOM: dict[str, str]      # v2 M2 确定性
CONTEXT_ALLOWED_ROOMS: dict[str, frozenset[str]]

def validate_memory_write(mem, source_ai) -> dict: ...
def validate_context_isolation(mem) -> dict: ...  # 可能 raise ValueError
def is_ai_soliloquy_structured(mem, source_ai) -> bool: ...  # v2.1: 只用 subject_id
def safe_clamp_importance(val) -> float: ...  # v2 M6: NaN/inf/非数字
```

**`ROOM_ALIASES` v2 收敛**：
```python
ROOM_ALIASES = {
    'preference': 'preferences',
    # v2 C1: 删除 'relationships': 'relationship'（两个不同房间）
}
```

**`validate_memory_write` 顺序**：
1. `safe_clamp_importance()` — NaN/inf/非数字/越界都归位
2. `_normalize_room()` — 仅 canonical typo
3. `_backfill_owner_ai_by_soliloquy()` — v2.1 用 structured soliloquy 判定
4. `_normalize_room_by_ownership()` — v2 智能路由 `relationship` vs `relationships`
5. `validate_context_isolation()` — 用 `context_kind`

**独白判定 v2.1**（阻塞 B 已改）：
```python
def is_ai_soliloquy_structured(mem, source_ai):
    """主判据：subject_id 是 AI 自己（含别名归一）。
    source_actor_id 只表示 speaker，不能单独判独白。
    subject_id 空 → 保守视为非独白。"""
    if not source_ai:
        return False
    aliased_ai = _AI_ALIASES.get(source_ai, source_ai)
    subj = (mem.get('subject_id') or '').strip()
    if not subj:
        return False
    aliased_subj = _AI_ALIASES.get(subj, subj)
    return aliased_subj == aliased_ai
```

**Prefix 修复 v2 降级**：只在 backfill `--check prefix` 输出 report_only，人工审。不动 content。

### D-2 (Event)：`_annotate_event()` v2 M1

```python
EVENT_STALE_DAYS = 30

def annotate_event(mem, now_utc):
    if mem.get('info_type') != 'event':
        return mem
    ts_str = (mem.get('event_date') or '').strip() or mem.get('created_at', '')
    ts = _parse_iso_safe(ts_str)
    if ts is None:
        return mem
    days = max(0, (now_utc - ts).days)  # v2 M6 clamp future
    if days > EVENT_STALE_DAYS:
        annotated = dict(mem)
        annotated['content'] = f'[{ts.strftime("%Y-%m-%d")}] {mem["content"]}'
        return annotated
    return mem
```

### D-2 (State)：`state_ttl.py` + 5 列 migration

Migration：
```sql
ALTER TABLE memories ADD COLUMN valid_from TEXT NOT NULL DEFAULT '';
ALTER TABLE memories ADD COLUMN valid_until TEXT NOT NULL DEFAULT '';
ALTER TABLE memories ADD COLUMN last_confirmed_at TEXT NOT NULL DEFAULT '';
ALTER TABLE memories ADD COLUMN state_ttl_days INTEGER NOT NULL DEFAULT 7;
ALTER TABLE memories ADD COLUMN context_kind TEXT NOT NULL DEFAULT '';
```

`_ALL_COLUMNS` 加 5 项，`_preserve_on_empty` 加 3（`valid_from/valid_until/last_confirmed_at` + `context_kind`）；`state_ttl_days` 不 preserve（DEFAULT 7），`_prep` 空/None → 7。

**State supersede 隔离键 v2 H2**（5 字段全非空才成 key）：
```python
STATE_KEY_FIELDS = ('subject_id', 'category', 'room', 'layer', 'owner_ai')
def state_supersede_key(mem):
    key = tuple((mem.get(f) or '').strip() for f in STATE_KEY_FIELDS)
    if any(not part for part in key):
        return None  # v2 H2 空 key skip
    return key
```

**`supersede_state_atomic`**：**内部走 `commit_maintenance_atomic`** 而不是自己 `BEGIN IMMEDIATE` — 复用 Step 0 已下沉的锁契约。**不需要**再自己加 `with _WRITE_LOCK`（会 nested deadlock）。

```python
def supersede_state_atomic(new_state_mem, source_ai):
    key = state_supersede_key(new_state_mem)
    if key is None:
        return None
    # 找同 key 老 state（读操作不需锁）
    conn = database._get_conn()
    row = conn.execute(
        "SELECT id, updated_at FROM memories WHERE status='active' "
        "AND info_type='state' AND subject_id=? AND category=? "
        "AND room=? AND layer=? AND owner_ai=? AND id != ?",
        (*key, new_state_mem.get('id', ''))
    ).fetchone()
    if not row:
        return None
    old_id, old_updated_at = row
    # 走 Step 0 下沉后的原子 helper
    try:
        database.commit_maintenance_atomic(
            memory_id=old_id,
            memory_updates={'status': 'superseded',
                            'valid_until': new_state_mem['valid_from'],
                            'superseded_by': new_state_mem['id']},
            audit_row={'action': 'state_supersede', 'target_id': old_id,
                       'decision_reason': f'new state {new_state_mem["id"]}',
                       'state_before': json.dumps({'status':'active'}),
                       'state_after': json.dumps({'status':'superseded'}),
                       'source_ai': source_ai, 'auto_executed': 1},
            expected_status='active',
            expected_updated_at=old_updated_at,  # drift gate
        )
        return old_id
    except database.MaintenanceDrift:
        return None
```

**Daemon archive**：新增 step `archive_stale_states`，扫 `info_type='state' AND last_confirmed_at < now - 3*ttl` → 走 `commit_maintenance_atomic` 标 `status='archived'`。

**State TTL 配置**：
```python
STATE_TTL_DAYS = {'mood': 3, 'health': 14, 'work_status': 14, 'energy': 3, 'default': 7}
```

### D-2b：Current Status daemon prompt 加时间约束（v1 保留）

`current_status.py`：memory 先走 `apply_temporal_annotation()`；prompt 加"看到日期或 X 天前前缀禁止写'近期/当前/正在'"约束段。

### D-6：Context Isolation（v2 用 context_kind）

```python
def validate_context_isolation(mem):
    kind = (mem.get('context_kind') or '').strip().lower()
    if not kind or kind not in CONTEXT_ALLOWED_ROOMS:
        return mem
    allowed = CONTEXT_ALLOWED_ROOMS[kind]
    room = (mem.get('room') or '').strip()
    if room in allowed:
        return mem
    if kind in ('roleplay', 'joke') and room in CANONICAL_ROOMS:
        if _is_strict_mode():
            raise ValueError(f"context_isolation: {kind} → {room!r} rejected")
        logger.warning(f"context_isolation SOFT WARN: {kind} → {room}")
    original_room = room
    mem['room'] = CONTEXT_PRIMARY_ROOM[kind]  # v2 M2 确定性
    tags = _parse_tags(mem.get('tags'))  # v2 M6 dict/坏 JSON 处理
    tags.append(f'_redirected_from_{kind}_{original_room or "empty"}')
    mem['tags'] = json.dumps(tags, ensure_ascii=False)
    return mem


def _is_strict_mode() -> bool:
    """v2 M3: env 值 exact '1' 才 strict；其他 → soft。"""
    return os.environ.get('MEMORY_HUB_CONTEXT_ISOLATION_STRICT', '').strip() == '1'
```

### D-4：`scripts/data_health_backfill.py` v2（plan/execute）

架构对齐 `dedup_legacy.py`：`--plan` / `--execute --plan-file` / `--db-path` / `--check` / `--max-fixes`。

PLAN 阶段快照 `updated_at`；EXECUTE 阶段通过 `commit_maintenance_atomic(expected_updated_at=snapshot)` drift gate。

Prefix 修复归入 `report_only` 段（v2 H1 降级）。

### D-5：接入 6 处 CREATE 点

见 Q2 表。daemon 3 处加 `try/except ValueError` skip 单条不 crash step。

### D-2c：`apply_temporal_annotation()` 接入 4 入口（v2 H6）

`smart_context.get_smart_context()` / `corridor.build_corridor()` 每板块 / `gateway.build_context()` / `memory_ops.dream_recall()`。

---

## Q4｜分步 + 验收

| Step | 工作 | 验收 | 估时 |
|---|---|---|---|
| **0（v2.1 新增）** | `_WRITE_LOCK` 迁 database.py + `commit_*` 内部持锁 + 3 条并发测试 | backfill+finalize / backfill+touch / nested guard 都通过 | **1 d** |
| 1 | `context_kind` migration + extractor/proposal/remember/pending/finalize/daemon 全链路 | context_kind 端到端持续，`test_context_kind_persists_from_extract_to_recall` 通 | 1 d |
| 2 | `memory_validation.py` D-1 + D-6 + `annotate_event` + `is_ai_soliloquy_structured` v2.1 + 30+ 单元测试 | v2.1 反例测试全过 | 1.5 d |
| 3 | `state_ttl.py` + 5 列 migration + `supersede_state_atomic`（走 Step 0 helper）+ 15 单元测试 | 隔离键测试全过 + 并发 supersede 测试通 | 1.5 d |
| 4 | `apply_temporal_annotation()` 接入 4 入口 + 快照测试 4 条 | 4 入口各断言 | 1 d |
| 5 | `current_status.py` prompt 加时间约束 | mock LLM 收到含约束段 prompt | 0.5 d |
| 6 | 6 处 CREATE 点接入 validation + daemon archive_stale_states step | grep 确认 6 处 + daemon 集成测试 | 1 d |
| 7 | `scripts/data_health_backfill.py` v2 plan/execute + 本地冒烟 | 构造脏数据 → plan 报告 + execute drift gate 生效 | 1 d |
| 8 | VPS backfill plan → Ceci 审 → execute | audit 每条 + rebuild_all_corridors | 0.5 d |

**v2.1 总估时：9 天**（v2 是 8，v2.1 +1 天 Step 0）

---

## v2.1 单元测试清单（60 条）

新增 Step 0 并发测试（3 条）：
1. `test_step0_backfill_and_finalize_concurrent_no_deadlock`
2. `test_step0_backfill_and_touch_concurrent_serialized`
3. `test_step0_nested_acquire_deadlock_guard`

独白判定 v2.1（阻塞 B 4 条）：
4. `test_soliloquy_ai_about_user_not_flagged` — **reviewer 原反例**：`subject_id=user + source_actor_id=cloudy + source_ai=cloudy` → **False**
5. `test_soliloquy_source_actor_id_alone_insufficient`
6. `test_soliloquy_subject_ai_via_alias` — `subject_id=cloudy + source_ai=claude` → **True**
7. `test_soliloquy_subject_other_ai_not_flagged` — `subject_id=jasper + source_ai=claude` → **False**

其余（保留 v2 全部）：
- D-1 核心 10 条（clamp / normalize / soliloquy backfill）
- 独白 owner_ai 补齐 3 条
- D-6 context_kind isolation 8 条
- Event annotation 4 条
- State ttl 15 条（含隔离键 6 条 + supersede 并发 2 条）
- 时间注入 4 条
- Backfill script 6 条

**测试总计约 60 条**。全套目标 410+（Phase 1.7 基础 352 + PR1 v2.1 ~60）。

---

## Q5｜风险

### 高

1. **Step 0 迁移影响面**（v2.1 新增）  
   `_WRITE_LOCK` 迁移 + `commit_*` 契约变更 → 现有 `_touch_recalled_memories` / `_check_auto_resolve` 的外层 with 全部要拆。**如果拆漏一处**，就变成 caller 包锁 + helper 也拿锁 → nested deadlock（`threading.Lock` 非重入）。  
   **缓解**：（a）Step 0 独立提交 + Codex 复审 pass 后才继续。（b）新增 `test_step0_nested_acquire_deadlock_guard` — 模拟 caller 意外包锁调用 helper → 用 `wait_for(timeout=5)` 兜底断言必然超时（防未来 caller 忘 contract）。（c）codebase-wide grep `with _WRITE_LOCK` 确认全部拆完。

2. **独白判定过严导致 owner_ai 补齐率下降**（v2.1）  
   subject_id 空的记忆 → 保守视为非独白 → 不补 owner_ai。存量数据里很多 subject_id 空，可能补不到多少。  
   **缓解**：backfill `--check owner_ai` 里用**更完整的规则**（不只 soliloquy 判定，也用房间归属+source_ai）扫存量。存量修在 backfill 侧，生成器只做保守 gate。

3. **`context_kind` 全链路串**（v2 高保留）  
   Extractor 到 daemon 6 层要一致。漏一层就默认空字符串走 pass。  
   **缓解**：Step 1 加端到端测试；grep `context_kind` 覆盖率检查。

### 中

4. **State supersede 隔离键太严** — 存量 state 大多缺 subject_id → skip supersede → 老 state 永不 supersede。  
   **缓解**：daemon 生成 state 侧兜底 subject_id（用 daemon:{category} fallback key）；backfill 生成 subject_id。

5. **backfill drift gate 全 skip** — plan 生成后 activation touch 改 updated_at → execute 全 skip。  
   **缓解**：Ceci 授权模式加 `--ignore-drift-if-only-touch` 参数（如果 status 未变只是 updated_at 变了，仍执行）。或 Ceci 审时决定重跑 plan。

### 低

6. **event_date 与 created_at 都缺** — annotate 返回原 mem。合理行为。加 test。

7. **strict env variable 值** — 明文档 "MUST be exactly '1'"。测试覆盖 'true'/'True' 返回 False。

---

## 附：文件改动预览 v2.1

```
新增：
  memory_validation.py             (~260 行)
  state_ttl.py                     (~220 行，走 commit_maintenance_atomic)
  scripts/data_health_backfill.py  (~400 行)
  tests/test_memory_validation.py  (~500 行，34 条含 v2.1 独白反例)
  tests/test_state_ttl.py          (~280 行，15 条)
  tests/test_temporal_annotation.py (~150 行，4 条)
  tests/test_backfill_script_v2.py (~180 行，6 条)
  tests/test_write_lock_step0.py   (~150 行，3 条 v2.1 Step 0)

改动：
  database.py       (+_WRITE_LOCK 从 memory_ops 迁入 + commit_* 内部持锁
                     + 5 列 migration + _ALL_COLUMNS + _preserve_on_empty)
  memory_ops.py     (removed _WRITE_LOCK 定义改为 re-export 
                     + 6 处 CREATE 加 validate + supersede_state_atomic 集成)
  corridor.py       (板块 items apply_temporal_annotation)
  smart_context.py  (get_smart_context 输出前 annotation)
  gateway.py        (build_context 输出前)
  mcp_server.py     (smart_context tool)
  current_status.py (prompt 加时间约束段)
  daemon.py         (3 处 CREATE + archive_stale_states step)
  conversation_capture.py (extractor prompt 加 context_kind)
  async_remember.py (finalize 传递 context_kind)
```

约 17 个文件、+2700 行 additive、60 条新测试。

---

## 交付流程 v2.1

1. **本方案 v2.1 推分支** `phase20/pr1-plan-doc`（本 commit 只推 md，方便 reviewer / Codex 看）
2. Ceci + Lucien + Codex 都审 v2.1
3. 都过 → 开工分支 `phase20/pr1-data-health`
4. **Step 0 独立提交 + Codex 复审 pass 后**才继续 D-1/D-2 等
5. 全套测试 410+ 通过
6. 开 PR → Codex 复审 2 轮
7. 合并 + VPS 部署
8. VPS backfill plan → Ceci 审 → execute
9. Ceci 观察 1 周体感
10. 稳定后开 PR2
