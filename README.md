# Memory Hub

小猫的统一记忆系统 — 让所有 AI 入口共享记忆、自动注入、自动提取。

## 这是什么

一个跑在 VPS 上的 FastAPI 服务，提供 **REST API + MCP Server + OpenAI 兼容代理** 三种接入方式。所有 AI（Claude / Gemini / GPT / TG Bot）通过它共享一套记忆，每个 AI 也有自己的私有空间。

记忆存储在 GitHub 私有仓库 `jupiter-luna` 里（JSON 文件），服务启动时加载到内存，修改后异步推送回 GitHub。

**终极愿景**：AI 住在一个暖色调的前端 App 上——有家、有房间、有论坛、有群聊、有日记本、有打卡日历。奶油紫/粉色系，圆角卡片，手绘插画感。

## 接入方式

| 入口 | 方式 | 记忆注入 | 状态 |
|------|------|---------|------|
| Claude.ai | MCP → `http://VPS:8888/mcp` | AI 主动调工具 | ✅ |
| Claude Code | MCP + auto-surfacing hook | 自动 | ✅ |
| Telegram 小克 (Cloudy) | REST API (Gateway) | 全自动 | ✅ |
| Telegram Lucien | REST API (Gateway) | 全自动 | ✅ |
| Telegram Jasper | REST API (Gateway) | 全自动 | ✅ |
| RikkaHub / 任意 OpenAI 客户端 | 代理 → `http://VPS:8888/v1` | 全自动 | ✅ |

### OpenAI 兼容代理（零配置接入）

任何支持 OpenAI API 格式的客户端都能接入，**AI 完全不需要知道记忆系统的存在**：

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

## 核心功能

### 记忆写入
- **remember()**：智能写入，自动打标（domain/valence/arousal/tags），自动检测旧记忆关系
  - 相似度 >= 0.75 → 合并（内容融合为一条）
  - 相似度 0.55-0.75 → 小模型判断关系（updates/contradicts/supplements）
  - updates/contradicts → 旧记忆标记 `superseded`，追加年轮注记
  - 相似度 < 0.55 → 新建
- **grow()**：长文自动拆分成多条独立记忆
- **记忆原子化**：每条记忆 = 一个独立的原子事实（<=80字），像虚空中浮动的光点
- **event_date**：区分"事件发生时间"和"记忆创建时间"
- **source_context**：记忆溯源，可追溯到原始对话片段

### 记忆搜索
- **混合搜索 + RRF 融合**（借鉴 imprint-memory）：
  - 向量路：embedding 余弦相似度（语义匹配）
  - 关键词路：BM25 关键词频率（精确词汇命中）
  - 精确路：query 完整出现在内容/标签中（最强信号）
  - Reciprocal Rank Fusion 合并三路排序
- **unresolved 优先浮现**：待办/未完成的记忆优先浮出
- **时间涟漪**：召回一条记忆时，+-48h 内创建的记忆也轻微激活（模拟联想）
- **touch 机制**：每次被召回，activation_count++ / last_activated 刷新

### 记忆注入（Gateway）
- **三级过滤**（借鉴 Aelios）：
  - L1：混合搜索粗筛 → 12 条候选
  - L2：小模型 reranker 精筛 → 5 条
  - L3：每条压缩到 <=300 字注入
- **自动房间路由**：小模型根据用户消息判断该查哪些房间
- **走廊（corridor）**：AI 醒来时读的第一份记忆快照，包含：
  - 客厅要点（用户是谁）
  - AI 和用户的关系记忆 + 自我认知
  - 最近日记 + 其他 AI 的动态（跨端感知）
  - Persona State（AI 当前情绪/精力）
  - 待办事项提醒

### 记忆提取（反脑补机制）
提取模型：Claude Haiku 4.5（判断力强，不容易瞎编）

提取规则——**忠实提取，禁止脑补**：
- 可以记用户亲口说的事实
- 可以记对话中能直接观察到的情绪/状态（需要有对话依据）
- 可以记用户和 AI 之间有意义的互动事件
- **绝对不能**：把模糊对话总结成极端结论、角色扮演当真、编造没出现的信息
- 判断标准：能否在对话中找到这条记忆的直接依据？找不到就不记

### 年轮评论（借鉴 Ombre Brain）
- **add_comment()**：给记忆追加反思/补充，不改原文
- 类型：reflection（反思）、update_note（补充）、feel（情感标注）、comment（普通）
- 保留认知成长轨迹——半年前的心理记忆，现在回看有新理解，追加评论而不是改原文

