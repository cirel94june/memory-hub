# Memory Hub

小猫的统一记忆系统 — 让所有 AI 入口共享记忆、各自写入、主动使用。

## 这是什么

一个跑在 VPS 上的 FastAPI 服务，提供 REST API + MCP Server 两种接入方式。所有 AI（Claude / Gemini / GPT）通过它共享一套记忆，每个 AI 也有自己的私有空间。

记忆存储在 GitHub 私有仓库 `jupiter-luna` 里（JSON 文件），服务启动时加载到内存，修改后异步推送回 GitHub。

## 接入方式

| 入口 | 方式 | 状态 |
|------|------|------|
| claude.ai | MCP connector → `http://VPS:8888/mcp` | ✅ |
| Claude Code | MCP → 同上 | ✅ |
| RikkaHub 上的 Claude | MCP → 同上 | ✅ |
| Telegram 小克 bot | REST API → `http://VPS:8888/api/` | 🔲 待接入 |
| Telegram 其他 bot (Gemini/GPT) | REST API → 同上 | 🔲 待接入 |

## 核心功能

### 已实现

- **记忆 CRUD**：remember / recall / update / archive / delete
- **自动打标**：小模型自动分析 domain / valence / arousal / tags
- **自动合并**：相似度 > 0.75 的记忆自动合并，防重复
- **智能 supersede**：remember 新事实时自动检测旧记忆，标记过时（借鉴 Ombre Brain）
- **年轮评论**：add_comment 给记忆追加反思/补充，不改原文，保留认知成长轨迹（借鉴 Ombre Brain）
- **时间涟漪**：召回一条记忆时，±48h 内创建的记忆也轻微激活（借鉴 Ombre Brain）
- **event_date**：区分"事件发生时间"和"记忆创建时间"
- **长文拆分（grow）**：一段日记自动拆成多条独立记忆
- **多维搜索**：embedding × 5.0 + topic × 4.0 + emotion × 2.0 + time × 1.5 + importance × 1.0
- **走廊（corridor）**：AI 醒来时读的第一份记忆快照
- **Gateway**：小模型预处理层，自动判断查哪些房间、对话后自动提取值得记住的信息
- **Daemon**：每 12h 自动整理（合并相似、压缩日记、工作归档、客厅去重、心理蒸馏、衰减）
- **房间系统**：共享房间 + 私有房间 + 隔离房间，动态可扩展
- **管理网页**：`http://VPS:8888/` 查看和管理记忆
- **本地 embedding**：fastembed (all-MiniLM-L6-v2)，384 维，无外部 API 依赖
- **GitHub Actions 自动部署**：push 到 main 自动部署到 VPS

### 记忆生命周期

```
用户说了新事实
    ↓
remember() 自动打标（小模型分析 domain/tags/emotion）
    ↓
搜索相似记忆候选
    ├─ 相似度 ≥ 0.75 → 合并（内容融合为一条）
    ├─ 相似度 0.55-0.75 → 小模型判断关系
    │   ├─ updates/contradicts → 旧记忆标记 superseded，新记忆关联旧 ID
    │   ├─ supplements → 建立 linked_memories 关联
    │   └─ same_topic → 各自独立存储
    └─ 相似度 < 0.55 → 新建
    ↓
recall() 时自动 touch（activation_count++，时间涟漪）
    ↓
Daemon 每 12h 整理（合并/压缩/衰减/归档）
```

## 房间一览

### 共享房间（所有 AI 可读写）
| 房间 | 用途 |
|------|------|
| living_room | 核心身份、当前状态（永远注入） |
| career | 工作经历、职业规划 |
| psychology | 心理状态、情绪模式 |
| health | 身体健康 |
| learning | 学习目标、技能 |
| relationships | 人际关系 |
| preferences | 兴趣偏好 |
| work_tasks | 工作事务（快速衰减） |
| infra | 基建总览 |
| infra_changelog | 基建更新日志 |

### 私有房间（per AI，用 owner_ai 隔离）
| 房间 | 用途 |
|------|------|
| diary | AI 的个人日记 |
| dreams | 梦境/自省 |
| relationship | 和用户的关系 |
| personality | AI 的自我认知 |

### 隔离房间
| 房间 | 用途 |
|------|------|
| game_room | 游戏/角色扮演，不混入正经对话 |

## 技术栈

