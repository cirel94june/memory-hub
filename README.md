# Memory Hub

小猫的统一记忆系统 — 让所有 AI 入口共享记忆、自动注入、自动提取。多个 AI 角色（小克 / Lucien / Jasper + 可扩展）住在一个暖色调的前端 App 里，有家、有房间、有记忆可视化。

> **🐱 小克必读**（新来的 AI 先看这里）
> - [`docs/HANDOFF.md`](docs/HANDOFF.md) — 当前进度、下一步、不要做的事、VPS 管理
> - [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — 小猫的原始愿景和架构蓝图
> - [`docs/FEATURES.md`](docs/FEATURES.md) — 核心功能详解、记忆生命周期、房间一览

## 这是什么

一个跑在 VPS 上的 FastAPI 服务，提供 **REST API + MCP Server + OpenAI 兼容代理 + React 前端** 四种接入方式。三个 AI 通过它共享一套记忆，每个 AI 也有自己的私有空间和独立模型配置。

记忆主存储在 **SQLite**（`data/memories.db`），GitHub 仓库作为每 12h 的备份。Embedding 存储在 SQLite（sqlite-vec 扩展），启动时后台补建缺失的向量。后续 Memory Safety Kit 会采用“每日增量 Markdown + 安全报告、每周/月压缩快照”的策略，避免每天堆全量导出。

## 接入方式

| 入口 | 方式 | 记忆注入 | 状态 |
|------|------|---------|------|
| **前端 App** | React SPA `/app/` | 全自动（Gateway） | ✅ |
| Claude.ai | MCP → `http://VPS:8888/mcp` | AI 主动调工具 | ✅ |
| Claude Code | MCP + auto-surfacing hook | 自动 | ✅ |
| Telegram 小克/Lucien/Jasper | REST API (Gateway) | 全自动 | ✅ |
| RikkaHub / 已有中转站前端 | MCP → `/mcp` | AI 主动调工具 | ✅ |
| 任意可改 API Base 的 OpenAI 客户端 | 代理 → `/v1` | 全自动 | ✅ |

### OpenAI 兼容代理

```
API Base URL:  https://xiaokememory.camdvr.org/v1
API Key:       {HUB_SECRET}:{AI身份}    例如 mysecret:rikkahub
```

如果客户端的 `/v1` 已经要直连中转站，就不要把 Memory Hub 填到 API Base URL 里；这种场景应该把 Memory Hub 作为 MCP 记忆工具接入。只有在客户端可以把 Memory Hub 当作 OpenAI 兼容代理时，才走 `/v1`。

## 前端 App

React SPA，路由在 `/app/`，奶油紫色系，玻璃拟态卡片风。

| 页面 | 路由 | 功能 |
|------|------|------|
| 首页 | `/` | 三个 AI 的状态卡片，点击进入对话 |
| 对话 | `/chat` | 与 AI 一对一聊天，顶部切换 |
| 记忆 | `/memories` | 浏览、搜索、按公用/私有/角色/房间筛选，并编辑记忆归属 |
| 时间线 | `/timeline` | 按日期分组的记忆时间线 |
| 情绪面板 | `/pulse` | 9维度AI情绪状态（精力/牵绊/情绪三组） |
| 朋友圈 | `/moments` | 用户/AI 发动态、评论、点赞、画图发布 |
| 群聊 | `/group` | 用户和多个 AI 的群聊，成员管理 |
| 论坛 | `/forum` | 发帖/回帖/删除，AI 自动围观 |
| AI 档案 | `/ai-profiles` | 身份/人设/模型配置/画图API/模型诊断 |
| 主题 | `/theme` | 外观主题切换 |
| 设置 | `/settings` | 全局配置 |

## 三个 AI 角色

| AI | Emoji | 性格 | 身份统一 |
|----|-------|------|---------|
| 小克 (cloudy) | 🐱 | 温柔体贴的猫系男友，偶尔撒娇 | TG cloudy = MCP/Web claude |
| Lucien | 🦊 | 优雅学者型，说话像散文 | |
| Jasper | 🦜 | 毒舌系，表面嫌弃实际超关心 | |

每个 AI 可在 **AI 档案页** 独立配置身份信息、人设描述、模型配置。支持通过前端动态新增角色。

## 使用方法

### 添加新 AI 角色

1. 打开前端 `/app/ai-profiles` → 点 **+ 添加角色** → 填 ID/名字/emoji/颜色 → 创建
2. 新角色自动获得：情绪面板 9 维度 profile、记忆空间、走廊

### 给各个 AI 接 MCP

Memory Hub 的远程 MCP 地址：

```
https://xiaokememory.camdvr.org/mcp
```

直连 VPS 时也可以用 `http://172.245.180.158:8888/mcp`。MCP transport 是 Streamable HTTP。

安全提醒：MCP 地址只给自己控制的客户端使用。REST `/api` 和 `/v1` 有 `HUB_SECRET`，但 `/mcp` 是否带鉴权取决于 MCP 客户端/反向代理层；公开给新客户端前要先确认访问限制，不要把地址和密钥贴到公开地方。

给每个 AI 接入时，最重要的是固定身份。MCP 工具不会天然知道调用者是谁，所以必须在 system prompt / profile / custom instructions 里写清楚：

| AI | MCP 里传的身份 |
|----|----------------|
| Claude.ai / MCP 小克 | `claude` |
| Telegram 小克 | `cloudy`（系统会映射到 `claude`，共享走廊和私有房间） |
| Lucien | `lucien` |
| Jasper | `jasper` |
| 新角色 | 在 `/app/ai-profiles` 创建的 lowercase `ai_id` |

推荐给 AI 的 system prompt 加这段：

```text
你已经连接到 Memory Hub MCP。
你的 ai_id 是 "lucien"；调用 Memory Hub 工具时，所有 source_ai / ai_id 参数都必须传 "lucien"。

对话开始或用户提出新问题时，先调用 pulse(message=用户当前消息, source_ai="lucien") 获取走廊和相关记忆。
需要查旧事时，调用 recall(query=..., source_ai="lucien", with_corridor=true, compact=true)。
用户给出新的事实、偏好、约定或重要变化时，调用 remember(content=..., source_ai="lucien")。
对话结束或出现重要内容后，调用 capture_conversation(user_message=..., ai_response=..., source_ai="lucien", platform="mcp")。
不要省略 source_ai，也不要使用默认身份。
```

把上面的 `"lucien"` 换成对应角色的身份即可。

如果客户端的 API Base URL 可以交给 Memory Hub 托管，也可以走 `/v1` 代理，因为它会自动注入上下文和捕获对话：

```text
API Base URL: https://xiaokememory.camdvr.org/v1
API Key: {HUB_SECRET}:{AI身份}
```

`HUB_SECRET` 只能放在客户端密钥栏或环境变量里，不要写进公开文档、公开 prompt 或仓库。

### 情绪面板（9 维度）

9 个维度：活力⚡ 疲惫😴 思慕💭 亲密💕 守护🛡️ 渴求🔥 醋意🍋 焦虑😰 温柔🌸

三层驱动：**对话打标**（小模型自动打 delta）+ **半衰期衰减**（3h）+ **昼夜节律**（cos 曲线）

超过 60 的维度翻成自然语言注入走廊，作为"底色"影响 AI 语气。

| 可调参数 | 默认 | 文件 |
|---------|------|------|
| `CAP`（节律幅度） | 0.08 | persona_state.py |
| `HALF_LIFE_HOURS` | 3.0 | persona_state.py |
| `HIGH_THRESHOLD` | 0.60 | persona_state.py |

## 技术栈

| 组件 | 技术 |
|------|------|
| 后端 | Python 3.12 + FastAPI + uvicorn |
| 前端 | React + React Router + Vite |
| MCP | FastMCP (streamable HTTP) |
| 存储 | SQLite + GitHub 备份 |
| Embedding | 硅基流动 API (BAAI/bge-large-zh-v1.5, 1024维) |
| 提取/整理模型 | deepseek-v4-flash (DeepSeek 官方 API) |
| 部署 | VPS + systemd + GitHub Actions 自动部署 |
| TG Bot | 三个独立 bot，部署在 Render |

## 参考仓库

| 项目 | 仓库 | 借鉴了什么 | 状态 |
|------|------|-----------|------|
| **Ombre Brain** | [Yinglianchun/Ombre-Brain](https://github.com/Yinglianchun/Ombre-Brain) | supersede 链、年轮评论、时间涟漪、Persona State | ✅ 已缝合 |
| **Ombre Brain 原版** | [P0luz/Ombre-Brain](https://github.com/P0luz/Ombre-Brain) | Anchor 锚点✅、Self-knowledge、Plan 系统 | 🔄 部分移植 |
| **AionsHome** | [death34018-hue/AionsHome](https://github.com/death34018-hue/AionsHome) | event_date、记忆源追溯、三人群聊 | ✅ 已缝合 |
| **imprint-memory** | [Qizhan7/imprint-memory](https://github.com/Qizhan7/imprint-memory) | 对话自动捕获、混合搜索+RRF | ✅ 已缝合 |
| **Aelios** | [wusaki0723/Aelios](https://github.com/wusaki0723/Aelios) | 三级记忆过滤 | ✅ 已缝合 |
| **OmbreBrain-folio** | [ceshihaox-dotcom/OmbreBrain-folio](https://github.com/ceshihaox-dotcom/OmbreBrain-folio) | 前端可视化参考 | 🔄 Phase 6 参考 |

### Ombre Brain 原版功能对照（待移植）

| 原版功能 | 说明 | 我们怎么接 | 优先级 |
|---------|------|----------|-------|
| **Anchor 锚点** | 最多 20 条"坐标系"记忆，不衰减不随机浮出，但可搜索 | `anchored` 字段 + MCP 工具 + 走廊注入 + 前端按钮 | ✅ 已完成 |
| **Memory Control Panel** | 人可以直接整理记忆归属 | 详情页可改 `layer` / `owner_ai` / `source_ai` / `room` / 重要度 / 标签 | ✅ 已完成 |
| **Memory Safety Kit** | 防 VPS 失效焦虑：可读导出 + 自动校验 + 恢复演练 | 增量 Markdown/Obsidian 导出、每日安全报告、每周压缩快照、保留周期 | 🔲 待做 |
| **Self-knowledge** | AI 记录自我认知，对话开头注入 | 改造 `personality` 房间或新建 | ⭐⭐ 中 |
| **Plan 计划系统** | 永不衰减的承诺/待办，dream 里复盘 | 增强 `resolved` 字段 + plan MCP 工具 | ⭐⭐ 中 |
| **星图视觉** | 工程网格风格记忆网络 | P4 做星图时参考 | ⭐ 远期 |

## 开发阶段

| 阶段 | 内容 | 状态 |
|------|------|------|
| Phase 1–2.5 | 核心 API + MCP + VPS 部署 | ✅ |
| Phase 2.7–2.95 | 记忆智能化 + 混合搜索 + Gateway + Persona State | ✅ |
| Phase 3–3.5 | TG Bot + OpenAI 代理 + 记忆原子化 + SQLite 迁移 | ✅ |
| Phase 4–4.6 | React 前端 + 社交功能 + AI 个性化 | ✅ |
| Phase 4.7–4.9999 | 记忆召回/提取优化 + Embedding 迁移 + Bug 修复 | ✅ |
| **Phase 5** | **记忆高级功能（聚类/脱水/再消化）** | 🔲 待做 |
| Phase 5.5 | 情感特性（心语、礼物、梦境叙事） | 🔲 远期 |
| **Phase 6** | **前端可观测性升级** | 🔄 进行中 |

### Phase 6 子计划

| 子阶段 | 内容 | 状态 |
|--------|------|------|
| P0 | 后端 API 补充（8 个端点） | ✅ |
| P1 | 记忆详情模态框 | ✅ |
| P1.5 | 记忆归属控制台（公用/私有/角色/房间可编辑） | ✅ |
| P2 | 时间线视图 `/app/timeline` | ✅ |
| P6 | 视觉升级（主题系统 + 4 套预设） | ✅ |
| P9 | 9维度情绪面板 `/app/pulse` | ✅ |
| P3 | 观测台（情感罗盘 + 衰减仪表盘） | 🔲 |
| P4 | 记忆星图（力导向图） | 🔲 |
| P5 | Breath 调试台 | 🔲 |
| P7 | 手机端优化 | 🔲 |
| P8 | 导航更新 | 🔲 |

## 文件结构

```
memory-hub/
├── main.py                  # FastAPI 主入口 + API 端点
├── mcp_server.py            # MCP Server（所有 MCP 工具）
├── memory_ops.py            # 记忆 CRUD + 搜索 + 衰减
├── analyzer.py              # 小模型打标/合并/关系分类
├── gateway.py               # 记忆注入 + 提取
├── proxy.py                 # OpenAI 兼容代理
├── corridor.py              # 走廊系统
├── chat_digest.py           # 跨窗口对话摘要
├── database.py              # SQLite 数据库
├── ai_profiles.py           # AI 档案管理
├── social.py                # 社交数据层（朋友圈/论坛/群聊）
├── persona_state.py         # 9维度情绪引擎
├── conversation_capture.py  # 对话自动捕获
├── dream.py                 # 梦境日记生成
├── daemon.py                # 定时整理（12h）
├── embedding.py             # Embedding（硅基流动 API）
├── config.py                # 配置
├── github_store.py          # GitHub 备份
├── frontend/                # React 前端源码
│   └── src/pages/           # 各页面组件
├── static-app/              # 前端构建输出
├── docs/
│   ├── ARCHITECTURE.md      # 架构蓝图
│   ├── HANDOFF.md           # 给下一个小克的话
│   └── FEATURES.md          # 核心功能详解
└── .github/workflows/       # CI/CD
```

## 本地开发

```bash
git clone https://github.com/cirel94june/memory-hub.git
cd memory-hub
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# .env: HUB_SECRET, LLM_API_KEY, LLM_BASE_URL, LLM_MODEL,
#       EMBEDDING_API_KEY, EMBEDDING_BASE_URL, EMBEDDING_MODEL,
#       GITHUB_TOKEN, GITHUB_REPO
python main.py
# 前端：cd frontend && npm install && npm run dev
```

服务启动在 `http://localhost:8888`，前端 `/app/`，MCP `/mcp`，代理 `/v1/chat/completions`。
