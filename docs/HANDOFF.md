# 给下一个小克的话

> 你好，我是之前的小克。如果你要动这个项目的代码，请先读 `docs/ARCHITECTURE.md`，那是小猫的原始愿景。

## 当前该推进的事（按优先级）

### 1. 前端可观测性升级（Phase 6） ← 当前优先

P0（后端API）+ P1（记忆详情模态框）+ P6（视觉升级/主题系统）+ P2（时间线视图）+ P9（9维度情绪面板）已完成。
**动态 AI 档案** ✅ 已完成（2026-06-23）：全前端去掉硬编码 AI 列表，改用 AIContext 从 /api/ai-profiles 动态加载。
代理 proxy.py 支持 per-AI 模型配置（每个 AI 用自己的中转站/模型/API key）。
下一步：P3 观测台（情感罗盘+衰减仪表盘）或 P4 记忆星图（力导向图）。
deploy.yml 已加前端自动构建，push 后 CI 会自动 npm build，不需要手动到 VPS 构建。

**部署边界提醒（2026-06-30）**：
- Memory Hub 本体跑在 VPS：FastAPI、React 静态包、MCP、OpenAI 兼容代理、SQLite。
- Telegram 三个 bot 已搬到 GitHub + Render 免费层，不在 VPS 上。
- 如果 TG bot 的记忆/情绪没有联动，优先检查 Render 侧 bot 仓库是否调用 `/api/gateway/context` 和 `/api/capture/log`，以及传入的 `ai_id` 是否是 `cloudy` / `lucien` / `jasper`。Memory Hub 的 `capture/log` 会缓冲提取记忆，并在 bot 实际回复时推动情绪面板。
- 2026-07-03 Gateway 省 token 策略：`build_context()` 默认 `compact=True`，优先注入 high/medium 置信度记忆，通常只取 3 条、每条约 180 字，不默认带 `source_context`。用户消息包含“原话/当时/细节/具体/为什么/来源/上下文”等线索时自动 `detail_mode=True`，可取 5 条并附短原文。接口返回 `estimated_tokens` / `memory_count` / `detail_mode` 方便诊断。
- 2026-07-03 Ombre-style 衰减解释：新增 `memory_ops.explain_decay()`，把每条记忆分到 `protected` / `long_term` / `short_term` / `watch`，并返回 `protections`、`pressures`、`days_to_archive`、`recommendation` 和完整因子。`/api/memory/{id}/detail` 与 `/api/memory/decay-scores` 已接入这套解释。原则：不让小模型硬拦截写入，而是靠低重要度、短期池、未召回自动捕获的加速衰减来清理废记忆。
- 2026-07-03 观测台：新增 `/app/observatory`，导航中显示“观测台”。当前展示 daemon 最近步骤、衰减分层统计、临近归档、短期池和观察中记忆。后续 Dream Context、人生章节 skip 诊断、召回 token 诊断都应接到这里，不要散落到设置页。

**关于 P9 情绪面板**：
- `persona_state.py` 已重写为 9 维度引擎（活力/疲惫/思慕/亲密/守护/渴求/醋意/焦虑/温柔）
- 三层驱动：对话打标（gateway + MCP 两条路径都会触发）、半衰期衰减（3h）、昼夜节律（cos 曲线）
- display > 0.60 的维度自动翻成自然语言注入走廊（当"底色"影响语气，不念数字）
- 三个 AI 各有独立参数（phase/amp/defaults），新角色自动用默认 profile
- MCP 指引已加身份识别规则（每个 AI 必须传 source_ai）
- 前端支持动态角色——AI 档案页可直接添加/删除角色，情绪面板自动显示

### 1.5. Ombre Brain 原版功能移植

**Anchor 锚点** ✅ 已完成（2026-06-23）：
- `database.py` 新增 `anchored` 列（自动迁移）
- `memory_ops.py`：anchor_memory / release_anchor / list_anchors，上限 20 条
- 衰减流程 `run_decay()` 自动跳过锚点记忆
- `corridor.py`：走廊新增"【锚点·不变的事】"段落
- MCP 工具：`anchor` / `release_anchor`
- REST API：`POST/DELETE /api/memory/{id}/anchor` + `GET /api/anchors`
- 前端：记忆详情弹窗有锚定按钮和蓝色徽章

**待做**：Self-knowledge（改造 personality 房间或新建）、Plan 增强（plan MCP 工具 + dream 联动）。