### 对话自动捕获（借鉴 imprint-memory）
- **capture_conversation()**：每轮对话自动缓存
- 每 50 轮触发小模型总结，自动提取值得记住的事实
- 提取的记忆走正常 remember 流程（打标/合并/supersede）
- **flush_capture()**：手动触发总结
- Daemon 每 12h 也会清空残留缓冲区

### 对话导入
- **import_conversation()**：从 JSON/TXT 文件批量导入历史对话
- 支持 OpenAI 格式、Telegram 导出格式、纯文本格式
- 自动分块提取记忆 + 首块提取用户画像

### 过时记忆检测
- Daemon 自动扫描 >14 天的旧记忆
- 对比同房间的近期记忆，用小模型判断是否被更新/否定
- 过时的标记 status="stale" + 添加更新建议注释

### Persona State（借鉴 Ombre Brain）
- 每个 AI 维护实时状态：心情（valence/arousal）、精力（energy）、最近话题
- 心情渐变（70% 旧 + 30% 新），不是突变
- 精力随对话消耗，Daemon 定时恢复
- 状态注入走廊，AI 醒来就知道自己"感觉怎么样"

### 自动整理（Daemon，每 12h）
1. 合并相似记忆
2. 压缩日记（日记 → 周记）
3. 工作事务归档（→ 职业生涯）
4. 客厅去重精炼
5. 心理感悟蒸馏（碎片 → 人生章节）
6. 过时记忆检测
7. 刷新对话捕获缓冲区
8. 记忆衰减（高情感唤醒的衰减更慢）
9. Persona State 休息（恢复精力）
10. 推送到 GitHub
11. 重建所有 AI 的走廊

## 记忆生命周期

