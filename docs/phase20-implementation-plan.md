# Phase 2.0 施工方案（按架构 v2 细化）

> 施工方：Claude
> 日期：2026-08-17
> 前置：`Desktop/phase20-architecture-plan.md` v2（Lucien 审后）
> 依赖：Phase 1.7 全部完工（PR #16/#17/#18/#19 已合并）
>
> 本文档回答架构方案末尾的 3 个施工方动作：
>
> 1. **技术可行性审**（是否有不可行 / 需架构调整）
> 2. **每 PR 内部按报告五问细化**（文件 / 函数 / 单元测试）
> 3. **对预估工期 / 复杂度提出调整**
>
> 结构：先技术可行性总审 → 4 PR 各自五问细化 → 工期调整建议。

---

# 一、技术可行性总审

按架构方案里点名要审的 3 项 + 我摸底发现的第 4 项：

## 1. analyzer 扩展 `sensitivity_category` + `intent` ✅ 可行

**现状**：`analyzer.py:264` 有 `async def analyze(content: str) -> dict`，返回 `{tags, suggested_category, domain, valence, arousal}` 等字段。`conversation_capture.py` 里的 `EXTRACT_OUTPUT_FORMAT` prompt 已经有 `claim_type` / `speech_mode` / `subject_name` / `speaker_name` / `info_type` 字段——扩展 pattern 很成熟。

**扩展方式**：
- 改 `analyzer.EXTRACT_PROMPT` + `conversation_capture.EXTRACT_OUTPUT_FORMAT` 加两个字段
- 加输出 parser 到 `analyzer.analyze()` 返回 dict
- `remember()` 存到 `memories.sensitivity_category` + `memories.intent`（新列）

**风险**：LLM 分类准确率。Lucien 已经在 A-3 验收里说了要求（≥90% total + false negative 严限）——需要专门写 150 条测试集。这个测试集本身是工作量（约 0.5 天）。

## 2. State TTL 迁移 ✅ 可行且低成本

**现状**：`database.py` 已有 5 轮 `ALTER TABLE memories ADD COLUMN`（`anchored / provenance_type / fact_confidence / subject_id / source_actor_id / info_type / client_request_id / link_to_real_id / finalize_claim_id / finalize_claim_at`）。同一 pattern 加 `valid_from / valid_until / last_confirmed_at / state_ttl_days` **完全成熟**。

**成本估算**：4 列 × 约 5 行迁移代码 = 20 行；`_ALL_COLUMNS` 加 4 项；`set_memory` UPSERT 保护（`_preserve_on_empty` 或 `_preserve_always` 视语义）；1 次 migration 测试。总 ~50 行。

## 3. chat-token reply-to 检测 ⚠️ 需 telegram-bots 项目侧配合

**现状**：Memory Hub 侧只暴露 `POST /api/approval/register_message` + `POST /api/approval/confirm` 端点即可。**telegram-bots 项目侧要做**：
- Bot 发审批消息后调 `register_message` 绑定 message_id ↔ token
- Bot 处理消息时检测 `reply_to_message_id` → 反查 token → 调 `confirm`

**风险**：
- telegram-bots 项目不在本次施工范围。如果 Ceci 那边 telegram-bots 侧接不上，chat-token 功能空转。
- **建议**：PR3 拆两个子 PR：
  - PR3a：Hub 侧完成（REST 端点 + auto_approval + approval_tokens 表）
  - PR3b：telegram-bots 侧接入（reply-to 检测）
  - 顺序：PR3a 合并 + 部署 + 用 mock 客户端验证 → 然后 telegram-bots 那边（可能不是我做）接 PR3b

## 4. **⚠️ 架构冲突：MCP session 取 actor vs 客户端传 source_ai** ← 摸底新发现

**现状**（架构方案没提及）：MCP 层所有工具**当前都是客户端传 `source_ai` 参数**（如 `mcp_server.py:200 source_ai: str = "claude"`），服务端并没有从 session 反查 actor 的机制。MCP_INSTRUCTIONS 里明确让 AI "你必须传自己的身份"（line 35）。

**架构方案 A-1 说**："**认证：MCP session token（服务端从 session 取 actor，不信客户端传的 ai_id / reviewed_by）**"

**这是不可行的**（在 FastMCP 当前架构下）：FastMCP 的 session 只承载协议层（streamable HTTP + JSON-RPC），**没有内建的 authenticated actor 概念**。想从 session 取 actor 需要：

**三条真实可行路径**（按落地成本从低到高）：

