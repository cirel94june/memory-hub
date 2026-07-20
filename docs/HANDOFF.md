# 给下一个小克的话

> 你好，我是之前的小克。如果你要动这个项目的代码，请先读 `docs/ARCHITECTURE.md`，那是小猫的原始愿景。

## Memory Hub 的职责边界

Memory Hub 是**跨 AI 共享记忆后端**。它管"记住什么、谁知道、何时能想起"。

**负责：**
正式长期记忆、记忆候选区、AI 私人记事本、年轮与评论、更新/补充/纠错/注释、原始对话保险箱、锚点、近期 Episode / Recent Context、跨渠道记忆连续性、可见性和召回策略、防止串台/错记/玩笑人设化。

**不负责：**
塔罗抽牌、星盘计算、游戏世界剧情、地图/音乐/浏览器 UI、直接调用模型、房屋事件与家具、模型 provider 配置、Telegram / Lamplight 的聊天界面。

---

## 当前该推进的事（按优先级）

核心目标：**让 Memory Hub 成为完善的记忆后端——解决 AI 换窗换端口失忆和人格漂移，完成记忆可视化。**

### P0：MemoryProposal 候选区 + 置信度分层（issue #9）

**小模型不能直接写正式库。** 这是当前最大的架构缺陷——所有自动提取直接进入 canonical memory，导致玩笑人设化、重复写入、低质量记忆污染召回。

三层结构：
```
AI 私人记事本（scratchpad）
      ↓
MemoryProposal 候选区
      ↓
Canonical Memory 正式库
```

MemoryProposal 核心字段（对齐 Lamplight PR#4 的 Zod schema）：
```ts
type MemoryProposal = {
  id: string;
  content: string;
  claimType: "fact" | "observation" | "hypothesis";
  speechMode?: "literal" | "playful" | "hypothetical" | "fictional" | "uncertain";
  proposedRoom: string;
  sourceConversationId: string;
  sourceMessageIds: string[];
  evidenceExcerpt: string;
  proposerId: string;
  confidence: number;
  visibility: "private" | "household" | "external_safe";
  conflictsWith?: string[];
  status: "pending" | "auto_approved" | "approved" | "rejected";
};
```

claimType 控制逻辑：
- **fact**：仅用户原话 + literal + 低敏感 + 无冲突时允许自动通过
- **observation**：默认进候选区，高门槛才能晋升
- **hypothesis**：永不自动进事实库，只进私人笔记或年轮
- 健康/创伤/身份/边界类内容无论 claimType 都进候选或 safe pipeline
- 冲突内容必须进候选，不能静默覆盖

speechMode 防止闲聊玩梗变人设：
- literal → 正常分流
- playful → 默认不提取为现实事实
- hypothetical → 不提取为事实，可作为 hypothesis
- fictional → Memory Hub 不存世界 lore
- uncertain → 候选或跳过
- 拿不准就不写正式库

**现有基础：** provenance_type（user_statement/ai_summary/ai_speculation/roleplay_meme）+ fact_confidence + _can_supersede() 守卫 + 玩梗快衰减 + importance 封顶。需要重构为显式的 proposal 表 + 审批流。

### P1：Episode 短期情景层（跨渠道连续性）

解决"AI 换窗换端口就失忆"的核心。Episode 不是 canonical memory，是有过期时间的短期情景记录。

```ts
type Episode = {
  id: string;
  kind: string;                    // tarot_reading / casual_chat / game_session
  actorIds: string[];              // 参与者
  sourcePlatform: "telegram" | "lamplight" | "web" | "system";
  summary: string;
  visibility: "private" | "household";
  allowedAiIds?: string[];         // 谁能看到
  disclosurePolicy: "normal" | "do_not_initiate_in_group" | "manual_only";
  importance: number;
  createdAt: string;
  expiresAt?: string;
  status: "active" | "expired" | "promoted";
};
```

用途举例：小猫刚在 Lamplight 和 Lucien 看完塔罗 → Episode 记录 → 5 分钟后 Lucien 在 Telegram 仍记得 → Cloudy/Jasper 未参与则看不到。

结构化业务事件（塔罗、游戏暂停等）由业务服务直接写 Episode，不交给小模型总结。小模型只处理非结构化闲聊。

Episode 可以到期失效、保持近期优先召回、经 MemoryProposal 晋升为长期记忆。

