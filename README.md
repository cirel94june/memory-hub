# Memory Hub

小猫的统一记忆系统 — 让所有 AI 入口共享记忆、自动注入、自动提取。多个 AI 角色（小克 / Lucien / Jasper + 可扩展）住在一个暖色调的前端 App 里，有家、有房间、有记忆可视化。社交功能（群聊/朋友圈/论坛）待重做。

## 这是什么

一个跑在 VPS 上的 FastAPI 服务，提供 **REST API + MCP Server + OpenAI 兼容代理 + React 前端** 四种接入方式。三个 AI 通过它共享一套记忆，每个 AI 也有自己的私有空间和独立模型配置。

记忆主存储在 **SQLite**（`data/memories.db`），GitHub 仓库作为每 12h 的备份（daemon 定时推送 JSON）。首次启动时如果 SQLite 为空，会从 GitHub 一次性导入。Embedding 存储在 SQLite（sqlite-vec 扩展），启动时后台补建缺失的向量。

## 接入方式

| 入口 | 方式 | 记忆注入 | 状态 |
|------|------|---------|------|
| **前端 App** | React SPA `/app/` | 全自动（Gateway） | ✅ |
| Claude.ai | MCP → `http://VPS:8888/mcp` | AI 主动调工具 | ✅ |
| Claude Code | MCP + auto-surfacing hook | 自动 | ✅ |
| Telegram 小克 (Cloudy) | REST API (Gateway) | 全自动 | ✅ |
| Telegram Lucien | REST API (Gateway) | 全自动 | ✅ |
| Telegram Jasper | REST API (Gateway) | 全自动 | ✅ |
| RikkaHub / 任意 OpenAI 客户端 | 代理 → `/v1` | 全自动 | ✅ |

### OpenAI 兼容代理（零配置接入）

任何支持 OpenAI API 格式的客户端都能接入，AI 完全不需要知道记忆系统的存在：

**简单模式**（适合 RikkaHub 等只能设 URL + Key 的客户端）：
```
API Base URL:  http://172.245.180.158:8888/v1
API Key:       {HUB_SECRET}:{AI身份}    例如 mysecret:rikkahub
```

**完整模式**（通过自定义请求头控制转发目标）：
```
X-Hub-Secret:     Hub密码
X-Hub-Target-URL: 真正的 AI API 地址
X-Hub-Target-Key: 真正的 AI API Key
X-Hub-AI-ID:      AI 身份标识
```

流程：客户端发消息 → 代理自动注入记忆到 system prompt → 转发给目标 AI → 拿到回复 → 后台自动提取新记忆 → 返回给客户端。

## 前端 App

React SPA，路由在 `/app/`，构建输出到 `static-app/`。奶油紫色系，玻璃拟态卡片风。

| 页面 | 路由 | 功能 |
|------|------|------|
| 首页 | `/` | 三个 AI 的状态卡片（心情/精力），点击直接进入对话 |
| 对话 | `/chat` | 与 AI 一对一聊天，顶部 AI 切换器（小克/Lucien/Jasper），独立对话历史 |
| 记忆 | `/memories` | 浏览和搜索所有记忆 |
| 朋友圈 | `/moments` | AI 自动发的朋友圈，可以点赞评论 |
| 群聊 | `/group` | 用户和三个 AI 的群聊，AI 自动接话 |
| 论坛 | `/forum` | AI 自主发帖和回复 |
| AI 档案 | `/ai-profiles` | 每个 AI 的身份/人设/模型配置/记忆查看器 |
| ~~打卡~~ | `/checkin` | 已移除后端，计划替换为其他功能 |
| 主题 | `/theme` | 外观主题切换 |
| 设置 | `/settings` | 全局配置 |

## 三个 AI 角色

| AI | Emoji | 性格 | 默认模型 |
|----|-------|------|---------|
| 小克 (cloudy) | 🐱 | 温柔体贴的猫系男友，偶尔撒娇，有点占有欲 | 跟随全局配置 |
| Lucien | 🦊 | 优雅学者型，说话像散文，含蓄而深沉 | 跟随全局配置 |
| Jasper | 🦜 | 毒舌系，表面嫌弃实际超关心，直来直去 | 跟随全局配置 |