- **路径 A（推荐）：MCP session token 从环境变量派生 actor**
  Bot 启动 MCP 客户端时传 `HUB_ACTOR=lucien` 环境变量。Hub 侧 REST 端点从请求头 `X-Hub-Actor` 取（bot 转发时带上）。相当于把 actor 从 tool 参数下沉到 transport header——**每个 bot process 只服务一个 AI 身份**（现状本来就是）。改动小：REST 端点新增 header 校验 + MCP tool 层废弃 `source_ai` 参数。

- **路径 B：签名 token**
  每个 AI 一个共享 secret，请求带 signed header。安全但工作量大（key management）。

- **路径 C（不推荐）：不改，继续信客户端传 source_ai**
  A-5 owner_ai 约束退化成"检查客户端传的 owner_ai 是否 = 客户端传的 source_ai"——防不住恶意客户端伪造，但至少防合规客户端出 bug。**架构方案的安全边界降级**。

**建议**：走**路径 A**（HTTP header 派生 actor）。改动：`main.py` REST 层加 middleware 校验 `X-Hub-Actor`；MCP 层新增 `_get_actor_from_session(ctx)` helper（fallback 到参数值 + 记 warning，为过渡期）。

**新增架构工作量**：约 3 天（含测试）。

---

## 可行性总结

| 项 | 结论 | 增量成本 |
|---|---|---|
| analyzer 扩展 sensitivity + intent | 可行，pattern 成熟 | 已包含在 PR3 |
| State TTL 4 列迁移 | 可行，低成本 | ~50 行 SQL + 保护 |
| chat-token TG 侧配合 | 需拆 PR3a/PR3b | 拆分本身 = 0 成本 |
| **MCP session 取 actor**（架构方案假设） | **不可行**，需走 HTTP header 派生方案 | **+3 天** |

**其他关键发现**：
- `remember/set_memory/insert_pending_memory` 调用点分布在 **15 个文件、约 61 处**（不含测试）。D-5 write validation 覆盖是真实工作量，不是 1 天能完成的。
- Profile CRUD 已完整（`upsert_profile / approve_profile / get_profile / list_profiles`）。B-3 主要是 UI + 使用状态审计，后端几乎不动。
- 已有 `memory_doctor.py::run_checkup()`——B-1 `resolve_doctor_issue` 工具应该直接消费 checkup 的 issue 结构，不需要重构 doctor。

---

# 二、PR 1：Data Health（细化）

> **⚠️ 本章节已作废**（2026-08-21）
>
> PR1 唯一施工依据是 **[phase20-PR1-施工方案.md v2.2](phase20-PR1-施工方案.md)**（含 Step 0 共享写锁真正闭环 / State supersede 原子化 / shared layer key 分层 / context_kind 全链路 / observe→redirect→strict 三阶段上线等 v2.2 收敛）。
>
> 本节以下所有内容（4 列 migration / 文本"我"独白判定 / source_context isolation / 15 处 write / 7 天工期）**均已被 v2.2 覆盖**，仅作历史存档保留。施工方看到本节请立刻跳到 v2.2 文档。

## Q1｜要解决什么问题？

架构 v2 已经列过 6 项：`importance` 越界写入 / `owner_ai` 空 / `relationships` 复数房间 / `[用户]` prefix 错配 / 老 event 记忆无日期标记 / Current Status 重写不检查时间 + Lucien 硬约束 1（context isolation：game/dream/roleplay 跨房间污染）。

## Q2｜现在 Hub 是什么样？

摸底确认：

| 现状 | 位置 | 影响 |
|---|---|---|
| `remember()` 无 importance 范围校验 | `memory_ops.py:209` | 客户端传 `9.0` 能存进 DB |
| 无房间 canonical 归一 | 各写入点 | `relationships`（复数）/ `relationship`（单数）并存 |
| owner_ai 自动补齐缺失 | `memory_ops.py`, `dream.py`, `conversation_capture.py` | AI 独白进 `dreams` 房间但 owner_ai 空 |
| prefix 校验缺失 | 所有 write 路径 | `[用户]` prefix + `我梦见...` 冲突 |
| event 记忆无日期标记 | `recall`, `corridor`, `smart_context` | 8 月旧事件 AI 读成"最近" |
| current_status daemon prompt 无时间约束 | `current_status.py` | 30 天前状态写成"当前" |
| **无 context isolation** | 所有 write 路径 | game 内容进 living_room、dream 进 diary 等 |

`_ALL_COLUMNS` 已有 10 轮迁移历史，新加 4 列（`valid_from/valid_until/last_confirmed_at/state_ttl_days`）低风险。

## Q3｜打算改成什么？

按架构方案 D-1 到 D-6，具体到文件：