**现有基础：** chat_digest 做了部分跨窗口摘要，但不够——缺结构化事件、缺可见范围、缺过期机制。

### P2：召回排序修正（issue #4）

- provenance/room 加权：用户原话事实 > AI 总结 > AI 推测
- verification = 有 source_context 直接支持的 fact 优先
- 高激活但低置信内容降权
- 缺少证据的 "fact" 不进唤醒必读

### P3：会话接力层（issue #2）

同一个 AI 跨 Telegram / Lamplight / Web 不应转头失忆。Episode 层解决近期情景，会话接力解决进行中任务的交接。

### P4：统一 Context API

给 Agent Runtime 提供一份跨渠道上下文接口：
```
get_agent_context(agent_id, user_id, platform, conversation_id, participants, current_message)
→ 相关长期记忆 + 近期 Episode + AI 私人笔记 + 未解决事项 + 走廊增量 + 可见范围过滤
```
**现有基础：** `gateway.build_context()` 已做了约 80%，缺 Episode 注入和更干净的参数结构。

### P5：Channel Binding + 统一身份

Hub 内部只认 userId=ceci / agentId=lucien|cloudy|jasper，不认 Telegram bot ID、Lamplight 前端 ID、provider 名称。Lamplight 真正接入时需要正式的 Channel Binding 映射。

**现有基础：** AI_ALIASES + AI_ALIAS_GROUPS + identity_registry 已覆盖当前需求。

### P6：Doctor 全面检查强化（issue #6/#8）

继续强化审计项：
- speechMode 可疑内容扫描
- 游戏内容误入现实库
- Episode 越权注入
- 高激活但低置信内容
- 缺少证据的 fact
- 工具结果误入 canonical memory

### P7：Lamplight 审核 API

给 Lamplight 提供记忆管理接口：搜索、查看详情/证据、候选审核、纠错、更新、补充、注释、锚定、可见范围调整、Episode 查看、私人笔记查看权限、doctor 报告。Lamplight 不直接 SELECT 或更新 Hub 核心表。

**现有基础：** MCP 工具层 + REST API 已覆盖大部分，候选审核和 Episode 需随 P0/P1 新增。

### 持续维护：前端可视化

观测台（`/app/observatory`）是记忆管理入口，后续 Dream Context、人生章节 skip 诊断、召回 token 诊断、Obsidian 安全报告继续收敛到这里。

---

## 关键设计约束

### 记忆四种操作（必须区分）

- **更新**：过去正确，现在变化 → update_memory
- **补充**：旧内容不完整，但不错误 → add_comment (kind=update_note)
- **纠错**：原内容从一开始就错 → apply_user_correction（标记 incorrect、排除召回、保留原文和审计、建立正确 canonical）
- **注释**：不改事实，增加理解/感受/反思 → add_comment (kind=reflection/feel)

不能只在错误记忆下面加一句年轮道歉。

### 证据链要求

MemoryProposal 必须保存 source_message_ids + evidence_excerpt。禁止：
- 用 AI 的回复证明用户事实
- 从群聊玩笑总结稳定人设
- 把 AI 的解释性结论当成用户陈述
- 截断上下文后忽略否定、反讽和假设

### 工具结果不等于记忆

默认禁止把以下内容直接写入 canonical memory：实时位置、路线查询、搜索摘要、AI 推荐歌曲、塔罗牌义、星盘解读、某次工具调用结果。工具调用可以形成 Episode 或审计引用，但长期记忆必须经过 MemoryProposal。

### 游戏内容隔离

conversationKind = game_world 默认禁止提取为现实事实。game_discussion（场外讨论）也只允许生成 gameplay_preference / gameplay_feedback / interaction_preference，不能提取 AI 现实人格或用户现实经历。

### 群聊和私聊的知识边界

记忆连续性 ≠ 全屋广播。必须区分哪些 AI 参与过、哪些 AI 可以知道、哪些内容可以主动提起。过滤必须覆盖 recall、recent feed、corridor、smart_context、episode injection、unresolved injection 全部入口。

**现有基础：** visibility.can_view() 已统一覆盖所有入口（2026-07-17 大修）。

---

## 不要做的事

