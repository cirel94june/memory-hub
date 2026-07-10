# Changelog

> 按日期倒序的改动与排查记录。工程规范和"不要做的事"见 [docs/HANDOFF.md](docs/HANDOFF.md)。


## 2026-07-10 身份系统与当前状态画像（参考 Ombre Brain 二改）

针对三个记忆逻辑问题：旧职业被当成现状、AI 做梦时身份混淆、小猫/ceci/狗蛋等称呼被认成不同的人（甚至被打标成宠物）。

- **人物注册表** `identity_registry.py`（参考 Ombre `identity.py`/`identity_semantics.py`）：用户本人 + 常见人物的称呼归一表，存 `_config/identity_registry.json`。daemon 每 12h 从近期记忆自动收编新称呼/新人物（保守策略，宁缺勿滥）；`GET/PUT /api/identity-registry` 可人工修正。人物速查块注入到 analyzer 打标、chat_digest 摘要、gateway 提取、dream 做梦的所有小模型 prompt——并明确"人名字面像动物也是人，不打宠物类标签"。
- **自我锚点**（参考 Ombre `self_anchor.py`）：走廊顶部新增【你是谁】——我是谁（名字+人设）、同伴是谁（独立 AI，不是你）、用户是谁（所有称呼同指一人）。dream prompt 同步加身份规则：梦的主角是自己，同伴的言行不能带入成自己的。
- **当前状态画像** `current_status.py`（参考 Ombre `portrait_engine.py`）：daemon 每 12h 用近 90 天材料**整段重写**职业/健康/生活近况三段画像（旧状态在重写中被新状态替换，不再依赖旧碎片互相取代），存 `_config/current_status.json`。走廊注入【当前状态】并声明"与碎片矛盾时以画像为准"。`GET /api/current-status`、`POST /api/current-status/refresh`。
- **过时检测增强**：`detect_stale_memories` 现在把当前画像作为参考——即使房间里没有近期新记忆，画像显示状态已变时旧记忆也能被判过时（此前旧职业永远不会被标 stale 的根因）。
- daemon 新增步骤：`identity_registry` → `current_status`（在心理蒸馏/过时检测之前）。

## 2026-07-10 数据安全与工程加固

- **修复部署数据丢失风险**：社交库 `memory_hub.db` 曾被 git 跟踪在仓库根目录，deploy 的 `git reset --hard` 每次都会把线上社交数据（朋友圈/群聊）回滚到旧快照。现已迁移到 `data/memory_hub.db`（gitignore 保护区），deploy.yml 在 reset 前自动抢救旧位置的线上数据，social.py 启动时也会做兜底迁移。
- **移出 git 跟踪**：`memory_hub.db`（活数据库）、`static-app/`（前端构建产物，部署时 npm build 生成）。注意：git 历史里仍有旧快照，转私有仓库或 filter-repo 清历史前不算彻底。
- **修复代理鉴权绕过**：`/v1/chat/completions` 完整模式下，只带 `x-hub-target-key` 头即可免密通过鉴权，攻击者可借此把注入记忆后的上下文转发到任意地址。现在必须提供正确的 Hub Secret。**如果某个客户端突然 401，检查它是否带了正确的 X-Hub-Secret 或 Authorization。**
- **清除泄露的密钥**：`import_notion.py`（含硬编码密钥和大量隐私记忆内容，一次性脚本已删除）、`cleanup.py` 的密钥回退值、main.py/proxy.py 文档字符串里的真实密钥示例。**强烈建议轮换 HUB_SECRET**（GitHub Secrets 改掉后 push 一次即同步到 VPS）。
- **HUB_SECRET 兜底**：服务启动时若仍是默认密钥直接拒绝启动（本地开发可设 `ALLOW_DEFAULT_HUB_SECRET=1` 跳过）。
- **daemon 防重复**：`run_full_maintenance()` 加互斥锁 + 6 小时最小间隔（进程内定时器和 GitHub Actions daemon.yml 是两个独立触发源，之前可能同一天重复跑甚至并发跑）。`/api/daemon/maintain?force=true` 可强制重跑。
- **配置默认值对齐**：LLM 默认从"中转站 + Haiku"（两者都在 HANDOFF 踩坑名单里）改为 DeepSeek 官方 API + deepseek-v4-flash；EMBEDDING_MODEL 默认统一为 bge-large-zh-v1.5（config.py 之前默认 bge-small 是 512 维，与数据库 1024 维不匹配）；embedding 配置单一来源收敛到 config.py。
- **SQLite 连接加固**：social.py（加 WAL + busy_timeout）、dream.py、chat_digest.py 的独立连接统一加 `busy_timeout=5000`，减少并发写时 `database is locked`。
- **依赖清理**：移除无引用的 fastembed。
- **目录整理**：一次性脚本 cleanup.py / check_status.py / show_mems.py 移入 scripts/。