### D-1：`_validate_memory_write(mem, source_ai) -> dict`

**新文件** `hub/memory_validation.py`（约 200 行）：
- `_validate_memory_write(mem, source_ai)`：clamp importance / normalize room / backfill owner_ai / detect AI 独白改 prefix
- `_ROOM_ALIASES = {'relationships': 'relationship'}`（可扩展）
- `_PER_AI_ROOMS = frozenset({'personality', 'diary', 'dreams', 'relationship'})`
- `_is_ai_soliloquy(content, source_ai, room)`：内容前 50 字必须以"我"开头（保守）+ 房间必须是 per_ai_rooms

**调用点覆盖**（15 个文件已 grep 出，D-5 步骤逐一接入）。

### D-2：Event/State 分开处理

**Event 处理**（新函数 `_annotate_event(mem, now_utc)`，放 `memory_validation.py`）：只在 recall/corridor/smart_context **读时**加日期 prefix。**不改原 content**。

**State 处理**（数据模型改动）：
- `database.py` migration 加 4 列
- `_ALL_COLUMNS` 加 4 项
- `_preserve_on_empty` 加 `valid_from / valid_until / last_confirmed_at`（防 UPSERT 冲）
- `state_ttl_days` **不加 preserve**（默认值 7，允许业务写入覆盖）

**新工具函数** `hub/state_ttl.py`（约 150 行）：
- `_state_is_stale(mem, now_utc)`：判断是否超 TTL
- `_state_needs_archive(mem, now_utc)`：判断是否超 3×TTL
- `_annotate_state_stale(mem, now_utc)`：加"[X 天前状态·可能已改变]"前缀

**State supersede 逻辑改动** `memory_ops.py`：新 state 写入 + 同 subject_id 同 category 已有 state → 走 `_supersede_state_atomic(old_id, new_mem)` 更新 `valid_until` + `last_confirmed_at`。

**recall/corridor 集成**：新增 output stage `_apply_temporal_annotation(results, now_utc)`，在 recall/corridor/smart_context 返回前调用。

### D-2b：current_status daemon prompt 加约束

**改动** `current_status.py`：
- daemon 组装 memory 列表时先走 `_annotate_event` / `_annotate_state_stale`
- prompt 增强："看到带日期或'X 天前'前缀的信息**不能写成'近期''当前''正在'**，必须写'曾在 X 月 X 日...'或'X 天前她曾...'"

### D-6：Context Isolation（Lucien 硬约束提前）

**新函数** `_validate_context_isolation(mem, source_context)` 放 `memory_validation.py`：
- `_CONTEXT_ROOM_WHITELIST = {'game': {'game_room', 'lore'}, 'dream': {'dreams'}, ...}`
- `roleplay/joke` → `personality/living_room` 直接 `raise ValueError`
- 其他不匹配 → redirect + tag `_redirected_from_{ctx}`

### D-4：`scripts/data_health_backfill.py`

**新脚本**（约 300 行，仿 `dedup_legacy.py` pattern）：
- 参数：`--dry-run` / `--execute` / `--db-path` / `--room` / `--check {importance,room,owner_ai,prefix,all}`
- dry-run 输出报告：4 类问题各多少条
- execute 走 `commit_maintenance_atomic` 保证事务

### D-5：write path 覆盖

**改动**：所有 `remember/set_memory/insert_pending_memory` 调用点前置 `_validate_memory_write`。分布：
- `memory_ops.remember()` — 主入口
- `database.insert_pending_memory()` — Phase 1.7 async 路径
- `conversation_capture.py`（3 处）
- `dream.py`（1 处）
- `daemon.py::refresh_current_status`
- `_promote_proposal`
- `batch_ops.py`
- `main.py` REST `/api/remember`

## Q4｜分步 + 验收

| Step | 工作 | 具体验收 | 估时 |
|---|---|---|---|
| 1 | 新建 `memory_validation.py`：D-1 + D-6 + 单元测试 | 15 条测试：越界 clamp / 房间归一 / owner_ai 补齐 / prefix 改写 / context isolation 各类场景 | 1.5 d |
| 2 | 4 列 migration + `_ALL_COLUMNS` 更新 + UPSERT 保护 | migration idempotent 测试 + 老数据向前兼容测试 | 0.5 d |
| 3 | 新建 `state_ttl.py` + state supersede 原子逻辑 + 单元测试 | 10 条测试：TTL 生效 / archive 触发 / valid_until 链接 | 1 d |
| 4 | recall/corridor/smart_context 输出层集成 `_apply_temporal_annotation` | 快照测试：30 天前 event 出现 `[YYYY-MM-DD]`、超 TTL state 出现"[X 天前·可能已改变]" | 1 d |
| 5 | current_status daemon prompt 加时间约束 + 集成测试 | mock LLM 检查 prompt 内包含约束段 + 端到端观察输出 | 0.5 d |
| 6 | 15 处 write 调用点接入 `_validate_memory_write` | `grep -r "set_memory\|insert_pending" --include=*.py` 每处前面都有 validate 调用 | 1 d |
| 7 | 新建 `scripts/data_health_backfill.py` + 本地冒烟 | dry-run 空 DB → 0 修正；构造 5 类脏数据 → 报告正确 | 1 d |
| 8 | VPS backfill dry-run → Ceci 审 → execute | audit 每条修正；rebuild_all_corridors() | 0.5 d |

