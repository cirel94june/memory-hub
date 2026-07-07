# Memory Hub 核心功能详解

## 记忆写入

- **remember()**：智能写入，自动打标（domain/valence/arousal/tags），自动检测旧记忆关系
  - 相似度 >= 0.85 → 合并（同房间内内容融合为一条，**跨房间不合并**）
  - 相似度 0.55-0.85 → 小模型判断关系（updates/contradicts/supplements）
  - updates/contradicts → 旧记忆标记 `superseded`，追加年轮注记
  - 相似度 < 0.55 → 新建
  - **force_create**：AI 可显式声明"这条必须独立"，跳过合并检测
  - **category 保留**：用户传的 category 不会被系统覆盖
  - 返回值统一带 `linked`/`superseded` 字段
- **grow()**：长文自动拆分成多条独立记忆
- **batch_remember()**：批量存储多条记忆
- **记忆原子化**：每条记忆 = 一个独立的原子事实（<=200字）
- **event_date**：区分"事件发生时间"和"记忆创建时间"
- **source_context**：自动保存对话原文前 1500 字，recall 时随记忆返回

### Gateway 省 token 注入策略

- `build_context()` 默认 `compact=True`：普通对话只注入少量高置信记忆，通常 3 条左右，单条裁短。
- 用户问“原话 / 当时 / 细节 / 具体 / 为什么 / 来源 / 上下文”等问题时自动进入 `detail_mode`，才附带短原文片段。
- `/api/gateway/context` 支持 `compact`、`max_memories`，并返回 `estimated_tokens`、`memory_count`、`detail_mode`，方便看这次记忆注入是否过胖。

### Ombre-style 衰减解释

- `memory_ops.explain_decay()` 会给每条记忆算出 `lane`：`protected` / `long_term` / `short_term` / `watch`。
- 诊断结果包含 `protections`（锚点、客厅、高重要度、常被召回、强情绪）和 `pressures`（低重要度、从未召回、快衰房间、自动捕获未召回、久远）。
- `/api/memory/{id}/detail` 和 `/api/memory/decay-scores` 会返回同一套解释字段，便于以后做衰减诊断台。
- 这不是硬拦截写入：可疑内容先进库，但低重要度、短期池、未召回的自动捕获会更自然地滑向归档线。
- `/app/observatory` 是 Ombre-style 观测台：集中展示后台整理状态、衰减分层统计、临近归档、短期池和观察中记忆。

## 记忆搜索

- **混合搜索 + RRF 融合**：
  - 向量路：embedding 余弦相似度（语义匹配）
  - 关键词路：BM25 关键词频率（精确词汇命中）
  - 精确路：query 完整出现在内容/标签中（最强信号）
  - Reciprocal Rank Fusion 合并三路排序
  - 每条结果带 **confidence**（high/medium/low/weak）
  - 每条结果带 **linked_memories**
- **search_by_tags()**：按标签精确搜索，支持 any/all 模式
- **时间敏感评分**：时间衰减权重 10%，embedding 60%，importance 15%，emotion 15%
- **unresolved 优先浮现**：待办/未完成的记忆优先浮出
- **时间涟漪**：召回一条记忆时，+-48h 内创建的记忆也轻微激活
- **touch 机制**：每次被召回，activation_count++ / last_activated 刷新

## 记忆注入（Gateway）

- **搜索 + 截断**：混合搜索 → RRF 排序 → top 5 → 每条压缩到 ≤400 字注入
- **时间标签**：每条注入的记忆带相对时间（"刚刚"/"3小时前"/"昨天"/"2周前"）
- **对话溯源**：记忆附带 source_context 预览（`↳ 当时聊的: ...`）
- **走廊（corridor）**：AI 醒来时读的第一份记忆快照，包含：
  - 客厅要点（用户是谁）
  - 重要人物/关系索引（共享 `relationships` 画像，避免混淆狗蛋/Lucien/小克/Jasper 等常见名字）
  - AI 和用户的关系记忆 + 自我认知
  - 最近日记
  - 跨窗口摘要（chat_digest）
  - 基建状态
  - Persona State（AI 当前情绪/精力）
  - 待办事项提醒