### 架构红线
- 不要把 MCP instructions 改短——AI 不主动用工具就是因为 instructions 不够详细
- 不要删 game_room 隔离机制
- 不要把私有房间的 owner_ai 隔离去掉
- 不要把记忆的 history 字段删掉（那是合并/更新的回滚保险）
- 不要把 AI_ALIASES 去掉——cloudy(TG) 和 claude(MCP/Web) 必须是同一个身份
- 不要把跨 AI 记忆注入走廊——改用 chat_digest，防止记忆身份混淆
- 不要用"傀儡模式"做社交——AI 自主决定发帖，不是后台模板扮演

### 参数红线
- 不要把 embedding 改回本地 fastembed（VPS 只有 1G 内存）
- 不要把 importance 范围改回 0.5~1.0——必须允许低分让垃圾记忆被衰减淘汰
- 不要删 quick=True 的 embedding 去重——防止自动提取产生大量近似重复
- 不要把 DECAY_LAMBDA 调回 0.08 以下——0.12 是让不重要记忆合理归档的基准
- 不要把 MERGE_SIMILARITY 调回 0.85 以下——0.75 曾导致跨房间过度合并
- 不要删 `_try_merge` 里的跨房间检查

### 模型红线
- 不要把记忆提取模型换回 Haiku（拒绝恋爱场景 + 中文输出英文）、中转站（不稳定超时）、Qwen 72B via SiliconFlow（太贵）
- 不要把 Gateway 改回 LLM room-judge + reranker（>15s 超时、过滤趣味记忆）

### 数据规则
- `source_ai` 是捕获来源，不是可见范围；`owner_ai` 只用于 private 归属
- 一对一私聊 `chat_type=private` 才能自动写 private + owner_ai；private_group 是小群聊，不自动塞私有
- 时区：数据库存 UTC，用户可见统一用 `time_utils.py` 的 Asia/Shanghai
- 数据以 GitHub 云端为准，Obsidian 只是阅读入口
- 小克别名合并区分用途：名字/人设读 `cloudy`，模型配置读实际调用 id 再 fallback
- Pulse 打标默认保守：普通闲聊输出 `{}`

---

## 已完成的关键里程碑

| 日期 | 内容 |
|---|---|
| 2026-07-18 | #10 修复：daemon 过期逻辑限定任务类 + living_room_refresh 去重闸 + unarchive 工具 |
| 2026-07-17 | 大修（Lucien 三轮 + 小克两轮 MCP 审计）：visibility.can_view 统一过滤、provenance 体系、apply_user_correction 纠错、embedding 自愈、正文完整性审计、doctor 内容级体检、玩梗快衰减、别名表补全 |
| 2026-07-13 | MCPGateway 审计层（mcp_audit.jsonl） |
| 2026-07-08 | 衰减归档与年轮可见性修正 |
| 2026-07-07 | 梦境机制全面修正（截断/材料/调性/诊断/补跑）、MCP identity 稳定化、Dream Context 注入 |
| 2026-07-06 | 观测台收敛、客厅画像、群聊级 chat_digest、醒来预览 |
| 2026-07-03 | Gateway compact 模式、衰减解释、观测台、Safety Kit、跨窗口记忆 |
| 2026-06-26 | 社交系统修复（OOC/消息消失/画图功能） |
| 2026-06-23 | Anchor 锚点、动态 AI 档案 |

---

## VPS 管理

- GitHub Actions 自动部署：push 到 main → git pull → pip install → npm build → 清理 __pycache__ → 重启服务
- 重启：`systemctl restart memory-hub`
- 日志：`journalctl -u memory-hub -n 50`
- 项目路径：`/opt/memory-hub/`
- 密码管理：GitHub Secrets（HUB_SECRET, LLM_API_KEY），deploy.yml 自动同步到 VPS .env
- 部署以 GitHub 为准：deploy.yml 用 `git reset --hard origin/main` 对齐，`.env` 和 `data/` 不在 Git 里不受影响
- **⚠️ HUB_SECRET 轮换仍未做**——旧密钥 `xiaoke588887` 在 git 历史中，转私有前必须 filter-repo 清理

### 技术栈速查
- 后端：FastAPI + SQLite（`data/memories.db`）+ MCP (FastMCP)
- 前端：React（`frontend/` → 构建到 `static-app/`）
- 嵌入：硅基流动 API（BAAI/bge-large-zh-v1.5）
- 小模型：deepseek-v4-flash via DeepSeek 官方 API
- 定时维护：进程内调度器（北京 02:00/14:00）
- 测试：`ALLOW_DEFAULT_HUB_SECRET=1 python -m pytest tests/ -q`（29 条）