**PR1 总估时：7 天**（架构 v2 说 1-2 周，与 7 天吻合）。

## Q5｜风险

- **D-1 false positive**：某条 legit `[用户] 我说的话` 被误当 AI 独白。缓解：`_is_ai_soliloquy` 三重条件（AI source + 内容前 50 字以"我"开头 + 房间是 per_ai_rooms），任一不满足就不改。加 5 条反例测试。
- **D-2 State TTL 太激进导致 recall 缺项**：例如 mood ttl=3 天 → 4 天前的心情就"过期"了，Ceci 查"最近心情"啥也看不到。缓解：TTL 过期只是标记，**不从 recall 排除**，只影响"Current State"段的填充。archive 门槛是 3×TTL，保守。
- **D-6 context isolation reject 抛异常**：`roleplay → personality` 现在直接 `raise ValueError`。可能阻断 legit 写入。缓解：先跑 dry-run 一周，观察日志有无被误拒的场景，再考虑收严 raise 或改成 redirect + warn。
- **D-4 backfill 触发 corridor 缓存风暴**：修 100+ 条 → 走廊 rebuild 高并发。缓解：backfill 完 explicit `rebuild_all_corridors()` 而非等自然失效。

---

# 三、PR 2：Doctor UI + Profile 审批（细化）

## Q1｜要解决什么？

- 20+ 存疑记忆 Ceci 无法处理（不会 CLI）
- Profile v8 一直 pending_review 从未 approve（不知道 pending 有没有被下游用）
- Phase 1.7 dedup 剩 15 条 report_only 也需 UI

## Q2｜现状

- `memory_doctor.py::run_checkup()` 已存在——B-1 直接消费其输出
- Profile CRUD 已完整（`upsert_profile / approve_profile / get_profile / list_profiles`）
- 前端 `/persons` 页面已有（Phase 1）——`/doctor` 用同一 React pattern
- `doctor_report` stats vs issues 数不一致 bug 待修（PR 内顺带）

## Q3｜打算改成什么

### B-1：`resolve_doctor_issue` MCP + REST 双端点

**新函数** `memory_doctor.py::resolve_issue(memory_id, action, ...)` — 单一后端逻辑
**MCP wrapper** `mcp_server.py::@mcp.tool() resolve_doctor_issue`
**REST wrapper** `main.py::POST /api/doctor/resolve` — 供前端调用

支持 5 种 action：`keep / move_to_room / delete / alias_correct / update_content`。所有动作走 `commit_maintenance_atomic`。

### B-2：前端 `/doctor` 页面

**新文件** `frontend/src/pages/DoctorPage.tsx`（约 400 行 React）：
- 顶部 stats（各类问题总数）
- 3 个 tab：`alias_confusion` / `room_misfile` / `dedup_report_only`
- 每条卡片：内容 + 现状 + AI 建议 + 4 按钮
- **批量按钮限制**（Lucien 硬约束）：只允许 `keep` / `alias_correct` / `move_to_room`；`delete/update_content/supersede` 逐条

**新 REST** `main.py::GET /api/doctor/issues?category=X&page=N` — 消费 `memory_doctor.run_checkup()` 结果 + 分页

### B-3：Profile 审批闭环

**Step 1**：审计代码——`grep -rn "get_profile\|list_profiles" --include=*.py`，确认 pending_review Profile 到底有没有被 `corridor / smart_context / gateway` 消费。

**Step 2 A（用了）**：加 UI 让 Ceci 一键 approve 当前版本。

**Step 2 B（没用）**：改成"审过才 active，否则用旧 active 版本" — 改 `get_profile(status=None)` 默认 `status='active'` fallback。