## 记忆提取（反脑补 + 防身份混淆）

提取模型：**deepseek-v4-flash**（via DeepSeek 官方 API）

提取规则——**忠实提取，禁止脑补**：
- 可以记用户亲口说的事实
- 可以记对话中能直接观察到的情绪/状态
- 可以记用户和 AI 之间有意义的互动事件
- **绝对不能**：把模糊对话总结成极端结论、角色扮演当真、编造没出现的信息

Prompt 设计：总长度 2079 字，importance 0.0~1.0，importance < 0.5 的记忆跳过不存储。

**about 字段（防 AI 身份混淆）**：
- 每条提取的记忆标注 `about`：`user` / `interaction` / `ai`
- 走廊编译时自动过滤 `[用户]` 记忆，防止 AI 把用户经历当成自己的

## 社交系统（进行中）

朋友圈、论坛、群聊的后端 API 已接入 `main.py`，前端页面可用；`social.py` 负责 posts/comments/groups/messages CRUD。

- 朋友圈/论坛：用户发帖或评论时，可以用 `@AI名` 呼唤指定 AI 回复，回复会进入社交记忆缓冲。
- 群聊：用户发消息后，群成员里的真实社交 AI 会按 @、被回复目标或随机围观回复；支持消息级回复、删除、`@` 指定成员，并进入 `private_group` 记忆缓冲。群成员和 @ 会统一 `claude/cloudy/小克`、大小写和中文标点，旧群不需要重建。AI 回复里的 `@` 只保留为文字，不自动触发第二轮。
- 模型调用：`_social_call_llm()` 会读取 AI 档案里的 per-AI 模型配置，注入角色人设和走廊记忆。
- 小克身份：`cloudy` 和 `claude` 是同一身份；合并档案时优先读 `cloudy` 的小克人设，再用 `claude` 补空字段。

待增强：AI 自主发帖、群聊成员管理、流式回复、统一时间线、AionsHome 风格的 token 用量/气泡拆分/更完整群聊设置。

## 锚点系统（Anchor）

将重要记忆设为"锚点"——永不衰减、不参与随机浮出，但可搜索、走廊里单独显示。

- **anchor(memory_id)**：设为锚点（MCP 工具 + REST API）
- **release_anchor(memory_id)**：解除锚点
- **上限 20 条**：防止滥用，只有"坐标系级别"的记忆值得锚定
- 走廊里单独一节"【锚点·不变的事】"
- 前端记忆详情模态框里有锚定/取消按钮
- 锚点记忆在衰减流程中自动跳过

适合锚定的内容：用户核心价值观、关系定义、绝对不能忘的事实。

## 年轮评论

- **add_comment()**：给记忆追加反思/补充，不改原文
- 类型：reflection / update_note / feel / comment

## 对话自动捕获

- **capture_conversation()**：每轮对话自动缓存
- 按类型分别触发：私聊 30 轮 / 私群 40 轮 / 公群 80 轮
- **flush_capture()**：手动触发总结

## 自动整理（Daemon，每 12h）

1. 合并相似记忆
2. 压缩日记（日记 → 周记）
3. 工作事务归档（→ 职业生涯）
4. 客厅去重精炼
4.5. 客厅/人物画像刷新（核心资料进 `living_room`，人物关系进 `relationships`）
5. 心理感悟蒸馏（碎片 → 人生章节；条件触发）
6. 过时记忆检测
7. 刷新对话捕获缓冲区
8. 记忆衰减（遗忘曲线：`score = importance × max(act,1)^0.3 × e^(-λ×days) × emotion_weight`，λ=0.12）
9. Persona State 休息（恢复精力）
10. 梦境日记（条件触发，不是每天必定生成）
11. 重建所有 AI 的走廊

### 自动后台功能的真实触发条件

这些功能已经有代码，但目前缺少前端诊断，所以用户可能会感觉“好像没跑”：