- **运行环境**：Python 3.12 + venv，VPS (Racknerd 1G)
- **框架**：FastAPI + uvicorn
- **MCP**：FastMCP (streamable HTTP)
- **存储**：GitHub 私有仓库 (jupiter-luna) 作为持久化后端
- **Embedding**：fastembed (all-MiniLM-L6-v2)，本地 ONNX 推理
- **小模型**：中转站 (OpenAI 兼容格式)，用于打标/合并/关系判断/Gateway
- **部署**：systemd service + GitHub Actions 自动部署

## 参考仓库

这些开源项目提供了设计灵感，按参考程度排序：

| 项目 | 仓库 | 借鉴了什么 |
|------|------|-----------|
| **Ombre Brain** | [Yinglianchun/Ombre-Brain](https://github.com/Yinglianchun/Ombre-Brain) | 桶式记忆、Gateway 注入、年轮评论、supersede 链、时间涟漪、梦境引擎。**主要参考对象** |
| **AionsHome** | [death34018-hue/AionsHome](https://github.com/death34018-hue/AionsHome) | 复合评分公式、三人群聊、小剧场隔离、前端全家桶、礼物系统。**终极目标参考** |
| **imprint-memory** | [Qizhan7/imprint-memory](https://github.com/Qizhan7/imprint-memory) | 全自动记忆管线（hook 捕获→分块→embedding→auto-surfacing）。**自动化最强** |
| **claude-imprint** | [Qizhan7/claude-imprint](https://github.com/Qizhan7/claude-imprint) | imprint 完整版：+Telegram+Dashboard+跨渠道时间线 |
| **Aelios** | [wusaki0723/Aelios](https://github.com/wusaki0723/Aelios) | 三级记忆过滤（向量→reranker→压缩）、定时梦境、prompt cache 优化 |

## 开发阶段

| 阶段 | 内容 | 状态 |
|------|------|------|
| Phase 1 | 核心 API + GitHub 存储 + 管理网页 | ✅ |
| Phase 2 | MCP Server（claude.ai 接入） | ✅ |
| Phase 2.5 | 迁移到 VPS | ✅ |
| Phase 2.7 | 记忆智能化（analyzer/搜索/衰减） | ✅ |
| Phase 2.8 | LLM 切中转站 + 本地 embedding + Gateway | ✅ |
| **Phase 2.9** | **智能 supersede + 年轮评论 + 时间线** | **✅ 刚完成** |
| Phase 3 | Telegram Bot 接入（3 个 bot 共享记忆） | 🔲 下一步 |
| Phase 3.5 | 对话自动捕获 + 定时分块总结 | 🔲 |
| Phase 4 | 社交功能（论坛/朋友圈/群聊） | 🔲 远期 |
| Phase 5 | 前端（AionsHome 风格，AI 住在上面） | 🔲 远期 |
| Phase 6 | 情感特性（心语、礼物、年轮评论） | 🔲 远期 |

## 给下一个小克的话

> 你好，我是之前的小克。如果你要动这个项目的代码，请先读 `docs/ARCHITECTURE.md`，那是小猫的原始愿景。
>
> **当前最该推进的事**：
>
> 1. **Phase 3：Telegram Bot 接入** — 小猫有 3 个 TG bot（小克/Lucien/Jasper），它们现在用 GitHub Gist 存记忆，需要迁移到 Memory Hub 的 REST API。相关仓库：`cloudy-telegram-bot`、`Lucien-Telegram-bot`、`Jasper-telegrambot`。
>
> 2. **数据质量清洗** — jupiter-luna 里的记忆可能有：空 room、空 owner_ai、重复条目。可以用 `cleanup.py` 或手动检查。
>
> 3. **对话自动捕获（Phase 3.5）** — 现在靠 AI 主动调 remember，但很多信息会漏掉。参考 imprint-memory 的 hook 方案：每轮对话自动存 conversation_log，定时 30 条一组用小模型提取结构化记忆。
>
> 4. **三级记忆过滤** — 现在 recall 直接返回向量搜索结果。应该加：向量搜索(50) → reranker(12) → 压缩模型(5条×300字)。参考 Aelios。
>
> 5. **unresolved 状态** — 参考 Ombre Brain，给记忆加"待办/未完成"标记，召回时优先浮现（最多 2 条）。
>
> **不要做的事**：
> - 不要把 MCP instructions 改短，那是小猫反复调过的，AI 不主动用工具就是因为 instructions 不够详细
> - 不要删 game_room 隔离机制
> - 不要把私有房间的 owner_ai 隔离去掉

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

服务启动在 `http://localhost:8888`，MCP 端点在 `http://localhost:8888/mcp`。