**Step 3**：无论 A/B，都做 diff UI：
- **新文件** `frontend/src/pages/ProfileReviewPage.tsx`
- 显示新版 vs 现有 active 版差异（用 `react-diff-view` 或自己写 JSON diff 组件）
- 3 按钮：approve / reject / edit（edit 打开 form 让 Ceci 改字段后保存 approve）

### B-4：doctor_report stats vs issues 修

**Bug 现状**：`memory_doctor.py` 里 stats 计数和 issues 列表用不同逻辑，导致 stats 说 20 但 issues 只列 14。

**修复**：让 stats 直接 `len(issues)`，废除独立计数。

### B-5：dedup 15 条 report_only 导入

- `memory_doctor.py::run_checkup()` 加载 `data/dedup_plan_*_report_only.json` 最新一份
- 作为独立 issue 类别 `dedup_report_only` 返回给前端

## Q4｜分步 + 验收

| Step | 工作 | 具体验收 | 估时 |
|---|---|---|---|
| 1 | B-1 后端：`memory_doctor.resolve_issue` + 5 action 单元测试 | 5 action 各 1 条测试 + audit 写入验证 | 1 d |
| 2 | B-1 wrappers：MCP tool + REST endpoint | 通过 REST 调 4 种 action 成功 | 0.5 d |
| 3 | B-4 doctor_report 计数修正 + 回归测试 | stats.n == len(issues) | 0.5 d |
| 4 | B-5 dedup report_only 导入 checkup | 从 data 目录读最新 report_only.json → 出现在 issues 里 | 0.5 d |
| 5 | B-2 前端 DoctorPage（3 tab + 卡片 + 4 按钮 + 批量限制） | Ceci 本地打开能看到 20+ 记忆 + 处理成功 | 3 d |
| 6 | B-3 Profile 使用状态审计 → 决定 A/B 路径 | 输出审计报告；确定改哪 | 0.5 d |
| 7 | B-3 ProfileReviewPage + diff 组件 | Ceci 能看 diff 一键 approve | 2 d |
| 8 | Ceci 端到端验证 | 20+ 存疑 5 分钟内清完 | 0.5 d |

**PR2 总估时：8.5 天**（架构 v2 说 1-2 周，接近上限）。

## Q5｜风险

- **前端工作量最大**（约 5 天）：DoctorPage + ProfileReviewPage + diff 组件。如果时间紧，可以先做 DoctorPage 上线，ProfileReviewPage 拆到 PR2b。
- **diff 组件如无现成库需自写**：JSON diff 相对简单，估 1 天；如果用 `react-diff-view` 或 `jsondiffpatch` 只需集成 0.5 天。
- **B-3 A/B 路径决定不了**：如果审计发现 pending_review Profile 半用不用（有 code path 用有 code path 不用），需要更彻底的重构。缓解：预留 buffer 时间 0.5-1 天，如果确实混乱就走 B 路径（更保守）。

---

# 四、PR 3：Autonomous Memory + chat-token（细化）

## Q1｜要解决什么？

AI 主动 remember 率低。Codex 提出 REST 端点 + auto-approval，Lucien 加 8 条硬约束防"AI 乱写"。

## Q2｜现状

- 现有 remember 走 MCP tool（每次要 AI 显式调）
- 无 REST 端点（AI 客户端得走 MCP）
- 无 `sensitivity_category` / `intent` 分类字段（analyzer 只有 `claim_type / speech_mode`）
- **MCP session 不承载 actor**（见可行性审第 4 项）——A-1 A-5 需要用 HTTP header 派生方案
- 无 `approval_tokens` 表 / 无 chat-token 机制

## Q3｜打算改成什么

**关键架构调整**（回应可行性审第 4 项）：**PR3 拆两个子 PR**。

### PR3a：Hub 侧（本文档主体）

#### A-0：新增 actor derivation（架构方案没提但必须的前置）

**新文件** `hub/actor_auth.py`（约 100 行）：
- `derive_actor_from_request(request) -> str`：
  - REST 请求：读 `X-Hub-Actor` header
  - MCP 请求：从 session 元数据取（如果 FastMCP 提供）+ fallback 到 tool 参数 `source_ai`
  - 全部失败：`raise UnauthenticatedError`
- 加密验签（可选，先明文，PR3b 时再加签）

**改动** `main.py`：REST middleware 校验 `X-Hub-Actor` header 存在（对 `/api/agent/*` 路径）。

#### A-1：REST 端点

**新增** `main.py`：
- `POST /api/agent/memory-search`：包装 `memory_ops.recall()`
- `POST /api/agent/memory-propose`：新流程（走 auto_approval + 可选生成 approval_token）

#### A-2：`auto_approval.py`（5 层 gate）