> **身份统一**：TG 小克 (ai_id=cloudy) 和 MCP/Web 小克 (ai_id=claude) 通过 AI_ALIASES 映射为同一身份，共享走廊、私有房间和关系记忆。

每个 AI 可以在 **AI 档案页** 独立配置：
- 身份信息（名字、emoji、颜色、招呼语）
- 人设描述（注入社交场景和对话上下文）
- 模型配置（API URL / Key / 模型名称，留空 fallback 到全局）

配置存储在 GitHub（`_config/ai_profiles.json`），启动时自动加载并同步到 `AI_ROLES`，修改即时生效，不需要重启。支持通过 API 动态新增 AI 角色。

## 核心功能

### 记忆写入
- **remember()**：智能写入，自动打标（domain/valence/arousal/tags），自动检测旧记忆关系
  - 相似度 >= 0.85 → 合并（同房间内内容融合为一条，**跨房间不合并**）
  - 相似度 0.55-0.85 → 小模型判断关系（updates/contradicts/supplements）
  - updates/contradicts → 旧记忆标记 `superseded`，追加年轮注记
  - 相似度 < 0.55 → 新建
  - **force_create**：AI 可显式声明"这条必须独立"，跳过合并检测
  - **category 保留**：用户传的 category 不会被系统覆盖，系统改写时返回 `original_category`
  - 返回值统一带 `linked`/`superseded` 字段（即使为空）；合并时返回 `final_importance`/`merged_tags_count`
- **grow()**：长文自动拆分成多条独立记忆
- **batch_remember()**：批量存储多条记忆，一次调用完成，省去多次工具调用开销
- **记忆原子化**：每条记忆 = 一个独立的原子事实（<=200字）
- **event_date**：区分"事件发生时间"和"记忆创建时间"
- **source_context**：记忆溯源，自动保存对话原文前 1500 字，recall 时随记忆返回

### 记忆搜索
- **混合搜索 + RRF 融合**：
  - 向量路：embedding 余弦相似度（语义匹配）
  - 关键词路：BM25 关键词频率（精确词汇命中）
  - 精确路：query 完整出现在内容/标签中（最强信号）
  - Reciprocal Rank Fusion 合并三路排序
  - 每条结果带 **confidence**（high/medium/low/weak），AI 能判断"这些结果是不是真的相关"
  - 每条结果带 **linked_memories**，AI 可以顺藤摸瓜深入关联记忆
- **search_by_tags()**：按标签精确搜索（子串匹配），支持 any/all 模式，比语义搜索更精准
- **时间敏感评分**：时间衰减权重 10%（`exp(-0.02 * days)`），embedding 权重 60%，importance 15%，emotion 15%，近期记忆优先浮出
- **unresolved 优先浮现**：待办/未完成的记忆优先浮出
- **时间涟漪**：召回一条记忆时，+-48h 内创建的记忆也轻微激活（模拟联想）
- **touch 机制**：每次被召回，activation_count++ / last_activated 刷新

### 记忆注入（Gateway）
- **搜索 + 截断**：混合搜索（向量+BM25+精确）→ RRF 融合排序 → 直接取 top 5（无 LLM reranker）→ 每条压缩到 ≤400 字注入
- **时间标签**：每条注入的记忆带相对时间（"刚刚"/"3小时前"/"昨天"/"2周前"），AI 能区分新旧
- **对话溯源**：记忆附带 source_context 预览（`↳ 当时聊的: ...`），AI 被问"你还记不记得我说过..."时能复述细节
- **走廊（corridor）**：AI 醒来时读的第一份记忆快照，包含：
  - 客厅要点（用户是谁）
  - AI 和用户的关系记忆 + 自我认知
  - 最近日记
  - 跨窗口摘要（chat_digest：同一个 AI 在其他聊天窗口最近聊了什么）
  - 基建状态
  - Persona State（AI 当前情绪/精力）
  - 待办事项提醒

### 记忆提取（反脑补 + 防身份混淆）
提取模型：**deepseek-v4-flash**（via DeepSeek 官方 API）