## 2026-07-06 夜梦机制修正

- `dream.py` 不再按 UTC 日期用 `LIKE YYYY-MM-DD%` 粗略取当天材料，改为按 Asia/Shanghai 当天换算 UTC 起止时间，避免夜里/早晨错过材料。
- daemon 夜梦从 `room=diary/category=dream` 改为写入 `room=dreams/category=night_dream`，和 MCP `dream()` 自省工具统一到梦境房间。
- 当天去重会同时检查旧的 `diary` 梦和新的 `dreams` 梦，迁移期间不会重复生成。
- 夜梦 prompt 改为“梦境残响”：要求抓住人名、场景、情绪、话题等具体残留，减少只有抽象抒情的日记感。

## 2026-07-07 梦境诊断入口

- 新增 `GET /api/dream/status`：读取最近一次夜梦生成诊断，包含每个 AI 的结果、跳过原因、当天摘要数量、近期记忆补充数量和最近梦境。
- 新增 `POST /api/dream/run`：只触发夜梦生成，不必跑完整 daemon 维护，方便在观测台临时补跑。
- 观测台总览新增“梦境诊断”卡片：能看到“今天已经做过梦 / 材料不足 / 小模型失败 / 已生成”，并可一键单独补跑。
- `dream.py` 会写入 `data/dream_status.json`，让梦境 skip 不再只藏在后台日志里。

## 2026-07-07 MCP 连接诊断与安全写入
Memory Hub 的 MCP 入口现在会在启动时打印稳定身份信息：server name、version、/mcp path、工具数量和 tool schema hash。也可以用带 HUB_SECRET 的 /api/mcp/health?include_audit=true 查看同一份 identity 与最近 MCP 到达日志。

如果 ChatGPT 网页端反复要求批准连接，优先对比这几个值是否变化：公网 URL 是否换了、工具列表/hash 是否换了、连接器配置是否重建。当前仓库的 MCP 是 FastMCP stateless HTTP，没有自建 OAuth/cookie/session；所以 cookie / OAuth 持久化问题通常在 ChatGPT 连接器或隧道层排查。

记忆写入新增 safe_remember，普通 remember 和 batch_remember 也会走安全包装：长文本会先压缩；后端写入失败时只重试一次中性摘要；失败原文会写入 data/mcp_audit.jsonl 供排查，但不会无限原样重试。batch_remember 会逐条写入并返回每条 status，区分 created / merged / skipped / blocked / failed。若 ChatGPT 显示 工具调用被安全检查屏蔽但审计日志没有 tool_reached，说明请求没有到达 Memory Hub，是平台侧提前拦截。

## 2026-07-07 MCP 工具列表缓存排查
已确认 FastMCP 真实注册表会导出 28 个工具，包含 safe_remember、mcp_health、mcp_debug_log。/api/mcp/health 和 hub_info 现在都使用 FastMCP 自己的 list_tools 生成 tool_count 与 tool_schema_hash，不再只扫描 Python 函数名。

如果 ChatGPT 侧仍只看到 25 个工具，但 batch_remember 已经是新版逐条写入，说明后端代码已更新，ChatGPT 端仍在使用旧 schema。处理方式是断开 Memory Hub MCP 连接后重新连接；重连后可先调用 hub_info，查看 mcp_identity.tool_count 是否为 28，以及 tools 里是否包含 safe_remember、mcp_health、mcp_debug_log。