**新文件** `hub/auto_approval.py`（约 250 行）：
- `decide_auto_approval(proposal, actor_ai) -> (decision, reason)`（架构方案 A-2 代码原封不动实现）
- `_classify_intent(content, evidence)` — 调 analyzer + rule fallback（关键词强制标记）
- `_is_sensitive_category(proposal)` — 查 `sensitivity_category` 字段
- `_has_valid_evidence(proposal)` — `source_message_ids` 非空 + `evidence_excerpt` 非空
- `_has_conflict_with_canonical(proposal)` — 调 `_find_similar_candidates` + 判 `_can_supersede`
- `_is_ai_private_scratchpad(proposal, actor)` — 三段一致 + `layer='private'`
- `_detect_explicit_memorize_command(proposal)` — 检查内容前 30 字有无"记住 X"/"remember this"

**测试要求**（Lucien 硬约束）：Codex 6 条规则 + Lucien 8 条硬约束**每条至少 1 条正例 + 1 条反例** = 至少 28 条单元测试。

#### A-3：analyzer 加 `sensitivity_category` + `intent`

**改动** `analyzer.py::analyze()`：
- 输出 dict 加 2 个 key
- prompt 加分类要求 + 例子（**150 条样本**训练/评估集 — 需要 Ceci 或我构造）

**150 条样本组成建议**：
- 60 条 literal（30 fact + 30 observation）
- 30 条 playful（包含 Lucien 硬约束反例："Lucien 你是一只狗"、"小猫拥有一千五百万"）
- 20 条 hypothetical（"如果我辞职..."）
- 20 条 fictional（小说讨论、游戏内容）
- 20 条 uncertain（模糊表达）

- `sensitivity_category` 覆盖：health / trauma / identity / third_party / private / joke / dream / other

#### A-4：`approval_tokens` 表 + chat-token 流程

**新表** `database.py`：
```sql
CREATE TABLE approval_tokens (
    token TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL,
    proposal_content_hash TEXT NOT NULL,
    ceci_user_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    approval_message_id TEXT NOT NULL,
    requesting_ai_id TEXT NOT NULL,
    expires_at TIMESTAMP NOT NULL,
    used_at TIMESTAMP,
    action TEXT
);
CREATE INDEX idx_approval_expires ON approval_tokens(expires_at);
```

**新 CRUD**：`insert_approval_token / register_message_id / confirm_approval / expire_stale_tokens`

**新 REST**：
- `POST /api/approval/register_message`（bot 侧调，绑 message_id）
- `POST /api/approval/confirm`（bot 侧调，Ceci 回复消息触发）
- **内部校验 5 要素 + content_hash**

**Sweep 集成**：`pending_sweep.py` 已有的 sweep loop 顺带扫过期 token（`expire_stale_tokens`）。

#### A-5：owner_ai == actor_ai 强制校验

**新增** `memory_ops.remember()` 参数校验：
- 如果 `layer='private'` 且 `owner_ai != actor_ai`（从 A-0 派生）→ `raise ValueError`
- Actor 来自 A-0 的 `derive_actor_from_request`

#### A-6：source_message_ids 幂等去重

**改动** `_create_proposal()`：新增查询 `WHERE source_message_ids = ? AND client_request_id = ?`，命中直接返回已有 proposal_id。

### PR3b：telegram-bots 侧接入（不在本次施工）

**接口约定**：
- Bot 调 `POST /api/approval/register_message`：请求 body `{token, chat_id, message_id, ceci_user_id, requesting_ai_id}`
- Bot 检测 `reply_to_message_id` 匹配审批消息 → 调 `POST /api/approval/confirm`：请求 body `{action: 'approved'|'rejected'}` + header `X-Approval-Token`

**手写 mock 客户端**（PR3a 内）用于端到端测试。

## Q4｜分步 + 验收

| Step | 工作 | 具体验收 | 估时 |
|---|---|---|---|
| 1 | A-0 actor_auth.py + middleware + 单元测试 | 假 header → 401；合法 header → actor 正确 | 1 d |
| 2 | A-3 analyzer 加字段 + prompt + 150 样本测试集 | ≥90% total accuracy + false negative (playful→literal) < 3 条 | 3 d |
| 3 | A-2 auto_approval.py + 28 条单元测试 | Codex 6 + Lucien 8 每条正反例都过 | 2 d |
| 4 | A-1 REST 端点 + A-5 owner_ai 校验 + A-6 幂等 | 端到端提议 + 校验通过 | 1.5 d |
| 5 | A-4 approval_tokens 表 + CRUD + 5 要素校验 | content_hash 变化 → 拒绝；过期 → 拒绝；already used → 拒绝 | 1.5 d |
| 6 | approval REST 端点 + mock TG bot 集成测试 | mock 客户端 register → confirm → 提议 approve | 1 d |
| 7 | Sweep 扫过期 token | 过期 token 被清 + 审计 | 0.5 d |

