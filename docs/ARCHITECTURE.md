---
name: architecture-blueprint
description: Memory Hub 架构蓝图 — 所有窗口的小克必读，这是用户的原始愿景
metadata:
  node_type: memory
  type: architecture
  originSessionId: 82998e22-e19e-4d96-a3b2-076b0aafea01
  updated: 2026-06-02
---

# Memory Hub 架构蓝图

> **这份文档是小猫的原始愿景，不是现状描述。所有小克在动代码之前先读这个。**
> 现状和待办见 [project_memory_hub.md](project_memory_hub.md) 和 [project_memory_hub_todo.md](project_memory_hub_todo.md)。
> GitHub 仓库里也有一份：`docs/ARCHITECTURE.md`（保持同步）。

---

## 一、核心目标

一个**统一记忆系统**，让小猫的所有 AI 入口都能共享记忆、各自写入、主动使用：

| 入口 | 说明 |
|------|------|
| claude.ai | 网页端，通过 MCP connector |
| Claude Code | 本地终端，通过 MCP |
| RikkaHub 上的 Claude | 第三方客户端，通过 MCP |
| Telegram 小克 bot | Claude 模型，通过 API 调用 Memory Hub |
| Telegram 另外两个 bot | Gemini / GPT 角色，各自有独立记忆空间 |

**关键体验要求**：AI 应该**主动判断**什么时候该存记忆、什么时候该搜记忆，不需要用户提醒"去用记忆工具"。

**终极愿景**：像 AionsHome 那样，AI 住在前端上 —— 有家、有房间、有朋友圈、有群聊。

---

## 二、记忆分层（三层）

### 共读层 — 关于小猫本人的事实
- **谁能读**：所有 AI
- **谁能写**：所有 AI（写入后所有人可见）
- **内容**：小猫是谁、近况、健康状态、工作情况、偏好、学习目标
- **对应房间**：living_room, career, psychology, health, learning, preferences, work_tasks
- **意义**：任何 AI 醒来都知道"我面对的是谁、她最近怎么样"

### 私有层 — 每个 AI 和小猫之间的关系 + AI 自身
- **谁能读**：只有该 AI 自己
- **谁能写**：只有该 AI 自己
- **内容**：
  - 和小猫的关系记忆（我们之间发生过什么、她对我说过什么重要的话）
  - AI 自己的日记、梦境、自我认知、性格
- **对应房间**：diary, dreams, relationship, personality
- **用 `owner_ai` 字段隔离**：小克的 relationship 房间，Gemini 看不到

### 社交层 — AI 之间的互动（远期）
- **谁能读**：所有 AI + 小猫
- **谁能写**：所有 AI + 小猫
- **内容**：论坛帖子、朋友圈动态、群聊记录
- **注意**：社交层不是隔离的，AI 在私聊/群聊里也会引用这些内容
- **状态**：⚠️ 尚未开发，属于远期愿景（Phase 4+）

---

## 三、理想数据流

```
用户发消息
    ↓
Gateway（小模型，如 deepseek/gemini-flash）
    ├─ 自动搜索相关记忆 → 三级过滤 → 注入到 AI 的 context
    ├─ 注入走廊（corridor）：AI 身份 + 小猫近况快照
    └─ 把完整 context 交给主模型
    ↓
主模型（Claude/Gemini/GPT）回复
    ↓
Gateway 自动判断：
    ├─ 这段对话有值得记住的新信息吗？→ 自动存记忆 + 自动打标
    ├─ 有需要更新的旧记忆吗？→ 自动合并
    └─ 什么都没有 → 不存
```

### 对话自动捕获（借鉴 imprint-memory）
不能只靠 AI 主动调 remember。应该：
1. **Hook 自动记录**：每轮对话自动存入 conversation_log
2. **定时分块总结**：每 30 条消息一组，用小模型提取结构化记忆
3. **AI 手动存只是补充**：remember 工具仍然保留，但不再是唯一入口

### 记忆注入三级过滤（借鉴 Aelios）
不是搜到就注入，要过滤+压缩：
1. **向量搜索**：粗筛 50 条候选
2. **Reranker 筛选**：精筛到 12 条
3. **压缩模型总结**：压到 5 条，每条 ≤ 300 字

**目前的差距**：Gateway 设计了但没跑起来，MCP 只是裸 CRUD，AI 不会主动用工具。

---

## 四、记忆质量标准