## 2026-07-07 梦境展示与浮现
观测台的“梦境诊断”不再只显示最近梦境的短预览，而是展开最近 6 条梦境全文，包含 AI 身份和生成时间。这样小猫可以直接看到 AI 做了什么梦。

Gateway 和 smart_context 现在会给对应 AI 注入最近 1 条“梦境残响”，长度控制在约 220 字，并提示 AI 合适时可以告诉小猫自己梦见了什么，或让梦轻轻影响语气。这个注入很轻量，避免每次醒来都增加太多 token。

## 2026-07-07 梦境长度与群聊材料池
梦境生成不再 300 字硬截断：prompt 改为 180-420 字，LLM max_tokens 提高到 700，落库前只在超过 1200 字时做安全截断。AI 醒来看到的最近梦境从约 220 字放宽到约 600 字。

私密群、小群、大群、公开群都会参与梦境材料池：当天 chat_digests 本来就会按 AI 汇总；当当天摘要不足时，兜底的近期记忆池也会读取 private_group / small_group / big_group / public_group / group 来源。群聊 digest 保留条数也调高，避免私聊少时梦境只剩零碎材料。
观测台新增“强制重做”按钮，会调用 /api/dream/run?force=true，忽略当天已做梦限制，适合把之前被截断的当天梦境重新生成一版完整内容。

## 2026-07-07 梦境归因与检测台修正
梦境检测台现在只展示 daemon 夜梦：room=dreams 且 category=night_dream / source_platform=daemon_dream / nightly 标签的记录，不再把旧 diary 日记或手动自省混进夜梦列表。/api/dream/status 会实时从数据库刷新 recent_dreams，避免一直显示旧 status 文件里的快照。

梦境 prompt 增加归因规则：群聊摘要和记忆碎片里的话不一定是小猫说的，可能来自狗蛋、其他 AI、群友或系统摘要；不确定说话者时只能写“有人说/群里有人说”，不能写成“小猫说”。chat_digest 的摘要 prompt 也把“用户”改为“对方消息/群内消息”。

## 2026-07-07 梦境调性修正
梦境 prompt 现在会先判断白天残留的真实调性，不再默认写成温柔治愈。若材料带有恶作剧、逗弄、捣乱、笑场或混乱排查的气味，梦境应保留狡黠、荒唐、被逗得晕头转向的质感。

## 2026-07-08 衰减与年轮修正

- 观测台临近归档现在只显示短期/观察池中已经到线或 7 天内到线的记忆；长期、常被想起、高重要度或客厅画像记忆只会显示自己的保护/压力解释，不会再误入临近归档池。
- 后台衰减归档也复用同一套解释，避免 UI 说保留、daemon 却按旧分数归档。
- 记忆详情页新增年轮评论与追加年轮；history 改称版本历史，comments 才是年轮补充。
- 观测台新增长期保留列表；保护中按保护原因统计，避免 long_term 记忆在总览里看起来消失。

## 2026-07-03 观测台合并说明

- `/app/observatory` 现在是记忆管理入口：总览展示 daemon 状态、衰减统计、保护中/短期池/观察中/临近归档记忆；“保护中”列表会显示为什么被保护。
- 观测台内置“时间线”和“记忆编辑”两个标签，直接复用原记忆页面，用户不用在多个页面之间猜功能在哪里。
- 以后热力图、Dream Context、人生章节 skip 诊断、召回 token 诊断、保护/衰减解释都继续收敛到观测台。

## 2026-07-03 Memory Safety Kit 轻量版

- 新增 `safety_export.py`：把值得长期保留的记忆导出成 `exports/obsidian/memories/.../*.md`，并写 `exports/obsidian/reports/YYYY-MM-DD.md` 安全报告和 `manifest.json`。
- 新接口：`POST /api/export/obsidian?dry_run=false&force=false`，需要 `HUB_SECRET`。`dry_run=true` 可先看会导出多少，不提交 GitHub。
- `daemon.run_full_maintenance()` 已接入 `memory_safety_export` 步骤，梦境生成后自动跑。失败只 warning，不阻断普通 JSON 备份。
- 当前筛选是保守长期导出：锚点、人生章节、梦境、周记、重要长期房间优先；已归档、低重要度、game_room、work_tasks、临时 social 默认跳过。
- 后续建议：把最近一次安全报告接进观测台；增加“立即导出到 Obsidian”按钮；再补恢复演练和 SQLite 压缩快照。