| 功能 | 触发条件 | 写入结果 | 待补 |
|------|----------|----------|------|
| **人生章节** | `psychology` 房间里 30 天以上、未归档、非 `life_chapter` 的记忆；总数至少 3 条，且同月至少 2 条；LLM 成功 | 新增 `room=psychology`、`category=life_chapter`、`importance=0.8` 的章节记忆，并归档原碎片 | 诊断 API/前端：显示材料不足、月份不足、LLM 失败等 skip 原因 |
| **夜间梦境日记** | 按 Asia/Shanghai 当天优先使用同一 AI 的 `chat_digests`；若摘要不足 2 条，则从最近 72 小时内仍 active、importance >= 0.5 的私聊/小群有效记忆补材料，至少 3 条才生成 | 新增 `layer=private`、`room=dreams`、`category=night_dream`、`source_platform=daemon_dream` 的梦境残响 | 梦境入口、skip log、Dream Context 浮现/注入开关 |
| **MCP dream 自省** | AI 主动调用 MCP `dream()` 工具 | 新增 `room=dreams` 的私有自省/梦境 | 和 daemon 夜梦统一房间后，还需要统一入口与浮现策略 |

下一步优先补“后台观测台”：显示最近一次 daemon 每一步的运行时间、产出数量、跳过原因和错误摘要。这样小模型有没有在总结人生章节、有没有做梦、为什么没产出，都能一眼看到。

Telegram / Render bot 如果只调用 `/api/capture/log`，也会顺手写入 `chat_digests`。因此 Telegram 私聊、小群只要有足够有效材料，就能进入梦境材料池；人生章节仍然主要依赖被提取到 `psychology` / `relationship` / `personality` 等长期房间、并且一段时间后仍未归档的材料。

`/api/daemon/status` 会返回最近一次后台整理报告，并写入 `data/daemon_status.json`。设置页会展示最近步骤、耗时、结果和错误摘要，用来替代猜日志。

## Memory Control Panel

- Memory list supports filtering by room, source AI, and visibility layer (`shared` / `private`).
- Memory cards show room, visibility, owner AI, and source/related AI.
- Memory detail edit mode can update content, importance, room, category, tags, `layer`, `owner_ai`, and `source_ai`.
- `layer=shared` means common memory for all AI; `layer=private + owner_ai` means private memory for one AI.
- `/api/memory/list` now passes `source_ai` through to the backend query, so role-filtered views are real filters, not just visual routing.

Next: batch move memories across room/layer/owner, and let the small model suggest whether a memory should be common, private, or moved to another room.

## Memory Safety Kit (Planned)

The goal is not to create a full copy every day. It should be readable, testable, and recoverable:

- Daily lightweight safety report: database readable, memory counts, AI/room distribution, latest backup time, suspicious items.
- Daily incremental Markdown export for newly created or updated long-term memories.
- GitHub should be the cloud source of truth for exported data; Obsidian opens a cloned/pulled folder as a vault and does not require an Obsidian account.
- Markdown export should deduplicate by memory id / updated_at, updating the same file for the same memory instead of creating daily duplicates.
- Weekly/monthly compressed snapshot with SQLite, Markdown, config checklist, and restore instructions.
- Archived, low-importance, and disposable social-chat memories should be excluded from long-term Obsidian export by default, or placed in a short-retention log pack.
- Scheduled restore drill: copy the database to a temporary path, open it, count rows, sample memories, and write the result into the maintenance report.

## 记忆生命周期

```
用户和 AI 对话（前端 App / TG Bot / Claude.ai / RikkaHub / 任意客户端）
    |
    v
Gateway 模式（全自动）或 MCP 模式（AI 主动调工具）
    |
    v
记忆提取（deepseek-v4-flash，反脑补规则）
    |
    v
remember(quick=True) 轻量去重 + 原子化（每条 <=200字）
    |-- quick 模式：跳过 LLM 合并分析，仅 embedding 去重
    |-- 完整模式：高相似→合并 / 中相似+updates→supersede / 低相似→新建
    |
    v
recall() 时：混合搜索 → RRF融合 → confidence标注 → unresolved优先 → touch+涟漪
    |
    v
Gateway 注入：RRF排序取top5 → 走廊 + 相关记忆 → 注入 AI 上下文
    |
    v
Daemon 每 12h：合并/压缩/蒸馏/过时检测/衰减/归档 → 重建走廊
```