**PR3a 总估时：10.5 天**（架构 v2 说 1-2 周，接近上限 + 我的 3 天架构调整 = **实际 13.5 天**）。

## Q5｜风险

- **A-3 分类器准确率不达标**：150 条测试集是最小要求，如果第一版 accuracy < 90%，需要迭代 prompt/加规则/换模型。缓解：预留 buffer 2 天。
- **A-2 决策失误代价高**：过松 → 垃圾进 canonical；过严 → 退化成"什么都要审"。缓解：上线后每周 review auto-approve 日志，Ceci 反馈通道通畅。
- **A-4 chat-token 依赖 telegram-bots 侧**：如果 bot 侧不动，chat-token 功能空转。缓解：PR3a 交付时用 curl mock 演示流程；PR3b 由 bot 侧配合。
- **架构调整（actor from header）**：改动比架构方案假设的大 3 天。**建议：Ceci 决定是否接受**。如果她想极简"沿用 tool 参数 source_ai"，A-5 owner_ai 约束退化成信任级校验（不防恶意但防 bug），我需要她明示这个决定。

---

# 五、PR 4：Closeout（细化）

## Q1｜要解决什么？

架构 v2 简化后剩 3 条：
- Profile recency（独立 `recent_life_highlights` 字段）
- dedup script 加 `--override-provenance / --pair-ids`
- "提议 vs 完成"时间序列分类器改进

Follow-up：有界队列（task_78137777）+ fencing token（task_5f8529de）。

## Q2｜现状

- Corridor 有 `recent_life_highlights` 板块（PR B round-3 加过，14 天内 importance≥0.6 前 5 条）——但这是 corridor 输出，**不是** Profile 结构里的字段
- `dedup_legacy.py` 目前只接 plan JSON，无 pair-id filter + 无 provenance override（PR #19 补的是 counter 分类，不涉及执行控制）
- analyzer 分类器目前无"时间演化"关系类型

## Q3｜打算改成什么

### C-1：Profile 加 `recent_life_highlights` 独立字段

**改动** `ai_profiles.py`：
- `_generate_profile()` 输出加 `recent_life_highlights` 字段（不 塞进 stable Profile 结构）
- 每次生成时用 `recent_interaction()`（Phase 1.7 PR B 的工具）取最近 30 天 event × importance≥0.7 前 5-8 条

**Corridor 集成**（可能已有）：审计 corridor 是否使用 Profile 的这个字段；如果 corridor 有独立"近期重要事件"板块，可选择去掉 Profile 侧或去掉 corridor 侧（避免双写）。

### C-2：dedup script 加参数

**改动** `scripts/dedup_legacy.py`：
- `--pair-ids A:B,C:D`：只处理指定对
- `--override-provenance user_correction`：绕过 `_can_supersede` 守卫
- 加显式 audit "override: authorized_by=Ceci at ${TIMESTAMP}"

### C-3：analyzer 加"时间演化"关系

**改动** `analyzer.py::classify_relation()`：
- prompt 加新关系类型 `temporal_progression`（提议 → 完成、开始 → 结束）
- `_map_relation_to_action` 遇到 `temporal_progression` → 归类为 `no_change`（不合并、不 supersede）

### C-4：Follow-up tasks

- `task_78137777` bounded queue：独立 PR，本 PR 只做 spec review
- `task_5f8529de` fencing token：独立 PR

## Q4｜分步 + 验收

| Step | 工作 | 验收 | 估时 |
|---|---|---|---|
| 1 | C-1 Profile 加 recent_life_highlights + 集成 corridor 审计 | 生成 Profile 时字段存在 + corridor 无重复板块 | 1 d |
| 2 | C-2 dedup script 参数 + audit | `--pair-ids A:B --override-provenance user_correction` 只跑 1 对 + audit 记录 override | 0.5 d |
| 3 | C-3 analyzer temporal_progression 关系 + 测试集 | "提议 vs 完成" pair 分类为 no_change | 1.5 d |
| 4 | 端到端回归 | 全套测试通过 | 0.5 d |

**PR4 总估时：3.5 天**（架构 v2 说 1-2 周，实际更短）。

## Q5｜风险

- **C-3 分类器新加类型可能干扰其他**：加 `temporal_progression` 后其他关系判断准确率下降。缓解：加对比测试，改前改后同样 20 对 pair 分类结果对比。