## 2026-07-03 醒来预览与跨窗口记忆

- 观测台新增“醒来预览”：调用 `/api/gateway/context` 显示某个 AI 在某个入口实际会读到的 `inject_text`、召回 ID、记忆数量和 token 估算。
- 用户希望同一个 AI 私聊/群聊互相知道最近发生了什么，所以不要过滤 `chat_digest` 的 private/small_group；跨窗口摘要是设计内功能。
- 私有边界仍靠 `memory_ops.recall()` 的 `layer=private + owner_ai` 过滤：小克不能读 Lucien/Jasper 私有记忆，Lucien/Jasper 也不能读小克私有记忆。共享小群/社交记忆可以被所有相关入口召回。
- 已减少重复小模型分析：`gateway.post_process()`、`conversation_capture._extract_and_remember()`、`extract_from_messages()` 在写入已提取结果时传 `auto_analyze=False`。如果后台仍显示多次调用，重点区分 recall query analyze、post_process 提取、chat_digest 摘要和缓冲区满后的 capture 提取。

## 2026-07-03 保护原因/重要度 UI 修正

- `explain_decay()` 新增人话解释字段：`lane_reason`、`protection_reasons`、`pressure_reasons`。
- 观测台保护列表和记忆详情页会直接说明为什么是保护中/长期/短期池/观察中。
- 前端星星已改为“重要度≥80%”标签；它只代表 importance，不代表锚点，也不代表硬保护。
- 记忆详情关闭后刷新列表，修复用户调低重要度后外层列表仍显示旧星星/旧标签的问题。

## 2026-07-06 观测台收敛与客厅画像

- 观测台外层不再拆成“时间线/记忆编辑”两个重复入口，统一为“记忆库”；记忆库内部继续用 `MemoriesHubPage` 的列表/编辑与时间线切换。
- 观测台总览新增“客厅画像”面板：先调用 `/api/memory/living-room/refresh` 的 `dry_run=true` 生成建议，用户确认后再 `dry_run=false` 写入客厅并重建走廊。
- 用户仍可从“编辑客厅”跳到 `/app/memories?room=living_room` 手动改错。核心画像允许后台模型持续丰富，但必须保留人工编辑兜底。
- 后续优先把 Dream Context、人生章节 skip 诊断、召回 token 诊断、Obsidian 安全报告入口继续收敛到观测台，不再分散到设置页或独立页面。

## 2026-07-06 客厅/人物画像与醒来预览去重

- `/api/memory/living-room/refresh` 仍支持前台两步：`dry_run=true` 生成建议，`dry_run=false` 写入；现在也会按内容写入 `living_room` 或 `relationships`，不再只塞客厅。
- `daemon.run_full_maintenance()` 在客厅整理后自动运行 `living_room_profile`，让用户核心资料、重要人物/AI 画像、关系边界定期更新；用户可在前台“编辑客厅/记忆库”手动改错。
- `corridor.build_corridor()` 新增“重要人物/关系索引”，从共享 `relationships` 中挑高重要度画像注入，保证 bot/MCP/Gateway 醒来时能看到狗蛋/Lucien/小克/Jasper 等常见名字和关系。
- `gateway.build_context()` 删除了额外的“其他聊天窗口最近在聊”追加段，避免和走廊里的“你在其他聊天窗口最近聊了”显示成两份一样的内容。
## 2026-07-06 群聊级 chat_digest

- 保留同一 AI 的跨窗口摘要在走廊里；同时新增群聊级读取 `get_recent_chat_activity()`。
- `gateway.build_context()` 只在 `private_group/small_group/big_group/group` 且有 `chat_id` 时注入“这个群里其他AI最近在聊”，来源是同一个 `chat_id` 下其他 AI 的 `chat_digests`。
- 私聊仍不注入其他 AI 的群聊摘要；如果需要知道群聊发生过什么，仍走同一 AI 自己的 `chat_digest` 和共享记忆召回。