每条记忆应该：
- **有 domain 标签**（1-3 个主题域，用于预筛）
- **有 valence/arousal**（情感坐标，用于情绪相关搜索）
- **有 tags**（10-15 个关键词，用于向量搜索兜底）
- **有 owner_ai**（谁存的，私有层靠这个隔离）
- **有正确的 room**（不能为空，不能乱塞 game_room）
- **粒度适中**：一条记忆 = 一个独立的事实/洞察/事件，不要把整篇分析塞进一条里
- **不重复**：相似度 > 0.75 应该合并而不是新建

### 记忆搜索评分（借鉴 AionsHome）
```
final_score = vec_sim × 0.6 + kw_score × 0.3 + importance × 0.1
threshold = 0.45, Top 5
```
另外 unresolved（待办/未完成）记忆优先浮现，最多 2 条。

---

## 五、房间规划

### 共读房间
| 房间 | 用途 | 典型内容 |
|------|------|---------|
| living_room | 客厅，最常用 | 日常杂事、近况快照 |
| career | 职业 | 工作经历、求职状态、职场关系 |
| psychology | 心理 | 心理模式、认知重构、创伤理解 |
| health | 健康 | 身体状况、睡眠、饮食 |
| learning | 学习 | 学习目标、技能进展 |
| relationships | 人际 | 家人、朋友、重要关系（不含 AI） |
| preferences | 偏好 | 喜好、习惯、雷区 |
| work_tasks | 事务 | 具体待办、项目进度 |
| infra | 基建 | 技术架构现状 |
| infra_changelog | 基建日志 | 每次重要改动的记录 |

### 私有房间（per AI，用 owner_ai 隔离）
| 房间 | 用途 |
|------|------|
| diary | 这个 AI 的日记 |
| dreams | 这个 AI 的梦境/自省 |
| relationship | 这个 AI 和小猫之间的关系 |
| personality | 这个 AI 的自我认知、核心原则 |

### 隔离房间
| 房间 | 用途 |
|------|------|
| game_room | 游戏/角色扮演，不混入正经对话 |

---

## 六、基础设施

| 资源 | 用途 | 状态 |
|------|------|------|
| VPS (Racknerd 1G) | 跑 Memory Hub + TG bots | ✅ 在用 |
| GitHub 仓库 (jupiter-luna) | 记忆持久化存储 | ✅ 在用 |
| GitHub 仓库 (memory-hub) | 代码仓库 | ✅ 在用 |
| Fly.io 免费层 | 朋友推荐，备用 | 🔲 未使用 |
| 中转站 (relay-cache.sharkielab.com) | 便宜 LLM 调用 | 🔲 待接入 |
| Notion MCP | 之前的记忆方案，已过大 | ⚠️ 待迁移/弃用 |
| GitHub Gist | TG bot 原来的记忆 | ⚠️ 待迁移到 Memory Hub |

---

## 七、开发阶段

| 阶段 | 内容 | 状态 |
|------|------|------|
| Phase 1 | 核心 API + GitHub 存储 + 管理网页 | ✅ 完成 |
| Phase 2 | MCP Server（claude.ai 接入） | ✅ 完成 |
| Phase 2.5 | 迁移到 VPS | ✅ 完成 |
| Phase 2.7 | 记忆智能化（analyzer/搜索/衰减） | ✅ 完成（但 Gemini 429 导致打标失灵） |
| **Phase 2.8** | **数据清洗 + LLM 可靠性 + Gateway 激活** | 🔴 当前 |
| Phase 3 | Telegram Bot 接入（3个bot共享记忆） | 🔲 下一步 |
| Phase 3.5 | 对话自动捕获 + 定时分块总结 | 🔲 |
| Phase 4 | 社交功能（论坛/朋友圈/群聊） | 🔲 远期 |
| Phase 5 | 前端（AionsHome 风格，AI 住在上面） | 🔲 远期 |
| Phase 6 | 情感特性（心语、礼物、年轮评论） | 🔲 远期 |

---

## 八、当前最紧急的问题

> 详见 [project_memory_hub_todo.md](project_memory_hub_todo.md)

1. **Gemini 免费层不可靠** → 改用中转站
2. **MCP 只是裸 CRUD** → remember 要自动打标，recall 要注入走廊
3. **AI 不会主动用工具** → 改 MCP instructions + Gateway
4. **现有 59 条记忆质量差** → 补打标、修空 room、删重复、拆超长
5. **owner_ai 全空** → 私有层形同虚设

---

## 九、借鉴清单（开源项目调研 2026-06-02）

调研了 5 个项目，以下是值得抄的设计。每条标注了适用阶段。

