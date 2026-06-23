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

## 社交系统（⚠️ 待重做）

当前社交功能（群聊/朋友圈/论坛）的后端存在但 **API 未接入 main.py**，前端页面无法使用。
期望模式：每个角色有独立 profile/模型，自主决定发帖时机，群组随机拉人组建。
数据层 `social.py` 可复用。

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
5. 心理感悟蒸馏（碎片 → 人生章节）
6. 过时记忆检测
7. 刷新对话捕获缓冲区
8. 记忆衰减（遗忘曲线：`score = importance × max(act,1)^0.3 × e^(-λ×days) × emotion_weight`，λ=0.12）
9. Persona State 休息（恢复精力）
10. 梦境日记（每个有对话的 AI 自动生成日记）
11. 重建所有 AI 的走廊

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