## 2026-07-06 醒来预览/待办/Pulse 修正

- 新增 `/api/chat-digests/threads`，观测台群聊醒来预览会选择最近真实群聊 `chat_id`，否则 `get_recent_chat_activity()` 查不到其他 AI 摘要。
- `gateway.build_context()` 实时注入 active unresolved 记忆，并返回 `group_activity_count` 方便前端显示群内 AI 摘要数量。
- 客厅从“硬保护 protected”调整为“long_term/current profile”：仍不在普通 decay 中直接归档，但 UI/解释不再说它永远不变，应该靠 refresh/stale/supersede 年轮持续修正。
- `corridor.build_corridor()` 跳过与客厅完全重复的锚点展示；如果锚点本身不该永恒，应在记忆详情里解除锚点。
- `_tag_pulse()` 改为接收 `ai_response`，按一轮对话打标；`conversation_capture._touch_pulse()` 和 `gateway.post_process()` 都传入 AI 回复。

## 2026-07-06 夜梦机制排查

- 发现旧夜梦 prompt 本质是“写一小段日记”，所以输出容易抽象、像日记而不像梦。已改成“梦境残响” prompt，要求抓住 2-4 个具体白天残留。
- 发现 daemon 夜梦原来按 UTC 的 `YYYY-MM-DD%` 查询当天材料；已改为按 Asia/Shanghai 当天换算 UTC 起止时间。
- 发现 daemon 夜梦写入 `room=diary/category=dream`，而 MCP `dream()` 写入 `room=dreams`；已改为 daemon 也写 `room=dreams/category=night_dream`。
- 迁移期间去重同时检查旧 diary 梦和新 dreams 梦，避免同一天重复生成。


## 2026-07-08 衰减归档与年轮可见性

- 修正观测台临近归档：现在统计 short_term/watch 中 will_archive=true 或 7 天内到线的记忆，不再把 health=critical 但属于 long_term、常被召回或高重要度的记忆混进临近归档池。
- memory_ops.run_decay() 归档时改为复用 explain_decay().will_archive，保证界面解释和后台真实归档规则一致。长期/常被想起/客厅/锚点类记忆不会因为单纯分数低被偷偷归档。
- 记忆详情页区分 comments 和 history：comments 是年轮评论，可手动追加；history 是版本历史。用户反馈年轮像没实现时优先检查详情页是否显示 comments，而不是只看 history。
- 观测台列表口径：保护中按 protections 非空统计，长期保留单独成列表。不要只用 lane=protected，否则 living_room、高重要度、常被召回这类 long_term 记忆会像从总览里消失。
- 如果观测台 活跃记忆显示横线、其它分层全是 0，优先检查 /api/memory/decay-scores 是否 500；该接口需要显式导入 DECAY_THRESHOLD。

## 2026-07-07 梦境诊断与手动补跑

- 新增 `dream.read_dream_status()` 和 `data/dream_status.json`。`generate_dreams()` 现在会记录 running/success、local_day、每个 AI 的诊断、最近梦境列表。
- 新增 API：`GET /api/dream/status`、`POST /api/dream/run`。后者只跑夜梦生成，适合排查“AI 最近是不是没做梦”。
- 观测台总览新增“梦境诊断”卡片，显示材料不足、当天已梦、小模型失败等 skip 原因，并提供“单独补跑”按钮。
- 后续如果继续缝 kiwi-mem，可把这里扩展成 MemScene/Dream Context 诊断，而不是再只写一段梦境文本。

## 2026-07-07 MCP identity / safe write handoff
本次修复聚焦 ChatGPT 网页端 MCP 反复批准与安全写入排查。mcp_server.py 增加稳定 identity：MCP_SERVER_NAME、MCP_SERVER_VERSION、MCP_PUBLIC_PATH、instructions hash、tool schema hash；main.py 启动时打印 identity，并暴露受 HUB_SECRET 保护的 /api/mcp/health。