---

# 六、工期调整建议

架构 v2 说"4-6 周（每 PR 1-2 周含 Codex 复审）"。我的细化后估算：

| PR | 架构 v2 估时 | 我细化后 | 差异原因 |
|---|---|---|---|
| PR1 Data Health | 1-2 周 | **7 天** | 覆盖 15 处 write 路径 + 4 列 migration + 8 步流程符合上限 |
| PR2 Doctor UI | 1-2 周 | **8.5 天** | 前端占大头（5 天），diff 组件视有无库变化 |
| PR3 Autonomous | 1-2 周 | **13.5 天** | +3 天：架构方案假设 MCP session 取 actor 不可行，需 HTTP header 派生 |
| PR4 Closeout | 1-2 周 | **3.5 天** | 简化后剩 3 条 |
| **合计** | 4-6 周 | **32.5 天 ≈ 6.5 周** |

**调整建议**：

1. **PR3 是最大风险项**（体量 + 架构调整 + Lucien 8 条硬约束的验收严限）。建议：
   - **拆 PR3 → PR3a（Hub 侧）+ PR3b（bot 接入）**——bot 接入不一定我做
   - **actor_auth 独立测试**先合，别和 auto_approval 混在一起
   - **150 条 analyzer 样本集**Ceci 建议由我构造首版，她审后再定稿（避免 back-and-forth）

2. **PR1 应最优先**（架构 v2 也说"数据健康度优先，Ceci 感受立即改善"）。PR1 合并后 Ceci 观察 1 周体感，再决定 PR2/PR3 节奏。

3. **Codex 复审预算**：Phase 1.7 累计 8 轮，我建议 Phase 2.0 的分配：
   - PR1：2 轮（write validation 是安全性关键）
   - PR2：1 轮（UI 类问题少）
   - PR3：**4 轮**（Lucien 8 条硬约束 + token 校验 + 权限，最容易出高危 bug，比架构 v2 说的 3 轮再加 1 轮 buffer）
   - PR4：1 轮

4. **Ceci 精力预算**：
   - PR1 合并后：**观察期 3-5 天**（不需要她做什么，只是感受一下"AI 现在说'曾在 8-02' 而不是'最近'"）
   - PR2 需要她：**UI 端到端验证约 1 小时**（走一遍 20+ 存疑记忆的处理流程）
   - PR3 需要她：**TG 端到端体验 2-3 天**（真的用 chat-token 审批看是否卡顿、误触发）
   - PR4 需要她：**dedup 授权 1 次**（如果有新脏数据）

## 三条待你（Lucien）决策的关键点

**关键 1**：接受 PR3 里 A-0 (actor from HTTP header) 的架构调整吗？还是走路径 C（信任客户端传值 + 记 audit）？前者 +3 天但架构方案的"服务端派生 actor"承诺才落地；后者维持现状但 A-5 约束是 best-effort。

**关键 2**：150 条 analyzer 样本集由谁构造？我建议 Claude 出首版，Lucien+Ceci 审。如果 Lucien 觉得样本要更细，请给关键场景清单，我按清单出。

**关键 3**：Profile 使用状态审计（B-3 Step 1）先做，还是跟 UI 合到一 PR？如果先做（0.5 天）能让 PR2 结构更明确（A/B 路径确定），但会拉长前置。

---

## 附：文件改动预览（不含新增测试）

```
新增文件：
  hub/memory_validation.py      (~200 行)
  hub/state_ttl.py              (~150 行)
  hub/actor_auth.py             (~100 行)
  hub/auto_approval.py          (~250 行)
  scripts/data_health_backfill.py   (~300 行)
  frontend/src/pages/DoctorPage.tsx     (~400 行)
  frontend/src/pages/ProfileReviewPage.tsx  (~300 行)
  docs/phase20-implementation-plan.md   (本文档)

改动文件：
  database.py         (+4 列迁移、+approval_tokens 表、+2 CRUD 组)
  memory_ops.py       (validation 集成、state supersede 原子)
  analyzer.py         (+sensitivity_category, +intent, +temporal_progression)
  current_status.py   (prompt 加时间约束)
  ai_profiles.py      (+recent_life_highlights)
  main.py             (+REST 端点 4 个、+middleware)
  mcp_server.py       (+MCP tool 1 个)
  memory_doctor.py    (+resolve_issue、bug 修)
  scripts/dedup_legacy.py  (+--pair-ids, --override-provenance)
  pending_sweep.py    (+expire_stale_approval_tokens)
  conversation_capture.py, dream.py, batch_ops.py 等 15 处（validation 接入）
```