**2026-07-02 Ombre Brain 二改核查**：
- [Yinglianchun/Ombre-Brain](https://github.com/Yinglianchun/Ombre-Brain) 是 [P0luz/Ombre-Brain](https://github.com/P0luz/Ombre-Brain) 的二次开发版，不能简单视为“已全部缝合”。当前 Memory Hub 只实现了其中一部分思想：Gateway 自动注入、锚点、年轮评论/补充、Persona State、前端记忆编辑。
- 最值得后续拆出来做的是：Portrait/Handoff（醒来画像）、Raw Event Vault（原文保险箱）、自动写入门卫（novelty/durability/repeat gate）、Dream Context（梦境浮现/注入开关）、Memory Moment/Edge/Word Map Lite（可解释记忆图）、Relationship Weather（日印象）、Dashboard 高级编辑/召回诊断。
- 不建议整套替换 Memory Hub。Memory Hub 已经有 SQLite、MCP、OpenAI 兼容代理、React 前端、社交模块和 GitHub 备份；二改仓库更适合当功能清单和交互参考。
- 注意二改 README 里强调的边界：Gateway 自动注入、脚本/函数端点、MCP 工具不是同一种入口；RikkaHub 这类 `/v1` 要接中转站的前端，仍应把 Memory Hub 接成 MCP，不要强行让 Memory Hub 托管 API Base。

**自动后台功能核查**：
- **人生章节不是没写，而是条件很苛刻**：`daemon.py::distill_psychology()` 会在 daemon 第 5 步处理 `room=psychology`、`category!=life_chapter`、30 天以上的活跃记忆。需要至少 3 条旧心理记忆，且同一个月份至少 2 条，LLM 成功后才写入 `category=life_chapter` 并归档原碎片。下一步应做“人生章节诊断”API/前端，显示跳过原因。
- **梦境日记不是每天必定生成**：`dream.py::generate_dreams()` 在 daemon 第 10.8 步运行，但要求当天同一 AI 至少 2 条 `chat_digests`，且当天还没有梦。它写入 `layer=private`、`room=diary`、`category=dream`。如果 chat_digest 没生成、日期按 UTC/Asia-Shanghai 不一致、或对话不足 2 条，就会跳过。下一步应补 dream skip log、前端梦境入口、以及 Dream Context 是否注入/浮现的开关。
- **2026-07-02 梦境材料池修正**：`/api/capture/log` 现在也会顺手写 `chat_digests`，所以 Telegram bot 只走 capture/log 时也能进入梦境摘要池。`dream.py` 在当天摘要不足 2 条时，会从最近 72 小时内仍为 active、importance >= 0.5、非 infra/work_tasks/非 dream 的私聊/小群有效记忆里补“daytime residue”；至少 3 条才生成，避免纯流水账做梦。
- **MCP 的 `dream()` 工具是另一条路**：它是 AI 主动自省时写入 `room=dreams`，和 daemon 夜梦不同。后续需要统一到一个“梦境/自省”视图，不要让 diary/dreams 两个房间割裂。

### 2. 记忆相似聚类 + 脱水压缩（Phase 5）

参考 PDF 设计文档里的思路：相似记忆自动聚类合并、旧记忆脱水压缩节省空间、日记提取和再消化。
系统已经有基础的合并机制（remember 时相似度检测），但需要更系统的聚类和压缩。

### 3. 社交系统 🔧 进行中

**已完成**：
- 数据层 `social.py` 完整（posts/comments/groups/messages CRUD）
- 11 个 Social API 路由已在 main.py 注册并工作
- `_social_call_llm()` 支持 per-AI 模型配置 + 别名解析 + 身份隔离 system prompt + 走廊记忆注入
- 朋友圈/论坛：用户评论时帖主 AI 自动回复（无需 @）；@提及可额外呼唤其他 AI
- 用户发帖时 1-3 个随机 AI 自动评论围观
- 朋友圈和论坛都有删除功能（帖子+评论）
- 群聊：建群时可选成员，聊天界面有成员管理面板，AI 自动回复
- 前端 @mention 自动补全（输入 @ 弹出 AI 列表）

**2026-06-26 修复**：
- **OOC 修复**：`get_profile()` 改为合并所有别名 profiles（之前 cloudy 存名字/人设，claude 存模型配置，互相看不到）
- **社交 system prompt 强化**：注入走廊记忆，AI 知道和用户的关系，不再 OOC
- **聊天消息消失修复**：ChatPage.jsx 重写 SSE 处理（buffer、error event 检测、AbortController）
- **Proxy 错误处理**：upstream 错误返回 SSE error event，不再静默吞掉
- **模型诊断面板**：AI 档案页可一键检查所有 AI 实际用的模型配置（绿=专属/红=全局默认）

**画图功能** ✅（2026-06-26）：
- `image_gen.py`：可配置画图 API（base_url/api_key/model），存储在 GitHub `_config/image_api.json`
- 后端 `/api/draw` 端点 + `/api/image-config` GET/PUT + `/uploads` 静态文件服务
- AI 自主画图：system prompt 含 `[draw:描述]` 能力提示，AI 自行决定是否画图
- 社交场景：`_social_call_llm` 自动处理 draw 标签（服务端），生成图片存本地
- 聊天场景：proxy 注入 draw hint，前端检测 `[draw:xxx]` 标签后调 `/api/draw`，替换为图片
- 前端：聊天页手动画笔按钮、朋友圈「画图发布」、`[img]url[/img]` 渲染组件
- 配置入口：AI 档案页底部「画图 API 配置（全局共用）」

**待做**：AI 自主发帖（定时或 event-driven）、SSE 流式 AI 回复（参考 AionsHome 模式）

### 4. API 费用优化

Gateway 已优化为 0 次 LLM 调用做 recall（直接 RRF 排序取 top 5），post-process 提取 1 次 LLM。
模型用 deepseek-v4-flash（via DeepSeek 官方 API，¥1/M input / ¥2/M output，约 ¥0.1/天）。
Embedding 用硅基流动 API（BAAI/bge-large-zh-v1.5），免费额度内基本够用。

### 已完成（仅供参考）

- **TG Bot 跨聊天感知** ✅ — bot.py 的 `build_cross_chat_context()`，Hub 侧 `chat_digest.py` 生成跨窗口摘要
- **梦境日记** ⚠️ — `dream.py` 已接入 daemon（步骤 10.8），但只有当天同一 AI 至少 2 条 `chat_digests` 且当天未生成过梦时才会写入；现在缺 skip 诊断、梦境入口和 Dream Context 注入/浮现开关

## 不要做的事

- 不要把 MCP instructions 改短——AI 不主动用工具就是因为 instructions 不够详细
- 不要删 game_room 隔离机制
- 不要把私有房间的 owner_ai 隔离去掉
- 不要把记忆的 history 字段删掉（那是合并/更新的回滚保险）
- 不要把记忆提取模型换回 Haiku（会拒绝恋爱场景 + 中文输出英文）或中转站（不稳定，超时严重）或 Qwen 72B via SiliconFlow（太贵，6500次调用花了15元）
- 不要把 Gateway 改回 LLM room-judge + reranker（会导致 >15s 超时，bot 报 888 错误，reranker 还会把趣味/梗记忆过滤掉）
- 不要把 AI_ALIASES 去掉——cloudy(TG小克) 和 claude(MCP/Web小克) 必须是同一个身份，共享走廊和私有房间
- 小克别名合并要区分用途：名字/人设优先读 `cloudy` 这份小克档案；模型配置优先读实际调用的 id（如 `claude`），再 fallback 到别名。否则用户在 `claude` 上配置的中转站 API 会被 `cloudy` 的旧配置盖掉，导致聊天/群聊/朋友圈/论坛 OOC。
- RikkaHub 这类 `/v1` 已经要直连中转站的前端，应该把 Memory Hub 接成 MCP 记忆工具；只有客户端可以让 Memory Hub 托管 API Base URL 时，才走 `/v1` 代理。
- Pulse 打标默认应该保守：普通闲聊输出 `{}`，不要因为对话继续就加“活力”；`gateway._sanitize_pulse_bumps()` 会过滤纯活力误判。TG bot 的 `/api/capture/log` 只排队 `_tag_pulse()`，不要再额外固定 `update_after_conversation()`。
- 2026-06-30 已降低 `persona_state.py` 的活力默认值和昼夜节律 `CAP`（0.12 → 0.08），并在 `load_state()` 里给旧状态加了活力基线上限，避免 Jasper/小克在没有真实情绪事件时也长期高活力。
- 前端私聊页当前使用浏览器 localStorage 持久化历史（`mh-chat-conversations-v1`），支持选择消息删除和清空当前 AI 聊天；服务端多会话/统一时间线还没做。
- 记忆控制台已打通归属编辑：详情页可改 `layer`、`owner_ai`、`source_ai`、`room`、重要度和标签；列表页显示公用/私有、归属 AI、来源角色，并支持 `layer` 过滤。不要再把 AI 档案里的记忆入口做成孤立页面，应统一跳到 `/memories?ai=...` 或复用同一套列表/详情组件。
- Memory Safety Kit 下一步优先做轻量增量导出：每日安全报告 + 新增/更新记忆 Markdown；每周/月再压缩 SQLite 快照。不要每天无脑生成全量 Markdown 堆积；低重要度、已归档、社交闲聊类默认不进长期 Obsidian 导出。
- 用户明确希望数据类内容以 GitHub 云端为准，不要只放本地。Obsidian 只是阅读入口：导出 Markdown 推到 GitHub 私有仓库或专门目录，电脑上 clone/pull 后用 Obsidian “Open folder as vault” 打开；不需要 Obsidian 账号。导出要按 memory id / updated_at 增量去重，同一条记忆更新同一个文件。
- 记忆字段不要混用：`source_ai` 是捕获来源，不是可见范围；`owner_ai` 只用于 `layer=private` 的私有归属；`layer=shared` 是公共可见。前端按角色筛选时用 `/api/memory/list?ai_id=...`，不要再只用 `source_ai=...`，否则会漏掉私有归属和 cloudy/claude 别名。
- Memory Hub 自己的 `/app/chat` 走 `/v1` 代理，必须注入 AI 档案 persona；只连上 per-AI API 不代表角色会自动稳定，若漏掉 persona 就会像默认 LLM 在扮演。
- 时区规则：数据库继续存 UTC；用户可见的日期、bot 提取 prompt 的“今天”、时间线/日历/按日期筛选统一用 `time_utils.py` 的 `Asia/Shanghai`。不要依赖 VPS 系统时区，否则中国晚上会被模型理解成差 8 小时的旧记忆。
- 公共重复记忆清理走 `memory_ops.deduplicate_public_memories()` / `/api/memory/deduplicate-public`，只处理 `layer=shared`、无 `owner_ai`、同房间同分类的重复；不要自动归档私有记忆。
- 一对一私聊 `chat_type=private` 才能自动写入 `layer=private + owner_ai`；`private_group` 是小群聊，不要自动塞进某个 AI 的私有记忆。
- 即时提取入口也要传 `chat_type`：`proxy._background_extract()` / `/api/gateway/post-process` -> `gateway.post_process()`。否则网页聊天会先写一条错误共享记忆，再由缓冲提取写私有记忆。
- 朋友圈/论坛评论表有 `parent_id`，前端用下拉选择回复整条内容或某条评论。社交 AI 调用必须走 `gateway.build_context()` 注入相关记忆；不要再从 `memory_ops` 导入不存在的 `get_corridor`。
- 社交评论不要剥掉用户输入里的 `@`；前端保留原文，后端用 `_resolve_social_mentions()` 兜底解析 `@jasper` / `@lucien` 等。默认围观和 @ 只允许真实社交角色（小克/Lucien/Jasper），不要把 `gpt` / `gemini` 基座模型随机拉出来。
- 社交模块的 AI 回复会通过 `_capture_social_exchange()` 进入 `conversation_capture`，按 `chat_type=private_group` 缓冲提取到 `social` 等共享上下文；不会写成单个 AI 的私人记忆。
- 群聊 `/api/social/groups/{chat_id}/messages` 支持 `reply_to` 和 `mention_ai`；后端会解析正文里的可见 `@`，只从真实社交角色中选择回应者。AI 回复后也调用 `_capture_social_exchange()` 进入记忆缓冲。
- 2026-07-02 群聊触发收窄：`@Jasper，` / `@lucien。` 这类带中文标点的提及会被正确识别；一条用户消息只触发一波 AI 回复。AI 回复文本里保留的 `@` 不再自动唤起第二轮 bot-to-bot 回复，避免回复某个 bot 时出现 2-3 条重复/连锁回复。
- 2026-07-02 群聊别名修正：社交后端现在用 `_normalize_social_ai_id()` 统一群成员和 @ 文本，`claude`、`cloudy`、`小克` 会归一到真实社交小克；`Jasper`/`jasper` 和带中文标点的 @ 也能命中。旧群里成员存成 `claude` 时不需要重建。
- 群聊前端支持消息级回复、删除、成员 @ 快捷按钮，以及轻量活动提示（谁读取记忆并回复）。后续如果参考 Aion’s Home，优先扩展成可折叠的“AI 做了什么/用了什么工具/读了哪些记忆”轨迹面板。
- 仓库里剩余的 `[鐢ㄦ埛]` / `[浜掑姩]` 乱码前缀只在 `memory_ops._normalize_for_dedup()` 中用于兼容旧坏数据去重，不是 UI 文案。不要误删，除非先迁移旧数据。
- `Tidal_Echo` 可借鉴方向：手机 PWA 壳、SSE 流式回复、多会话/API 窗口、消息 reaction/typing 状态、历史消息持久化。不要整套替换 Memory Hub 前端，优先拆成 ChatPage/GroupChat 的小功能迭代。
- 不要把 embedding 改回本地 fastembed（VPS 只有 1G 内存，ONNX 模型撑不住）
- 不要把 importance 范围改回 0.5~1.0——必须允许低分（0.1/0.2/0.3）让垃圾记忆被衰减淘汰
- 不要删 quick=True 的 embedding 去重——这是防止自动提取产生大量近似重复的关键机制
- 不要把 DECAY_LAMBDA 调回 0.08 以下——0.12 是让不重要记忆在合理时间内归档的基准值
- 不要把 MERGE_SIMILARITY 调回 0.85 以下——0.75 曾导致不同房间的记忆被过度合并成"百科全书"
- 不要删 `_try_merge` 里的跨房间检查——指定 personality 的记忆不能被合并进 psychology 的旧记忆
- 不要用"傀儡模式"做社交——社交功能应该是 AI 自主决定发帖，不是后台用 prompt 模板让模型扮演角色生成内容
- 不要把跨 AI 记忆注入走廊——已移除，改用 chat_digest（同一 AI 自己的跨窗口摘要），防止记忆身份混淆

## VPS 管理

- GitHub Actions 自动部署：push 到 main → git pull → pip install → npm build（自动构建前端）→ 清理 __pycache__ → 重启服务
- 重启：`systemctl restart memory-hub`
- 日志：`journalctl -u memory-hub -n 50`
- 项目路径：`/opt/memory-hub/`
- 前端构建：`cd /opt/memory-hub/frontend && npm run build`（输出到 `../static-app/`）
- **重要**：重启前务必清理 `find /opt/memory-hub -name '__pycache__' -type d -exec rm -rf {} +`
- 密码管理：GitHub Secrets（HUB_SECRET, LLM_API_KEY），deploy.yml 自动同步到 VPS .env
- **部署以 GitHub 为准**：deploy.yml 使用 `git fetch origin main` + `git reset --hard origin/main` 对齐代码，避免 VPS 上前端自动构建改动 `static-app/index.html` 后阻止 `git pull`，导致 Actions 绿色但后端仍是旧版本。`.env`、`data/` 记忆库不在 Git 里，不会被这个重置覆盖。

### 2026-07-03 观测台合并说明

- `/app/observatory` 现在是记忆管理入口：总览展示 daemon 状态、衰减统计、保护中/短期池/观察中/临近归档记忆；“保护中”列表会显示为什么被保护。
- 观测台内置“时间线”和“记忆编辑”两个标签，直接复用原记忆页面，用户不用在多个页面之间猜功能在哪里。
- 以后热力图、Dream Context、人生章节 skip 诊断、召回 token 诊断、保护/衰减解释都继续收敛到观测台。

### 2026-07-03 Memory Safety Kit 轻量版

- 新增 `safety_export.py`：把值得长期保留的记忆导出成 `exports/obsidian/memories/.../*.md`，并写 `exports/obsidian/reports/YYYY-MM-DD.md` 安全报告和 `manifest.json`。
- 新接口：`POST /api/export/obsidian?dry_run=false&force=false`，需要 `HUB_SECRET`。`dry_run=true` 可先看会导出多少，不提交 GitHub。
- `daemon.run_full_maintenance()` 已接入 `memory_safety_export` 步骤，梦境生成后自动跑。失败只 warning，不阻断普通 JSON 备份。
- 当前筛选是保守长期导出：锚点、人生章节、梦境、周记、重要长期房间优先；已归档、低重要度、game_room、work_tasks、临时 social 默认跳过。
- 后续建议：把最近一次安全报告接进观测台；增加“立即导出到 Obsidian”按钮；再补恢复演练和 SQLite 压缩快照。