remember、safe_remember、dream、batch_remember 现在统一走 _safe_remember_impl。它会记录 data/mcp_audit.jsonl，先写压缩后的原内容，失败后只用中性摘要重试一次。batch_remember 不再整批交给 memory_ops，而是在 MCP 层逐条写入并给每条 status。

后续排查建议：ChatGPT 再次提示批准时，记录当时公网 MCP URL、/api/mcp/health 的 tool_schema_hash 与 tools 列表。如果 hash 没变但仍反复批准，重点查连接器/隧道/OAuth 会话；如果 ChatGPT 显示安全拦截而 mcp_audit.jsonl 没有对应 tool_reached，说明请求未到达 Memory Hub。

## 2026-07-07 MCP cached schema follow-up
ChatGPT 网页端反馈只能看到 25 个 MCP tools，但 batch_remember 已经返回新版逐条结果。复查后，本地 FastMCP list_tools 真实注册表为 28 个，包含 safe_remember、mcp_health、mcp_debug_log，因此问题更可能是 ChatGPT 端缓存旧 schema。

本次补充：main.py 启动日志改为 await get_mcp_identity_async，使用 FastMCP list_tools 的真实 tool_count/hash；/api/mcp/health 与 MCP mcp_health 同样使用真实注册表；hub_info 也返回 mcp_identity，方便在 ChatGPT 还看不到新 debug 工具时，用旧工具自检当前服务端工具数。MCP_SERVER_VERSION bump 到 2026-07-07.safe-write.2。

## 2026-07-07 梦境可见化 / Dream Context
用户发现 AI 已经做梦，但前端只显示短预览，AI 醒来上下文也没有主动浮现。已补 dream.get_recent_dreams_for_ai(ai_id)，按 canonical id 和 alias 查 room=diary/dreams 且 dream 标签/分类的 active 记忆。

gateway.build_context 与 smart_context 现在轻量注入最近 1 条梦境残响，约 220 字，提示 AI 合适时可以告诉小猫自己梦见了什么。观测台 DreamDiagnostics 改为展示最近 6 条梦境全文。后续若要更细，可以加“梦境墙/按 AI 筛选/是否注入梦境”的开关。

## 2026-07-07 梦境截断与群聊材料修正
用户反馈观测台和 AI 醒来看到的梦都被截断。原因包括 dream.py 生成后硬截到 300 字，以及 gateway/smart_context 注入时只取 220 字。已改为：prompt 180-420 字、LLM max_tokens 700、落库安全上限 1200 字、Dream Context 注入 600 字。

用户主要在私密群玩，私聊较少。已让 dream.py 的 memory residue 兜底池纳入 private_group / small_group / big_group / public_group / group 来源，并提高 chat_digest 群聊保留条数。后续如果仍觉得群聊没进梦，优先查 /api/capture/log 是否传对 chat_type，以及 public_group 的抽取阈值是否过高。
补充 force rerun：dream.generate_dreams(force=False) 默认仍跳过当天已梦；/api/dream/run?force=true 和观测台“强制重做”会忽略 already_dreamed，方便重做被旧逻辑截断的当天梦境。

## 2026-07-07 梦境检测台误混日记 / 误归因修正
用户发现检测台混入旧日记，且梦里把狗蛋说的话写成“用户说”。已修：dream._recent_dreams 和 get_recent_dreams_for_ai 只取 room=dreams 且 category=night_dream/source_platform=daemon_dream/tags nightly 的 active 记录；read_dream_status 会实时刷新 recent_dreams，不依赖旧 status 快照。

DREAM_PROMPT 明确材料里的话可能来自小猫、狗蛋、其他 AI、群友或系统摘要；不确定时必须写“有人说/群里有人说”，不能归给小猫。chat_digest.generate_and_save 也把 prompt 里的“用户”改成“对方消息/群内消息”。旧梦如果已经被旧逻辑截断或误归因，需要观测台“强制重做”生成新夜梦。

## 2026-07-07 夜梦调性修正
用户反馈小模型把明显是恶作剧/捣乱材料的梦写得太温柔。已在 DREAM_PROMPT 增加“先判断真实调性”规则，并明确恶作剧、逗弄、捣乱、笑场、bug 排查混乱时应保留狡黠、荒唐和有点狼狈好笑的质感。