## 房间一览

### 共享房间（所有 AI 可读写）

| 房间 | 用途 | 特殊行为 |
|------|------|---------|
| living_room | 核心身份、当前状态 | 永远注入走廊，定期去重精炼 |
| career | 工作经历、职业规划 | 工作事务 7 天后压缩转入 |
| psychology | 心理状态、情绪模式 | 30 天后蒸馏为"人生章节" |
| health | 身体健康 | |
| learning | 学习目标、技能 | |
| relationships | 人际关系 | |
| preferences | 兴趣偏好 | |
| work_tasks | 工作事务 | 快速衰减，7天归档 |
| infra | 基建总览 | |
| infra_changelog | 基建更新日志 | |
| social | 社交动态 | ⚠️ 待重做 |

### 私有房间（per AI，用 owner_ai 隔离）

| 房间 | 用途 |
|------|------|
| diary | AI 的个人日记（7 天后压缩为周记） |
| dreams | 梦境/自省 |
| relationship | 和用户的关系 |
| personality | AI 的自我认知 |

### 隔离房间

| 房间 | 用途 |
|------|------|
| game_room | 游戏/角色扮演，不混入正经对话 |

### 观测台整合（2026-07-03）

- `/app/observatory` 已经合并为记忆系统的主要工作台：总览、时间线、记忆编辑三个入口放在同一页。
- 总览会直接列出被保护中的记忆，并显示保护原因，例如锚点、高重要度、经常被召回、强情绪或长期层。
- 时间线和记忆编辑复用原来的记忆页面能力，后续热力图、衰减诊断、召回 token 诊断、梦境/人生章节诊断都优先合并到观测台，避免记忆功能散在多个页面。

### Memory Safety Kit 轻量版（2026-07-03）

- 新增 `safety_export.py`，生成 GitHub/Obsidian 可读 Markdown 导出，不替代 SQLite 主数据库和 JSON 房间备份。
- 手动接口：`POST /api/export/obsidian?dry_run=false&force=false`；`dry_run=true` 只统计将导出的数量，不提交 GitHub。
- daemon 每 12 小时完整整理时会自动运行一次，位置在梦境生成之后、普通 JSON GitHub 备份之前。
- 导出路径固定在 `exports/obsidian/`：`memories/` 存长期记忆 Markdown，`reports/YYYY-MM-DD.md` 存每日安全报告，`manifest.json` 记录 memory id、文件路径、updated_at 和 checksum。
- 去重策略：同一 memory id 固定写同一个 Markdown 文件，只有内容校验变化或 `force=true` 时才重新写；低重要度、已归档、临时社交、工作临时事项默认不进长期 Markdown。
- Obsidian 不需要账号；用户在电脑上 clone/pull GitHub 仓库后，用 Obsidian 打开 `exports/obsidian` 文件夹即可阅读。

### 醒来预览 / 记忆注入审计（2026-07-03）

- 观测台新增“醒来预览”标签，可选择 AI 和入口（私聊、朋友圈/论坛、群聊、MCP），输入一句测试消息后查看 `gateway.build_context()` 实际会注入的走廊、跨窗口摘要和相关记忆。
- 设计目标：同一个 AI 在私聊和小群之间应该有连续感，私聊能知道群聊摘要，群聊也能知道这个 AI 最近私聊摘要；但 `layer=private + owner_ai` 的私有记忆仍只对同一 AI 可见，不会给其他 AI。
- `gateway.build_context()` 继续注入同一 AI 的 `chat_digest` 跨窗口摘要；这是“知道最近聊过什么”的轻量通道，不等于把完整私聊原文注入群聊。
- 为减少后台重复分析，`gateway.post_process()` 和 `conversation_capture` 在 LLM 已经提取出房间/重要度后，写入时使用 `auto_analyze=False`，避免每条记忆再调用一次 analyzer。

### 保护原因与重要度解释（2026-07-03）