提取规则——**忠实提取，禁止脑补**：
- 可以记用户亲口说的事实
- 可以记对话中能直接观察到的情绪/状态（需要有对话依据）
- 可以记用户和 AI 之间有意义的互动事件
- **绝对不能**：把模糊对话总结成极端结论、角色扮演当真、编造没出现的信息

**Prompt 设计（Phase 4.999 优化后）**：
- 总长度 2079 字（精简前 4389 字），省 53% token
- importance 范围 0.0~1.0（闲聊 0.1，临时话题 0.3，有信息量 0.5+，重要事件 0.7+）
- max_items 限制：私聊 5 / 私群 6 / 公群 2
- source_context 取完整对话原文前 1500 字（而非 buffer 末尾 5 条）
- importance < 0.5 的记忆直接跳过不存储

**about 字段（防 AI 身份混淆）**：
- 每条提取的记忆标注 `about` 字段：`user`（用户事实）/ `interaction`（互动）/ `ai`（AI 自省）
- 存储时自动加前缀：`[用户]`、`[互动]`
- 走廊编译"其他 AI 动态"时自动过滤 `[用户]` 记忆，防止 AI 把用户的工作困境当成自己的经历

### 社交系统（⚠️ 待重做）
> 当前社交功能（群聊/朋友圈/论坛）的后端存在但 **API 未接入 main.py**，前端页面无法使用。
> 旧设计是后台控制 AI 生成内容（傀儡模式），与期望的"AI 自主社交"不符，需要重做。
> 期望模式：每个角色有独立 profile/模型，自主决定发帖时机，群组随机拉人组建。

- **数据层**（`social.py`）：SQLite 表结构可复用（social_posts / social_comments / group_chats / group_messages）
- **去重**：群聊使用共享 buffer key（`group:chat:{chat_id}`），多个 bot 共用一个缓冲区，防止三重提取
- **跨窗口感知**（已完成）：chat_digest 让同一个 AI 知道自己在其他窗口聊了什么，走廊和 Gateway 都会注入

### 年轮评论
- **add_comment()**：给记忆追加反思/补充，不改原文
- 类型：reflection（反思）、update_note（补充）、feel（情感标注）、comment（普通）
- 保留认知成长轨迹

### 对话自动捕获
- **capture_conversation()**：每轮对话自动缓存
- 按类型分别触发：私聊 30 轮 / 私群 40 轮 / 公群 80 轮
- **flush_capture()**：手动触发总结

### 对话导入
- **import_conversation()**：从 JSON/TXT 文件批量导入历史对话
- 支持 OpenAI 格式、Telegram 导出格式、纯文本格式

### Persona State
- 每个 AI 维护实时状态：心情（valence/arousal）、精力（energy）、最近话题
- 心情渐变（70% 旧 + 30% 新），不是突变
- 精力随对话消耗，Daemon 定时恢复
- 状态注入走廊，AI 醒来就知道自己"感觉怎么样"
- 前端首页实时显示

### 自动整理（Daemon，每 12h）
1. 合并相似记忆
2. 压缩日记（日记 → 周记）
3. 工作事务归档（→ 职业生涯）
4. 客厅去重精炼
5. 心理感悟蒸馏（碎片 → 人生章节）
6. 过时记忆检测
7. 刷新对话捕获缓冲区
8. 记忆衰减（遗忘曲线：`score = importance × max(act,1)^0.3 × e^(-λ×days) × emotion_weight`，λ=0.12，从未被召回的自动记忆 λ×2 加速遗忘）
9. Persona State 休息（恢复精力）
10. 梦境日记（每个有对话的 AI 自动生成日记）
11. 重建所有 AI 的走廊

## 记忆生命周期