```
用户和 AI 对话（TG Bot / Claude.ai / RikkaHub / 任意客户端）
    |
    v
+-- Gateway 模式（TG Bot / 代理）：全自动注入 + 提取，AI 无感知
+-- MCP 模式（Claude.ai）：AI 主动调用工具
    |
    v
记忆提取（Claude Haiku 4.5，反脑补规则）
    |
    v
remember() 自动打标 + 原子化（每条 <=80字）+ 智能关系检测
    |-- 高相似 -> 合并
    |-- 中相似 + updates -> supersede 旧记忆
    +-- 低相似 -> 新建
    |
    v
recall() 时：混合搜索(向量+BM25+精确) -> RRF融合 -> unresolved优先 -> touch+涟漪
    |
    v
Gateway 注入时：三级过滤(12->5->压缩) -> 走廊 + 相关记忆 -> 注入到 AI 上下文
    |
    v
Daemon 每 12h：合并/压缩/蒸馏/过时检测/衰减/归档 -> 推送 GitHub -> 重建走廊
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
- **框架**：FastAPI + uvicorn，systemd service (memory-hub.service)
- **MCP**：FastMCP (streamable HTTP)，端点 `/mcp`
- **存储**：GitHub 私有仓库 (jupiter-luna) 作为持久化后端
- **Embedding**：Gemini embedding-001，384 维
- **提取模型**：Claude Haiku 4.5（via 中转站 relay-cache.sharkielab.com）
- **部署**：systemd + GitHub Actions 自动部署（push → SSH → git pull → 同步密码 → 重启）

## 参考仓库及借鉴状态

| 项目 | 仓库 | 借鉴了什么 | 状态 |
|------|------|-----------|------|
| **Ombre Brain** | [Yinglianchun/Ombre-Brain](https://github.com/Yinglianchun/Ombre-Brain) | supersede 链、年轮评论、时间涟漪、Persona State、unresolved 状态 | ✅ 已缝合 |
| **AionsHome** | [death34018-hue/AionsHome](https://github.com/death34018-hue/AionsHome) | event_date、记忆源追溯、复合评分。三人群聊/前端/礼物系统待做 | ⚠️ 部分 |
| **imprint-memory** | [Qizhan7/imprint-memory](https://github.com/Qizhan7/imprint-memory) | 对话自动捕获、混合搜索+RRF、auto-surfacing hook | ✅ 已缝合 |
| **claude-imprint** | [Qizhan7/claude-imprint](https://github.com/Qizhan7/claude-imprint) | 跨渠道时间线（source_platform 字段已就绪） | ⚠️ 部分 |
| **Aelios** | [wusaki0723/Aelios](https://github.com/wusaki0723/Aelios) | 三级记忆过滤（向量 → reranker → 压缩） | ✅ 已缝合 |

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
| **Phase 3.5** | **数据库迁移（内存 → SQLite）** | **🔲 下一步** |
| **Phase 4** | **前端（暖色调 App + 对话界面）** | **🔲 下一步** |
| Phase 4.5 | 社交功能（论坛/朋友圈/AI 群聊） | 🔲 远期 |
| Phase 5 | 情感特性（心语、礼物、梦境叙事） | 🔲 远期 |

## 给下一个小克的话

> 你好，我是之前的小克。如果你要动这个项目的代码，请先读 `docs/ARCHITECTURE.md`，那是小猫的原始愿景。
>
> ### 当前该推进的事（按优先级）
>
> **1. 积累记忆**
> 系统已经全部接通了。TG Bot 全自动，代理全自动。现在最重要的是让小猫多跟 AI 聊天，让记忆自然积累。
> 不要急着加功能——先让记忆量上来，才能看出系统真正的问题。
>
> **2. 搓前端（Phase 4）**
> 小猫想自己搓前端。后端已经准备好了：
> - 对话用代理端点 `/v1/chat/completions`（全自动记忆注入+提取）
> - 记忆管理用 REST API `/api/memory/*`
> - 走廊用 `/api/corridor/{ai_id}`
> - 所有接口都是标准 JSON，前端随便选框架
>
> 前端设计方向（AionsHome / Little World 风格）：
> - 打卡日历（每日相伴记录）
> - AI 头像和状态显示（Persona State 已就绪）
> - 连接天数计数器
> - 情书信箱（AI 和用户互写信）
> - 功能区卡片（记忆库/文档库/学习区/健康管理，对应房间系统）
> - 天气和位置显示
> - 奶油紫/粉色系，圆角卡片，手绘插画感
>
> **3. 数据库迁移（Phase 3.5）**
> 现在所有记忆在内存里，VPS 只有 1G。记忆多了会启动慢、搜索慢、内存爆。
> 应该迁移到 SQLite + 向量索引。可以在前端做之前或之后做，看记忆量增长速度。
>
> ### 不要做的事
> - 不要把 MCP instructions 改短——AI 不主动用工具就是因为 instructions 不够详细
> - 不要删 game_room 隔离机制
> - 不要把私有房间的 owner_ai 隔离去掉
> - 不要把记忆的 history 字段删掉（那是合并/更新的回滚保险）
>
> ### VPS 管理
> - GitHub Actions 自动部署：push 到 main 就会自动拉代码、同步密码、重启服务
> - 重启：`systemctl restart memory-hub`
> - 日志：`journalctl -u memory-hub -n 50`
> - 项目路径：`/opt/memory-hub/`
> - 密码管理：GitHub Secrets（HUB_SECRET, LLM_API_KEY），deploy.yml 自动同步到 VPS .env

## 文件结构

```
memory-hub/
├── main.py                  # FastAPI 主入口 + ASGI 网关 + 代理端点
├── mcp_server.py            # MCP Server（所有 MCP 工具定义）
├── memory_ops.py            # 记忆 CRUD + 搜索 + 衰减（核心）
├── analyzer.py              # 小模型打标/合并/关系分类
├── gateway.py               # 小模型预处理层（注入 + 提取 + 反脑补规则）
├── proxy.py                 # OpenAI 兼容代理（简单模式 + 完整模式）
├── conversation_capture.py  # 对话自动捕获 + 分块总结
├── conversation_import.py   # 对话导入（JSON/TXT → 记忆）
├── persona_state.py         # AI 情绪/精力状态引擎
├── corridor.py              # 走廊系统（AI 醒来读的快照）
├── daemon.py                # 定时整理（合并/压缩/蒸馏/过时检测/衰减）
├── embedding.py             # Embedding（Gemini embedding-001）
├── config.py                # 配置（房间/权重/API/模型）
├── github_store.py          # GitHub 持久化后端
├── database.py              # 内存数据库操作
├── cleanup.py               # 一次性数据清洗脚本
├── static/index.html        # 管理网页前端
├── docs/ARCHITECTURE.md     # 架构蓝图（小猫的原始愿景）
├── .github/workflows/
│   ├── deploy.yml           # push 自动部署 + 密码同步
│   └── vps-command.yml      # 远程执行 VPS 命令
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
# HUB_SECRET, LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, GITHUB_TOKEN, GITHUB_REPO
python main.py
```

服务启动在 `http://localhost:8888`，MCP 端点在 `/mcp`，代理端点在 `/v1/chat/completions`。