- `memory_ops.explain_decay()` 现在返回 `lane_reason`、`protection_reasons`、`pressure_reasons`，前端不再只显示抽象标签。
- “保护中”只表示锚点或客厅这类硬保护；高重要度、常被召回、强情绪会解释为长期/保护因素，但不等同于用户手动锚定。
- 记忆列表里的星星改为“重要度≥80%”文字标签，避免误解成锚点或保护状态。
- 记忆详情关闭后会刷新列表，避免用户调低重要度后外层卡片仍显示旧的重要度标签。

### 走廊新鲜度与客厅画像刷新（2026-07-06）

- `corridor.get_corridor()` 支持 `force=True`，缓存窗口缩短为 5 分钟；观测/调试时可以强制重建，避免看到一小时前的旧走廊。
- MCP `get_corridor(source_ai=...)` 不再写死 Claude，Lucien/Jasper/小克各自能读自己的走廊；`pulse(..., force_corridor=True)` 可强制刷新。
- `/api/gateway/context` 返回 `requested_ai_id`、`ai_id`、`chat_id`、`chat_type`、`corridor_forced`，用于审计“AI 醒来实际读的是谁的记忆”。
- 编辑记忆后会异步重建全部走廊，避免过期/被修改的客厅画像继续残留。
- 新增 `POST /api/memory/living-room/refresh`：默认 `dry_run=true` 只生成画像更新建议；`dry_run=false` 才会把用户基本情况、重要人物画像、稳定状态变化写入 `living_room` 或 `relationships` 并重建走廊。`daemon` 每 12 小时维护时也会自动跑一次 `dry_run=false`，前台按钮用于临时补跑和人工确认。

### 观测台记忆库收敛（2026-07-06）

- `/app/observatory` 外层保留“总览 / 醒来预览 / 记忆库”三个入口；时间线和编辑不再重复拆成两个外层标签。
- “记忆库”复用 `MemoriesHubPage`，内部可在列表/编辑和时间线之间切换。
- 总览新增“客厅画像”面板：支持先生成画像更新建议，再确认写入；也可直接跳到客厅记忆列表手动编辑。

### 客厅/人物画像自动刷新（2026-07-06）

- 观测台里的“生成建议”只做 `dry_run=true`，让用户先看模型准备写什么；“写入建议”才会真的写入。
- 后台 `daemon` 每 12 小时会自动执行一次画像刷新：用户核心资料写入 `living_room`，常被提到的人、AI、昵称和关系边界写入 `relationships`。
- 走廊新增“重要人物/关系索引”，每个 AI/MCP/Gateway 醒来时都会看到共享人物画像的一小段摘要，减少把人名、AI 名和关系搞混。
- Gateway 不再额外重复追加“其他聊天窗口最近在聊”，跨窗口摘要统一来自走廊里的“你在其他聊天窗口最近聊了”。
### 群聊级摘要注入（2026-07-06）

- `chat_digest.get_recent_digests(ai_id=...)` 继续负责“同一个 AI 在其他窗口最近聊了什么”，注入走廊，帮助私聊/群聊连续。
- 新增 `chat_digest.get_recent_chat_activity(chat_id=..., exclude_ai_id=...)`，只在群聊、小群入口额外注入“这个群里其他 AI 最近在聊什么”。
- 这样群聊里的 Lucien/Jasper/小克能知道同一个群里其他 AI 的近期发言摘要，但私聊不会直接混入别的 AI 摘要，避免跨 AI 身份混淆。

### 醒来预览、待办与情绪修正（2026-07-06）

- 观测台醒来预览的“群聊”模式会读取最近有 `chat_digest` 的真实群聊 `chat_id`，因此能显示“这个群里其他AI最近在聊”的摘要；没有真实群聊摘要时仍显示 0 条。
- Gateway 每轮都会实时读取 unresolved 记忆，额外注入“当前待办/未完成”，不用等走廊重建；提示词会要求 AI 在相关时主动提醒、推进或询问是否完成。
- 客厅记忆不再解释为“永远不变”的硬保护：它仍优先注入走廊，但属于会被年轮、stale 检查和画像刷新持续更新的长期当前画像。真正不衰减的是锚点。
- 走廊显示锚点时会跳过和客厅完全重复的内容，减少“锚点/客厅”重复刷屏。
- Pulse 情绪打标改为看一轮用户+AI对话，而不是只看用户单句；Telegram `/api/capture/log` 也会把 AI 回复传给打标器。