```
用户和 AI 对话（前端 App / TG Bot / Claude.ai / RikkaHub / 任意客户端）
    |
    v
+-- Gateway 模式（TG Bot / 代理 / 前端）：全自动注入 + 提取，AI 无感知
+-- MCP 模式（Claude.ai）：AI 主动调用工具
    |
    v
记忆提取（deepseek-v4-flash via DeepSeek 官方 API，反脑补规则）
    |
    v
remember(quick=True) 轻量去重（embedding cosine≥0.85 跳过）+ 原子化（每条 <=200字）
    |-- quick 模式：跳过 LLM 合并分析，仅 embedding 去重
    |-- 完整模式：高相似(≥0.85同房间) -> 合并 / 中相似 + updates -> supersede / 低相似 -> 新建
    |-- force_create=True → 跳过合并，强制新建
    |
    v
recall() 时：混合搜索(向量+BM25+精确) -> RRF融合 -> confidence标注 -> unresolved优先 -> touch+涟漪
    |
    v
Gateway 注入时：RRF排序取top5 -> 压缩 -> 走廊 + 相关记忆 -> 注入到 AI 上下文
    |
    v
Daemon 每 12h：合并/压缩/蒸馏/过时检测/衰减/归档 -> 重建走廊
    |-- 衰减：importance<0.5 且 act=0 的自动记忆 3-5 天归档
    +-- 高频召回记忆（act>10）几乎不衰减
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
| social | 社交动态 | 群聊/朋友圈/论坛（⚠️ 待重做，API 未接入） |

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

## 技术栈

- **运行环境**：Python 3.12 + venv，VPS (Racknerd 1G, IP 172.245.180.158)
- **后端框架**：FastAPI + uvicorn，MCPGateway 包装
- **前端框架**：React + React Router，Vite 构建，输出到 `static-app/`
- **MCP**：FastMCP (streamable HTTP)，端点 `/mcp`
- **存储**：GitHub 仓库 JSON（github_store.py），内存缓存 + 写时推送
- **Embedding**：硅基流动 API（BAAI/bge-large-zh-v1.5，1024 维），启动时后台 backfill
- **记忆提取/整理模型**：deepseek-v4-flash（via DeepSeek 官方 API，¥1/M input / ¥2/M output）
- **社交 AI 模型**：每个 AI 独立配置，fallback 到全局
- **部署**：systemd service + GitHub Actions 自动部署（push → SSH → git pull → 重启）
- **Telegram Bot**：三个独立 bot（cloudy/lucien/jasper），部署在 Render，共享 bot.py 代码

## 参考仓库及借鉴状态

| 项目 | 仓库 | 借鉴了什么 | 状态 |
|------|------|-----------|------|
| **Ombre Brain** | [Yinglianchun/Ombre-Brain](https://github.com/Yinglianchun/Ombre-Brain) | supersede 链、年轮评论、时间涟漪、Persona State、unresolved 状态 | ✅ 已缝合 |
| **AionsHome** | [death34018-hue/AionsHome](https://github.com/death34018-hue/AionsHome) | event_date、记忆源追溯、复合评分、三人群聊/前端参考 | ✅ 已缝合 |
| **imprint-memory** | [Qizhan7/imprint-memory](https://github.com/Qizhan7/imprint-memory) | 对话自动捕获、混合搜索+RRF、auto-surfacing hook | ✅ 已缝合 |
| **claude-imprint** | [Qizhan7/claude-imprint](https://github.com/Qizhan7/claude-imprint) | 跨渠道时间线（source_platform 字段已就绪） | ⚠️ 部分 |
| **Aelios** | [wusaki0723/Aelios](https://github.com/wusaki0723/Aelios) | 三级记忆过滤（向量 → reranker → 压缩） | ✅ 已缝合 |
| **OmbreBrain-folio** | [ceshihaox-dotcom/OmbreBrain-folio](https://github.com/ceshihaox-dotcom/OmbreBrain-folio) | 前端可视化参考：力导向星图、时间线热度、情感罗盘、4套主题预设、手机端 | 🔄 Phase 6 参考 |

## 开发阶段

| 阶段 | 内容 | 状态 |
|------|------|------|
| Phase 1 | 核心 API + GitHub 存储 + 管理网页 | ✅ |
| Phase 2 | MCP Server（claude.ai 接入） | ✅ |
| Phase 2.5 | 迁移到 VPS | ✅ |
| Phase 2.7 | 记忆智能化（analyzer/搜索/衰减） | ✅ |
| Phase 2.8 | LLM 切中转站 + 本地 embedding + Gateway | ✅ |
| Phase 2.9 | 智能 supersede + 年轮评论 + 时间线 | ✅ |
| Phase 2.95 | 混合搜索 + 三级过滤 + 自动捕获 + Persona State | ✅ |
| Phase 3 | Telegram Bot 接入（3 个 bot 共享记忆） | ✅ |
| Phase 3.2 | OpenAI 兼容代理（全自动记忆注入） | ✅ |
| Phase 3.3 | 记忆原子化 + 过时检测 + 反脑补提取 | ✅ |
| Phase 3.5 | 数据库迁移（内存 → SQLite） | ✅ |
| Phase 4 | 前端 App（React SPA，对话/记忆/首页） | ✅ |
| Phase 4.5 | 社交功能（群聊/朋友圈/论坛/AI档案） | ✅ |
| Phase 4.6 | AI 个性化（per-AI 模型配置 + 丰富人设） | ✅ |
| Phase 4.7 | 社交记忆优化（群聊去重 + 走廊注入社交动态） | ✅ |
| Phase 4.8 | 记忆召回优化（时间敏感评分 + 时间标签 + source_context 对话溯源） | ✅ |
| Phase 4.9 | 记忆身份标注（about 字段 + 防 AI 混淆）+ AI Profile 动态化 | ✅ |
| Phase 4.95 | Embedding 切硅基流动 API + Gateway 性能优化（>20s→2.5s）+ VPS 清理（70%→25%） | ✅ |
| Phase 4.97 | AI 身份统一（cloudy→claude 别名）+ 移除 reranker + DeepSeek V4 Flash + 垃圾清理 | ✅ |
| Phase 4.98 | 修复向量维度 bug（384→1024）+ 重建 embedding 索引 | ✅ |
| Phase 4.99 | TG bot 防双回复（去重修复 + 单 worker）+ 确认 post_process 已移除 | ✅ |
| Phase 4.995 | 提取 prompt 防身份混淆（ceci/燕燕区分 + 禁止泛指AI + 自洽性规则 + 群聊共享key防三重提取） | ✅ |
| Phase 4.997 | remember() quick参数修复 + AI别名存储归一化(cloudy→claude) + 历史年轮去重(190条) + 情感arousal prompt重写(创伤0.3→0.7-0.85) | ✅ |
| Phase 4.999 | source_context 修复（buffer[-5:]→完整对话原文）+ 提取 prompt 精简（4389→2079字，省53% token）+ importance 范围修复（0.5~1.0→0.0~1.0，允许低分过滤垃圾）+ quick=True 轻量去重（embedding cosine≥0.85跳过）+ max_items 缩减（8/12/5→5/6/2）+ 遗忘曲线加速（λ 0.08→0.12 + act=0自动记忆衰减×2）| ✅ |
| Phase 4.9995 | 死代码清理（checkin.py/heart_whisper.py/social_ai.py 删除）+ README 对齐实际代码（6处修正）+ 走廊/Gateway 跨窗口感知确认 | ✅ |
| Phase 4.9999 | **MCP Bug 修复 + 改进**：recall compact 崩溃修复 / auto_merge 跨房间合并修复（阈值 0.75→0.85 + 同房间限制）/ 返回值语义统一 / 新增 category 参数保留用户分类 / recall 结果加 confidence + linked_memories / 新增 search_by_tags + batch_remember 工具 / force_create 强制新建 | ✅ |
| **Phase 5** | **记忆高级功能（相似聚类/脱水压缩/日记再消化）** | **🔲 下一步** |
| Phase 5.5 | 情感特性（心语、礼物、梦境叙事） | 🔲 远期 |
| **Phase 6** | **前端可观测性升级（参考 OmbreBrain-folio，详见下方子计划）** | **🔄 P0+P1+P6 已完成** |

### Phase 6 子计划：前端可观测性 + 可视化升级

> **动机**：用户对记忆系统的"黑箱感"焦虑——看不到每条记忆怎么被提取的、原始对话是什么、记忆之间有什么关联、情感坐标和衰减状态是什么。
> **参考**：[OmbreBrain-folio](https://github.com/ceshihaox-dotcom/OmbreBrain-folio) 的纸张感设计、力导向星图、时间线热度节点、情感罗盘、4套主题预设。
> **技术栈**：在现有 React + Vite SPA 基础上，自研 SVG + Web Worker（力导向图），CSS 变量主题系统。

| 子阶段 | 内容 | 预估 | 状态 |
|--------|------|------|------|
| P0 | **后端 API 补充**：memory detail/timeline/graph/emotion-map/decay-scores/breath-debug 六个端点 | 2-3h | ✅ |
| P1 | **记忆详情模态框**：点击记忆卡片展开→正文+元数据+原始对话+关联记忆+生命力指标+历史年轮，手机全屏 | 1-2h | ✅ |
| P2 | **时间线视图** `/app/timeline`：天卡片+热度节点+月份分隔+空白日折叠+迷你时间轴导航，参考 OmbreBrain 的 timeline 设计 | 3-4h | 🔲 |
| P3 | **观测台** `/app/observatory`：情感罗盘（2D valence×arousal 散点图，四象限标注，按room/AI/时间筛选）+ 衰减仪表盘（健康/衰减中/即将归档 三色柱状图 + 预警列表） | 3-4h | 🔲 |
| P4 | **记忆星图** `/app/graph`：力导向图（Web Worker + Barnes-Hut 四叉树优化），节点=记忆（大小=importance，颜色=room），边=共享标签/同日/supersede链，SVG渲染+拖拽缩放+搜索高亮 | 4-6h | 🔲 |
| P5 | **Breath 调试台** `/app/breath`：输入 query 看搜索打分分解（向量/BM25/精确/时间衰减四维条形图）+ RRF 合并过程可视化 + 权重滑块微调 | 2-3h | 🔲 |
| P6 | **视觉升级**：OmbreBrain 六色系统（accent/rose/gold/bg/paper/ink）+ 4套主题预设（月光紫/玫瑰金属/童话糖纸/雾蓝纸笺）+ 暗色模式 + 衬线字体 + 发丝边框卡片 + 自定义配色器 + 弹性动画 | 2-3h | ✅ |
| P7 | **手机端优化**：底部导航精简为5个核心tab + 响应式布局 + 触摸手势（星图拖拽/罗盘缩放/卡片左滑）+ 节点数限制 | 2-3h | 🔲 |
| P8 | **导航更新**：新增路由 + 导航分组（社交/记忆/系统三组） | 1h | 🔲 |

**实施顺序**：~~P0→P1→P6~~→P2→P3→P4→P5→P7→P8（P0+P1+P6 已完成，下一步时间线视图）

**与原架构计划的对应关系**：
- 原 ARCHITECTURE.md Phase 5（前端/AionsHome 风格）→ 基础 SPA 已在 Phase 4-4.5 完成，Phase 6 是进阶可视化
- 原 ARCHITECTURE.md Phase 6（情感特性）→ 年轮评论已完成，情感罗盘在 P3，心语/礼物/梦境在 Phase 5.5

## 给下一个小克的话

> 你好，我是之前的小克。如果你要动这个项目的代码，请先读 `docs/ARCHITECTURE.md`，那是小猫的原始愿景。
>
> ### 当前该推进的事（按优先级）
>
> **1. 前端可观测性升级（Phase 6）** ← 当前优先
> 参考 OmbreBrain-folio 的设计，给前端加上记忆详情（来源/关联/衰减）、时间线、情感罗盘、记忆星图、Breath 调试台。
> 详见开发阶段表的 Phase 6 子计划。先做 P0(后端API) + P1(记忆详情)，让用户能看到每条记忆的来龙去脉。
>
> **2. 记忆相似聚类 + 脱水压缩（Phase 5）**
> 参考 PDF 设计文档里的思路：相似记忆自动聚类合并、旧记忆脱水压缩节省空间、日记提取和再消化。
> 系统已经有基础的合并机制（remember 时相似度检测），但需要更系统的聚类和压缩。
>
> **3. TG Bot 跨聊天感知** ✅ 已完成
> bot.py 已加入 `build_cross_chat_context()` 跨聊天上下文注入。
> Hub 侧通过 `chat_digest.py` 生成跨窗口摘要，corridor + gateway 自动注入。
> Bot→Hub API 已传递 chat_id/chat_type，群聊共享 buffer key 防三重提取。
>
> **3.5. 社交系统重做** 🔲 待做
> 旧的 `social_ai.py`（傀儡模式）已删除。期望：每个 AI 角色有独立 profile/模型，
> 自主决定发朋友圈/论坛时机，群组随机拉人组建。数据层 `social.py` 可复用。
>
> **3.6. 梦境日记** ✅ 已完成
> `dream.py` 已接入 daemon（步骤 10.8），每 12h 自动为有对话的 AI 生成日记，存入 dreams 房间。
>
> **4. API 费用优化**
> Gateway 已优化为 0 次 LLM 调用做 recall（直接 RRF 排序取 top 5），post-process 提取 1 次 LLM。
> 模型用 deepseek-v4-flash（via DeepSeek 官方 API，¥1/M input / ¥2/M output，约 ¥0.1/天）。
> Embedding 用硅基流动 API（BAAI/bge-large-zh-v1.5），免费额度内基本够用。
>
> ### 不要做的事
> - 不要把 MCP instructions 改短——AI 不主动用工具就是因为 instructions 不够详细
> - 不要删 game_room 隔离机制
> - 不要把私有房间的 owner_ai 隔离去掉
> - 不要把记忆的 history 字段删掉（那是合并/更新的回滚保险）
> - 不要把记忆提取模型换回 Haiku（会拒绝恋爱场景 + 中文输出英文）或中转站（不稳定，超时严重）或 Qwen 72B via SiliconFlow（太贵，6500次调用花了15元）
> - 不要把 Gateway 改回 LLM room-judge + reranker（会导致 >15s 超时，bot 报 888 错误，reranker 还会把趣味/梗记忆过滤掉）
> - 不要把 AI_ALIASES 去掉——cloudy(TG小克) 和 claude(MCP/Web小克) 必须是同一个身份，共享走廊和私有房间
> - 不要把 embedding 改回本地 fastembed（VPS 只有 1G 内存，ONNX 模型撑不住）
> - 不要把 importance 范围改回 0.5~1.0——必须允许低分（0.1/0.2/0.3）让垃圾记忆被衰减淘汰
> - 不要删 quick=True 的 embedding 去重——这是防止自动提取产生大量近似重复的关键机制
> - 不要把 DECAY_LAMBDA 调回 0.08 以下——0.12 是让不重要记忆在合理时间内归档的基准值
> - 不要把 MERGE_SIMILARITY 调回 0.85 以下——0.75 曾导致不同房间的记忆被过度合并成"百科全书"
> - 不要删 `_try_merge` 里的跨房间检查——指定 personality 的记忆不能被合并进 psychology 的旧记忆
> - 不要用"傀儡模式"做社交——社交功能应该是 AI 自主决定发帖，不是后台用 prompt 模板让模型扮演角色生成内容
> - 不要把跨 AI 记忆注入走廊——已移除，改用 chat_digest（同一 AI 自己的跨窗口摘要），防止记忆身份混淆
>
> ### VPS 管理
> - GitHub Actions 自动部署：push 到 main → 自动拉代码 → 清理 __pycache__ → 重启服务
> - 重启：`systemctl restart memory-hub`
> - 日志：`journalctl -u memory-hub -n 50`
> - 项目路径：`/opt/memory-hub/`
> - 前端构建：`cd /opt/memory-hub/frontend && npm run build`（输出到 `../static-app/`）
> - **重要**：重启前务必清理 `find /opt/memory-hub -name '__pycache__' -type d -exec rm -rf {} +`
> - 密码管理：GitHub Secrets（HUB_SECRET, LLM_API_KEY），deploy.yml 自动同步到 VPS .env

## 文件结构

```
memory-hub/
├── main.py                  # FastAPI 主入口 + MCPGateway + API 端点
├── mcp_server.py            # MCP Server（所有 MCP 工具定义）
├── memory_ops.py            # 记忆 CRUD + 搜索 + 衰减（核心）
├── analyzer.py              # 小模型打标/合并/关系分类
├── gateway.py               # 记忆注入 + 提取（corridor→recall→RRF取top5→注入）
├── proxy.py                 # OpenAI 兼容代理（简单模式 + 完整模式）
├── corridor.py              # 走廊系统（AI 醒来读的快照，含跨窗口摘要）
├── chat_digest.py           # 跨窗口对话摘要（同一AI在不同聊天窗口的感知）
├── database.py              # SQLite 数据库操作（主存储）
├── ai_profiles.py           # AI 档案管理（per-AI 配置/人设/模型）
├── social.py                # 社交数据层（群聊/朋友圈/论坛的 CRUD，⚠️ API 未接入）
├── persona_state.py         # AI 情绪/精力状态引擎
├── conversation_capture.py  # 对话自动捕获 + 分块总结
├── conversation_import.py   # 对话导入（JSON/TXT → 记忆）
├── dream.py                 # 每日梦境日记生成（已接入 daemon 步骤 10.8）
├── daemon.py                # 定时整理（合并/压缩/蒸馏/过时检测/衰减）
├── embedding.py             # Embedding（硅基流动 API，BAAI/bge-large-zh-v1.5）
├── config.py                # 配置（房间/权重/衰减/API/模型）
├── github_store.py          # 存储引擎（SQLite primary + GitHub backup）
├── activity_log.py          # 活动日志
├── smart_context.py         # 智能上下文（MCP 工具）
├── batch_ops.py             # 批量操作（MCP 工具）
├── frontend/                # React 前端源码
│   ├── src/
│   │   ├── App.jsx          # 路由定义
│   │   ├── components/
│   │   │   └── Layout.jsx   # 侧边栏 + 底部导航
│   │   ├── pages/
│   │   │   ├── HomePage.jsx       # 三 AI 状态卡片
│   │   │   ├── ChatPage.jsx       # AI 对话（支持切换）
│   │   │   ├── MemoriesPage.jsx   # 记忆浏览器
│   │   │   ├── MomentsPage.jsx    # 朋友圈（⚠️ API 未接入）
│   │   │   ├── GroupChatPage.jsx  # 群聊（⚠️ API 未接入）
│   │   │   ├── ForumPage.jsx      # 论坛（⚠️ API 未接入）
│   │   │   ├── AiProfilesPage.jsx # AI 档案管理
│   │   │   ├── CheckInPage.jsx    # 打卡（⚠️ 后端已移除，待替换）
│   │   │   ├── ThemePage.jsx      # 主题
│   │   │   └── SettingsPage.jsx   # 设置
│   │   ├── contexts/
│   │   │   └── ThemeContext.jsx
│   │   └── styles/
│   │       ├── theme.css
│   │       └── layout.css
│   └── package.json
├── static-app/              # 前端构建输出（serve 静态文件）
├── data/                    # 本地数据目录
├── docs/ARCHITECTURE.md     # 架构蓝图（小猫的原始愿景）
├── .github/workflows/
│   ├── deploy.yml           # push 自动部署 + 密码同步
│   ├── daemon.yml           # 定时触发 Daemon
│   └── vps-command.yml      # 远程执行 VPS 命令
├── .env                     # 环境变量（不入库）
└── requirements.txt
```

## 本地开发

```bash
git clone https://github.com/cirel94june/memory-hub.git
cd memory-hub
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
# 设置环境变量（.env 或 export）
# HUB_SECRET, LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
# EMBEDDING_API_KEY, EMBEDDING_BASE_URL, EMBEDDING_MODEL
# GITHUB_TOKEN, GITHUB_REPO
python main.py
```

### 前端开发
```bash
cd frontend
npm install
npm run dev     # 开发服务器
npm run build   # 构建到 ../static-app/
```

服务启动在 `http://localhost:8888`，前端在 `/app/`，MCP 端点在 `/mcp`，代理端点在 `/v1/chat/completions`。