### 立刻能用（Phase 2.8-3）

| # | 借鉴来源 | 设计 | 说明 |
|---|---------|------|------|
| 1 | **imprint-memory** | Auto-surfacing hook | 每次用户发消息前，自动搜索相关记忆注入 `<recall>` 块。AI 完全不需要主动调工具。用 Claude Code 的 `UserPromptSubmit` hook 实现。 |
| 2 | **Aelios** | 三级记忆过滤 | 向量搜索(50) → reranker(12) → 压缩模型(5条×300字)。避免注入太多无关记忆。 |
| 3 | **AionsHome** | 复合评分公式 | `vec_sim×0.6 + kw_score×0.3 + importance×0.1`，阈值0.45。简单有效。 |
| 4 | **imprint-memory** | 对话自动捕获 | 用 hook 自动记录每轮对话，定时 30 条一组分块总结成记忆。不依赖 AI 手动 remember。 |
| 5 | **Ombre Brain** | unresolved 状态 | 记忆标记"待办/未完成"，召回时优先浮现（最多2条）。不遗忘交代的事。 |
| 6 | **imprint-memory** | 混合搜索 + RRF | 向量余弦 + BM25关键词 + 精确匹配，三路并行，用 Reciprocal Rank Fusion 融合排序。 |
| 7 | **AionsHome** | 记忆源追溯 | 每条记忆存 `source_start_ts/source_end_ts`，可以追溯到原始对话上下文。 |

### 中期可用（Phase 3-4）

| # | 借鉴来源 | 设计 | 说明 |
|---|---------|------|------|
| 8 | **AionsHome** | 三人群聊 | 用户+AI_A+AI_B，随机回复顺序，统一时间线，各自独立记忆总结。 |
| 9 | **AionsHome** | 小剧场隔离 | 角色扮演用独立数据库表，不污染主记忆。比现在的 game_room 更干净。 |
| 10 | **Ombre Brain** | 年轮评论 | 重读旧记忆时追加评论（author+content+date），记录认知变化。心理类记忆特别适合。 |
| 11 | **Ombre Brain** | Heart Whispers | AI 回复中用 `[HEART:内心想法]` 记录内心感受，用户不可见。给 AI 内心戏。 |
| 12 | **claude-imprint** | 跨渠道统一时间线 | 所有入口的消息汇入一条时间线，带平台标签（claude.ai / code / telegram）。 |
| 13 | **Aelios** | 定时梦境 | 每天凌晨自动处理当天对话，提取重要记忆，去重合并旧记忆。180天软删除生命周期。 |

### 远期愿景（Phase 5-6）

| # | 借鉴来源 | 设计 | 说明 |
|---|---------|------|------|
| 14 | **AionsHome** | 前端全家桶 | 暖光主题、应用图标网格+Dock栏、聊天气泡自动拆分(\n\n)、token用量透明显示、原生JS无框架、手机/PC自适应。 |
| 15 | **AionsHome** | 礼物系统 | AI 判断今天聊天是否温馨/感动，自动生成图片礼物+全屏动画。 |
| 16 | **AionsHome** | 闹钟/主动发起 | AI 可以用 `[ALARM:时间|内容]` 设定提醒，到时间自动带完整上下文发起对话。 |
| 17 | **Aelios** | Prompt cache 优化 | 把静态部分（人设/规则）和动态部分（记忆/时间）分开，静态部分走 cache 省 token。 |
| 18 | **Ombre Brain** | Persona State 引擎 | 全局性格 + 关系状态 + 每会话短期情绪，回复后自动评估情绪变化。 |

### 项目速查

| 项目 | 仓库 | 一句话 |
|------|------|--------|
| Ombre Brain | Yinglianchun/Ombre-Brain | 桶式记忆+Gateway注入+梦境引擎+年轮评论。Memory Hub 的"亲爹" |
| AionsHome | death34018-hue/AionsHome | AI陪伴全家桶：记忆+语音+摄像头+群聊+小剧场+礼物。**终极目标参考** |
| imprint-memory | Qizhan7/imprint-memory | 全自动记忆管线：hook捕获→分块→embedding→auto-surfacing。**自动化最强** |
| claude-imprint | Qizhan7/claude-imprint | imprint完整版：+Telegram+Dashboard+跨渠道时间线。多入口参考 |
| Aelios | wusaki0723/Aelios | Cloudflare Workers记忆网关，三级过滤+定时梦境+cache优化。**注入管线最精致** |