### 夜梦机制修正（2026-07-06）

- daemon 夜梦已改为写入 `room=dreams/category=night_dream`，不再混在 `diary` 里。
- 当天材料按 Asia/Shanghai 日期边界计算，避免 VPS/UTC 日期造成“今天材料不新”。
- 去重会同时检查旧 `diary` 和新 `dreams` 的 dream 标签，防止迁移期同一天重复做梦。
- prompt 改为要求具体残留（人名、场景、情绪、话题），减少抽象日记式输出。

### 梦境诊断入口（2026-07-07）

- `dream.py` 每次运行都会写 `data/dream_status.json`，记录本地日期、UTC 日期范围、每个 AI 的 dreamed/skipped 状态、跳过原因和材料数量。
- 新增 `GET /api/dream/status` 和 `POST /api/dream/run`，观测台可以单独查看或触发梦境，不必等待 12 小时 daemon。
- 观测台总览新增“梦境诊断”卡片，显示最近梦境片段和每个 AI 为什么没梦。

### MCP 连接与降敏写入诊断（2026-07-07）
- MCP server identity 固定为 Memory Hub，新增版本号、public path、instructions hash、tool schema hash，启动日志会打印 hash，便于排查 ChatGPT 是否把同一服务当作新权限。
- 新增 MCP 工具 mcp_health / mcp_debug_log，以及后端接口 /api/mcp/health?include_audit=true。它们用于查看最近工具请求是否真的抵达 Memory Hub。
- 新增 safe_remember。remember、dream、batch_remember 也统一走安全写入包装：压缩长文本、失败后改成中性摘要重试一次、记录审计日志。
- batch_remember 改成逐条 fallback，返回每条写入结果和汇总的 created / merged / skipped / blocked / failed，避免整批记忆因为一条高敏内容全部失败。
- 若服务端审计没有 tool_reached，而 ChatGPT 显示安全拦截，应按平台侧拦截处理，不要误判成 Memory Hub 后端拒绝。

### MCP 工具列表真实注册表诊断（2026-07-07）
- /api/mcp/health、mcp_health 与 hub_info 现在使用 FastMCP list_tools 的真实注册表生成 tool_count 和 tool_schema_hash。
- hub_info 会返回 mcp_identity，因此即使 ChatGPT 端暂时看不到新加的 mcp_health / mcp_debug_log，也能通过旧工具确认服务端实际工具列表。
- 当前真实注册表应为 28 个工具，包含 safe_remember、mcp_health、mcp_debug_log；若 ChatGPT 只显示 25 个，优先按客户端旧 schema 缓存处理，断开并重新连接 MCP。

### 梦境展示与轻量 Dream Context（2026-07-07）
- dream.py 新增 get_recent_dreams_for_ai，用 canonical id + aliases 读取某个 AI 最近的私有梦境。
- gateway.build_context 和 smart_context 会注入最近 1 条“梦境残响”，让 AI 知道自己最近做过什么梦，并可在合适时告诉用户。
- 观测台梦境诊断展开最近 6 条梦境全文，不再只显示一行 42 字摘要。

### 群聊参与梦境与记忆（2026-07-07）
- 夜梦不再硬截到 300 字，只有超过 1200 字才安全截断；Dream Context 注入最近梦境时放宽到 600 字。
- dream.py 的兜底材料池现在纳入 private_group、small_group、big_group、public_group、group 来源记忆。
- chat_digest 对小群/私密群/大群/公开群的保留条数上调，让群聊活动更稳定地参与梦境。
观测台新增“强制重做”按钮；/api/dream/run 支持 force=true，允许忽略 already_dreamed 重新生成当天梦境。

### 梦境归因与夜梦筛选（2026-07-07）
- 梦境检测台 recent_dreams 收窄为 daemon 夜梦，不再混入 diary/category=dream 或手动自省。
- /api/dream/status 读取 status 文件后会实时刷新数据库里的夜梦列表。
- 梦境 prompt 和 chat_digest prompt 都增加说话者归因约束，避免把狗蛋/群友/其他 AI 的话误写成用户说。
