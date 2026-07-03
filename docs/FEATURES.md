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
| **夜间梦境日记** | 优先使用当天同一 AI 的 `chat_digests`；若摘要不足 2 条，则从最近 72 小时内仍 active、importance >= 0.5 的私聊/小群有效记忆补材料，至少 3 条才生成 | 新增 `layer=private`、`room=diary`、`category=dream`、`source_platform=daemon_dream` 的梦境日记 | 梦境入口、skip log、Dream Context 浮现/注入开关；考虑与 `room=dreams` 合并展示 |
| **MCP dream 自省** | AI 主动调用 MCP `dream()` 工具 | 新增 `room=dreams` 的私有自省/梦境 | 和 daemon 夜梦统一展示，避免 diary/dreams 两套入口割裂 |

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
