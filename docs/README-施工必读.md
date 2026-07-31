# Memory Hub 施工必读

> 每次开工前必读。任何设计决策与本文档冲突时，以本文档为准。
> 修改本文档需要 Ceci 亲自确认。

---

## Part 1｜五分钟入职

Memory Hub 是**所有 AI 居民（小克/Lucien/Jasper）共享的跨端记忆后台**。不同前端（Telegram bot、Lamplight 房子、RikkaHub、Operit）都是"门"，Hub 是背后的记忆之家。

**核心定位**：
- 不是聊天记录数据库，是记忆认知系统
- 服务的是"多个独立 AI 居民"（不是"一个 AI 换 prompt"），架构上必须支持每个 AI 有自己的私人记忆 + 共享公共记忆
- Ceci 的画像：不写代码，用中转站/CLIProxyAPI，焦虑点是"搓了很久白搓"

**技术栈**：Python FastAPI + MCP server，SQLite + sqlite-vec（1024 维），FTS5 全文搜索，DeepSeek 提取模型。VPS 部署，push main 自动部署。

---

## Part 2｜必读文档（真相源优先级）

冲突时按顺序采信：

1. **本文档**（README-施工必读.md）
2. **当前施工单**（如 memory-hub-施工单v1.md）
3. **Continuity Layer v2 红线**（continuity-layer-review-v2.md，16 条红线）
4. **代码中 canonical 标记的部分**（如 canonical memories、已确认 Profile）
5. Memory Hub 中的普通召回内容只作参考，不覆盖前四项

**这些文档在哪里**：
- 施工单和红线目前在**小猫的桌面**（未来会推 Hub repo docs/）
- 需要看时找小猫要，不要凭旧印象改代码

---

## Part 3｜四条最高频踩坑

### 坑 1：把不同 AI 的记忆混在一起
**错**：所有 AI 读同一个 relationship / diary / personality 房间。
**对**：这些房间是 `scope=per_ai`，每个 AI 各自一份。跨房间读取时必须过 `owner_ai` 过滤。红线 #4。

### 坑 2：让提取器无脑 create
**错**：Ceci 说三次"我在腾讯做产品经理"，就存三条重复记忆。
**对**：Phase 1 之后有 MemoryMaintenanceDecision 引擎，必须先搜相关记忆（sim≥0.55）→ 走关系分类 → 决定 9 种动作之一（create/update/supplement/correct/supersede/annotate/resolve_thread/reopen_thread/no_change）。

### 坑 3：让后台维护模型（DeepSeek 等）代替 AI 生成主观内容
**错**：DeepSeek 便宜，用它生成 Lucien 的日记 / Handoff / 对 Ceci 的第一人称观察。
**对**：后台模型只做**客观整理**——摘要、分类、去重候选、审计。主观内容（第一人称感受、印象、日记）必须由**该 AI 自己的模型**生成。红线 #12。

### 坑 4：Profile 反向污染 memory
**错**：Profile 生成后被写回 memory 表当作新记忆源。
**对**：单向流动——**memory → Profile，绝不反向**。Profile 是派生视图，不是记忆源。红线 #20。

---

## Part 4｜施工方式约定

1. **实时更新文档**：施工方案、进度、当前状态实时更新到 repo 或 issue，任何 agent 冷启动都能接手
2. **不推翻已验证的东西**：现有 25 MCP 工具、16 房间、corridor、daemon 都要保留；施工是"在骨架上升级"，不是重建
3. **偏离产品定义或红线要立刻停手**：立刻停下问 Ceci，不要"先做再说"
4. **公开仓库注意事项**：Hub 仓库当前是公开的，且 git 历史有过泄露的旧 HUB_SECRET；提交前检查有没有硬编码密钥、Session、Token
5. **VPS 部署后一次性操作**：施工方默认不动 VPS，但小猫可以就特定命令给一次性授权。走五步流程：Claude 审 → 小猫授权 → 施工方 dry-run 截图 → 小猫确认 → 施工方正式跑 → 结果贴回

---

## Part 5｜施工报告 / 施工方案模板

### 施工方案（开工前提交给 Ceci 审）

按这五问：
1. **要解决什么问题？** —— 用户视角，不写"实现 X 功能"，写"以前 X 不行，会导致 Y"
2. **现在 Hub 是什么样？** —— 用户视角描述当前行为
3. **打算改成什么样？** —— 用户视角描述改后行为
4. **分几步做、每步验收标准？** —— 拆子任务，每步一个可验证的判断
5. **有什么风险或不确定的地方？** —— 报忧不报喜

### 施工报告（每完成一个 PR / 里程碑提交给 Ceci）

按这五问，每问 1-3 句话，Ceci 不写代码，用她能听懂的语言：

1. **原来遇到的问题是什么？**（举例：❌"实现了 X 函数" ✅"以前提取器无脑创建重复记忆，你说三次'我在腾讯'就存三条"）
2. **以前 Hub 怎么运行？**（举例：❌"调用了 _create_proposal" ✅"以前 remember() 只会 create，不检查已有记忆"）
3. **现在改成了什么？**（举例：❌"新增 MemoryMaintenanceDecision 引擎" ✅"现在会先搜相关记忆，重复内容会走 supplement/annotate/supersede 之一，不再无脑 create"）
4. **小猫用什么方式能亲自看出区别？**（举例："对着 Telegram 说三次'我在腾讯做 PM'，然后 recall 看只有一条越写越准的记忆，不是三条"）
5. **哪些地方仍然没修好？**（必写！举例："`_write_audit` 已经接受 model_id 参数但调用点还没传值，等 Phase 2 analyzer 支持了再接"）

---

## Part 6｜发送时机

- 施工方案：开工前发，等 Ceci（或 Claude 代 Ceci 审）通过后再动代码
- 施工报告：每个 PR 合并前发一次；每个施工单里程碑完成时发一次；遇到设计层面的偏离立刻发（不要等做完）
- VPS 部署后：如果需要跑迁移或初始化脚本，按 Part 4 的授权模式走
