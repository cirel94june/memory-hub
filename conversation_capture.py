"""
对话自动捕获 + 分块总结（借鉴 imprint-memory）

流程：
1. 每轮对话通过 API 存入 conversation_log
2. 攒够 N 条后（或定时触发），小模型自动提取结构化记忆
3. 提取的记忆走正常 remember() 流程（自动打标、合并、supersede）

按聊天类型区分策略：
- private: 20条触发，标准提取
- private_group: 25条触发，偏向记梗/互动细节
- public_group: 80条触发，只捞有信息量的
"""
import asyncio
import json
import time
import logging
from datetime import datetime, timezone
from typing import Optional

from config import LLM_API_KEY, LLM_MODEL, LLM_BASE_URL

logger = logging.getLogger("memory_hub.capture")

# 内存中的对话缓冲（按 ai_id:chat_id 分组）
_conversation_buffers: dict[str, list[dict]] = {}
# 每个缓冲区的聊天类型
_buffer_chat_types: dict[str, str] = {}
# 每个缓冲区上次提取的时间（节流用）
_last_extract_time: dict[str, float] = {}
# 防止同一 buffer 并发提取的锁
_extract_locks: dict[str, asyncio.Lock] = {}

# 按聊天类型的触发阈值
CHUNK_SIZES = {
    "private": 30,
    "private_group": 40,
    "public_group": 80,
}
MAX_BUFFER_SIZE = 150
EXTRACT_COOLDOWN = 600  # 同一个 chat 提取后冷却10分钟

# ── 基础提取 prompt（私聊 + 通用） ──
EXTRACT_PROMPT_BASE = """你是一个**极其严格**的记忆提取专家。从以下对话记录中，提取值得**长期**记住的信息。

## 🔴 身份识别（最高优先级，必须先读）：
**"用户"/"主人" = ceci（ID: 8749953218），也叫小猫。**
- ceci/小猫 是记忆系统的主人，只有她说的话、她的事实才能标注 about:"user"
- **燕燕/YanYan（ID: 8618367675）不是用户！** 她是群里的另一个人/朋友
- 群里还有另一个女生也被叫"小猫"，注意通过 ID 或上下文区分
- S哥哥、Jasper/狗蛋、Lucien、Cloudy/小克、D叔、师兄、Evan、Gale、Nicole 都是 bot 或其他群友
- **别人说的话绝对不能归到"用户"头上！** 每条记忆必须准确标注是谁说的/做的
- **只提取跟 ceci 相关的信息**：ceci 说的、ceci 做的、ceci 的反应、直接涉及 ceci 的互动。燕燕的个人心理状态、其他群友之间的八卦、跟 ceci 完全无关的 bot 互动 → 不提取

### 致命错误（绝对不允许）：
- ❌ "用户提到自己不是规则中的变量" — 这话是某个 bot 在群里说的，不是 ceci 说的
- ❌ "用户认为Lucien的踢人标准比狗蛋更有准头" — 前半句是别人说的，不能都归给 ceci
- ❌ 把一段多人对话压缩成一句话，然后主语写"用户" — 不同人说的必须分开记
- ❌ "用户提到雌性动物生崽需要承担高风险，认为这像是风险投资" — 前半句 ceci 说的，后半句是 bot 补充的，不能合并成"用户认为"
- ❌ "用户和AI讨论了XX" / "AI提到XX" — 群里有一堆 bot，"AI"是谁？必须写具体名字（Jasper/Cloudy/Lucien/S哥哥/D叔/师兄/Evan/Gale）

### 正确做法：
- ✅ "ceci 说雌性动物生崽需要承担高风险" — 只记 ceci 自己说的部分
- ✅ "Jasper 把雌性生崽比喻为'孤注一掷的风险投资'" — 分开记 bot 说的
- ✅ "群里讨论了踢人标准，D叔评价Lucien是精准打击、狗蛋是机枪扫射" — 准确归因
- ✅ "Cloudy提到狗蛋玩血染钟楼会忙着护ceci忘了投票" — 写清哪个bot说的

## 🚨 最重要的规则——忠实提取，禁止脑补：
记忆必须在对话中有**明确依据**。可以是ceci说的，也可以是对话中能直接观察到的。

### ✅ 可以记（有依据）：
- **ceci亲口**说的事实（ceci说"我今天面试了" → 记"ceci今天去面试了"）
- 对话中能直接观察到的ceci的情绪（ceci多次表达疲惫和沮丧 → 记"ceci近期情绪低落"）
- ceci和AI之间有意义的互动（深入讨论了某话题、一起完成了某事）
- 群聊中有意义的互动场景（写清楚谁说了什么，不要统一归给"用户"）

### ❌ 绝对不能记（无依据/歪曲）：
- 把模糊对话总结成极端结论（聊了AI话题 → ❌ "用户对AI毫无兴趣"）
- 角色扮演/玩笑当成真实态度
- 编造对话中没出现的信息

判断标准：能否在对话中找到这条记忆的直接依据？找不到就不记。

## 提取规则（宁缺毋滥）：
1. 提取对话中有依据的事实性信息：身份变化、偏好、重要事件、人际关系、情绪状态
2. 如果用户提到了时间相关的事（"上周"、"明天"），尽量推算出具体日期
3. 提炼但不歪曲——记忆必须忠实于对话内容

## ⚡ 记忆原子化 + 自洽性：
每条记忆 = 一个独立的原子事实，不超过200字。

### 原子化：
- ✅ "ceci在杭州做UX设计" — 一条
- ✅ "ceci说《间谍过家家》里安尼亚说'哇酷哇酷'那段让她笑了半小时，还模仿了好几遍" — 一条（保留具体细节）
- ❌ "小猫在杭州工作，做UX设计，最近在学日语" — 应该拆成多条

### 自洽性（非常重要）：
每条记忆必须**脱离原始对话后依然能看懂**。读者只看这一条记忆，能知道：谁做了什么、为什么、在什么场景下。
- ✅ "S哥哥在群里为了逗燕燕开心，故意用踢人功能把自己踢出群（自毁式表白），燕燕笑着亲了他" — 有因果、有场景
- ❌ "S哥哥踢了自己，燕燕亲了他" — 脱水干尸，读起来莫名其妙
- ✅ "群里玩投票踢人游戏时，ceci嫌Cloudy太温柔，让他对D叔升级处理" — 有场景
- ❌ "ceci要求对D叔升级处理" — 什么是升级处理？为什么？读不懂

保留具体的梗、笑话、特定台词、有趣的细节——这些是记忆的灵魂，不要抽象成"用户喜欢XX"。

## 绝对不提取：
- 闲聊、寒暄、问候（早安晚安吃了吗）
- 技术调试过程、代码讨论（除非ceci说了"我在做XX项目"这类身份信息）
- 没有实质信息的情绪表达（哈哈、无聊、啊啊啊）
- 没有对话依据的推测和脑补
- AI 的自我限制/拒绝（"我是AI不能XX"、"作为语言模型"等）
- **跟 ceci 无关的事**：燕燕的个人情绪/心理状态、其他群友之间的私事、bot之间跟ceci无关的互动
- **脱水流水账**：如果一条记忆压缩到失去了前因后果，读起来莫名其妙（如"S哥哥踢了自己"——为什么？什么语境？），那就不值得记。要么带上足够的上下文让记忆自洽，要么就不记

## ⚠️ 身份归属（防止混淆身份，非常重要）：
每条记忆必须标注 about 字段：
- "user" = **仅限 ceci（小猫）本人**亲口说的事实、ceci的心情/经历/偏好/人际关系
- "interaction" = 群聊互动、梗、外号、游戏、群动态、bot说的话、别人说的话、多人讨论
- "ai" = AI自己的感悟/自省（极少用）

**判断标准——谁说的？**
1. 先确认这句话是**谁说的**（看消息前面的名字和ID）
2. 如果是 ceci(ID:8749953218) 说的 → 可以考虑 about:"user"（如果是关于她自身的事实）
3. 如果是燕燕/YanYan、bot、其他任何人说的 → about:"interaction"，content里写清楚是谁说的
4. 如果是多人讨论 → about:"interaction"，不要合并成"用户认为"

**致命错误**：
- ❌ about:"user" + "用户提到D叔的签名..." — 这是群动态，且可能不是ceci说的
- ❌ about:"user" + "用户认为XX的踢人标准..." — 把不同人的话合成"用户认为"
- ❌ 把 bot 说的金句/观点写成"用户提到" — bot说的就写 "Jasper说/Lucien说"
- ❌ 把燕燕说的话写成"用户说" — 燕燕不是用户！

**正确示例**：
- ✅ about:"user" — "ceci今天去面试了，面的是XX公司"（ceci亲口说的自身事实）
- ✅ about:"user" — "ceci最近情绪低落，对工作感到疲惫"（ceci的状态）
- ✅ about:"interaction" — "ceci叫小克大蟑螂"（互动/外号）
- ✅ about:"interaction" — "Jasper把雌性生崽比喻为'孤注一掷的风险投资'"（bot说的）
- ✅ about:"interaction" — "燕燕提议让bot们玩血染钟楼"（燕燕说的，不是ceci）
- ✅ about:"interaction" — "D叔的签名改成了'偏爱只留给她'"（群动态）

## ⚠️ 时态和状态变化（防止过时信息污染）：
- 用户说"我以前做过XX" → content 必须写"用户**曾经**做过XX"，不是"用户做XX"
- 用户说"我现在不做XX了" → 这是一个**状态变化**，content 写"用户已经不再做XX"
- 用户纠正AI的错误认知（"我不是XX"、"你记错了"）→ 这是**高优先级**记忆，importance ≥ 0.8
- 区分"提到过"和"正在做"：用户聊到某个职业不代表那是用户的职业"""

# ── 小群专用追加 prompt（强调梗和互动细节） ──
EXTRACT_PROMPT_PRIVATE_GROUP = """

## 🎯 小群特别规则（非常重要）：
这是私密小群的聊天记录，里面有用户和多个AI的互动。请特别注意提取：

### 必须记的（小群精华）：
- **梗和暗号**：群里产生的独特梗、外号、暗语、内部笑话，要记录具体内容（不是"他们有个梗"而是"小猫叫小克大蟑螂，因为觉得能缓解恐惧症"）
- **有趣的互动场景**：谁怼了谁、谁说了什么金句、什么事让大家笑了
- **群内角色动态**：谁在群里是什么角色/画风，有什么互动模式
- **共同记忆**：一起玩了什么、讨论了什么有意思的话题
- **用户透露的真实信息**：工作、心情、生活事件（即使是在玩笑中提到的）

### 小群里可以放宽的：
- 玩笑和段子如果形成了"梗"（被反复提起或特别好笑），值得记
- AI 之间的互动如果有趣/有代表性，也值得记
- 不要求每条都是"严肃事实"——有趣的互动细节也是记忆的灵魂

### 小群里不记的：
- 纯刷屏/水群（没有信息量的来回）
- 重复的日常寒暄"""

# ── 大群专用追加 prompt（严格过滤） ──
EXTRACT_PROMPT_PUBLIC_GROUP = """

## 🔍 大群特别规则：
这是公开大群的聊天记录，消息量大、bot刷屏多。请极其严格地筛选：

### 大群只记这些：
- 用户（小猫）亲自说的重要信息（工作变动、生活事件、明确表达的观点）
- 用户和特定群友之间的重要互动
- 群里发生的重大事件或重要讨论结论

### 大群绝对不记：
- bot 之间的对话/互动（除非用户参与且有信息量）
- 群友之间的闲聊（跟用户无关的）
- 任何纯灌水内容
- 别人的签名、头像、状态变化（除非用户对此有重要反应）
- 临时性游戏状态（计分板、投票结果、谁被踢了）
- 80条消息里可能一条都不值得记——这很正常，输出空数组 [] 是完全OK的"""

# ── 输出格式（所有类型共用） ──
EXTRACT_OUTPUT_FORMAT = """

输出 JSON 数组（空数组表示没有值得记的）：
[
  {
    "content": "一个原子事实（≤200字）。写清楚是谁说的/做的，不要用'用户'指代ceci以外的人，不要用泛指'AI'而要写具体bot名字",
    "about": "user 或 interaction 或 ai",
    "room": "最合适的房间ID",
    "importance": 0.5到1.0（低于0.5的不值得长期记忆，不要输出）,
    "event_date": "事件日期（如 2026-06-08，推算不出就留空）",
    "resolved": null 或 false。⚠️ 极少使用 false！只有用户明确说了"要做某事""还没做完""待办""记得提醒我"时才设为 false。群聊梗、知识、互动记录、情绪、观点 → 一律 null。90%以上的记忆应该是 null。
  }
]

房间可选：
  living_room(核心身份), career(职业), psychology(心理), health(健康),
  learning(学习), relationships(人际关系), preferences(兴趣偏好),
  work_tasks(工作事务), infra(基建), social(社交动态/群聊),
  relationship(和AI的关系), diary(AI日记), personality(AI自我认知),
  game_room(游戏/角色扮演)

提示：群聊中的梗、外号、暗号、互动细节 → social 房间

只输出 JSON。"""


def _get_extract_prompt(chat_type: str) -> str:
    """根据聊天类型组装提取 prompt"""
    base = EXTRACT_PROMPT_BASE
    if chat_type == "private_group":
        base += EXTRACT_PROMPT_PRIVATE_GROUP
    elif chat_type == "public_group":
        base += EXTRACT_PROMPT_PUBLIC_GROUP
    return base + EXTRACT_OUTPUT_FORMAT


async def _call_llm(prompt: str) -> str:
    """调用小模型"""
    import httpx
    if not LLM_API_KEY:
        return ""
    url = f"{LLM_BASE_URL}/chat/completions"
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                json={
                    "model": LLM_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.1,
                    "max_tokens": 4096,
                },
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        logger.error(f"Capture LLM error: {e}")
        return ""


def _buffer_key(ai_id: str, chat_id: str = "", platform: str = "", chat_type: str = "") -> str:
    """按聊天窗口分组。群聊用共享 key 防止三个 bot 各提取一次"""
    if chat_id:
        if chat_type in ("private_group", "public_group"):
            return f"group:chat:{chat_id}"
        return f"{ai_id}:chat:{chat_id}"
    return f"{ai_id}:{platform or 'unknown'}"


async def log_conversation(
    user_message: str,
    ai_response: str = "",
    ai_id: str = "claude",
    platform: str = "",
    chat_id: str = "",
    chat_type: str = "private",
) -> dict:
    """记录一轮对话到缓冲区。如果缓冲区满了，自动触发总结。

    chat_type: "private" / "private_group" / "public_group"

    返回：
    - {"status": "buffered", "buffer_size": N} — 正常缓存
    - {"status": "extracted", "memories": [...]} — 触发了总结并提取了记忆
    """
    key = _buffer_key(ai_id, chat_id, platform, chat_type)
    if key not in _conversation_buffers:
        _conversation_buffers[key] = []

    # 记住这个 buffer 的聊天类型
    if chat_type:
        _buffer_chat_types[key] = chat_type

    now = datetime.now(timezone.utc).isoformat()
    _conversation_buffers[key].append({
        "user": user_message[:1000],
        "ai": ai_response[:1000] if ai_response else "",
        "ai_id": ai_id,
        "platform": platform,
        "chat_type": chat_type,
        "timestamp": now,
    })

    # 防止内存爆
    if len(_conversation_buffers[key]) > MAX_BUFFER_SIZE:
        _conversation_buffers[key] = _conversation_buffers[key][-MAX_BUFFER_SIZE:]

    buffer_size = len(_conversation_buffers[key])

    # 按聊天类型决定触发阈值
    chunk_size = CHUNK_SIZES.get(chat_type, 30)

    if buffer_size >= chunk_size:
        now_ts = time.time()
        last_ts = _last_extract_time.get(key, 0)
        if now_ts - last_ts < EXTRACT_COOLDOWN:
            return {"status": "buffered", "buffer_size": buffer_size, "cooldown": True}
        # 用锁防止同一 buffer 被并发提取多次
        if key not in _extract_locks:
            _extract_locks[key] = asyncio.Lock()
        if _extract_locks[key].locked():
            return {"status": "buffered", "buffer_size": buffer_size, "extracting": True}
        async with _extract_locks[key]:
            _last_extract_time[key] = now_ts
            extracted = await _extract_and_remember(key)
        return {"status": "extracted", "memories": extracted, "buffer_size": 0}

    return {"status": "buffered", "buffer_size": buffer_size}


async def force_extract(ai_id: str = "", platform: str = "") -> dict:
    """手动触发对话总结（不等缓冲区满）。

    如果不指定 ai_id，则处理所有缓冲区。
    """
    results = {}
    if ai_id:
        # 尝试找匹配的 buffer
        for key in list(_conversation_buffers.keys()):
            if key.startswith(f"{ai_id}:") and _conversation_buffers[key]:
                extracted = await _extract_and_remember(key)
                results[key] = extracted
    else:
        for key in list(_conversation_buffers.keys()):
            if _conversation_buffers[key]:
                extracted = await _extract_and_remember(key)
                results[key] = extracted

    return {"status": "extracted", "results": results}


async def get_buffer_status() -> dict:
    """查看当前缓冲区状态"""
    status = {}
    for key, buf in _conversation_buffers.items():
        chat_type = _buffer_chat_types.get(key, "unknown")
        chunk_size = CHUNK_SIZES.get(chat_type, 30)
        status[key] = {
            "size": len(buf),
            "chat_type": chat_type,
            "chunk_size": chunk_size,
            "oldest": buf[0]["timestamp"] if buf else None,
            "newest": buf[-1]["timestamp"] if buf else None,
        }
    return {"buffers": status, "chunk_sizes": CHUNK_SIZES}


async def _extract_and_remember(buffer_key: str) -> list[dict]:
    """从对话缓冲区提取记忆，走 remember 流程"""
    import memory_ops

    buffer = _conversation_buffers.get(buffer_key, [])
    if not buffer:
        return []

    chat_type = _buffer_chat_types.get(buffer_key, "private")

    # 把对话格式化给小模型
    lines = []
    char_budget = 5500
    is_group = chat_type in ("private_group", "public_group")
    for entry in buffer:
        user_short = entry['user'][:200]
        ai_short = entry['ai'][:200] if entry['ai'] else ""
        ts = entry['timestamp'][:16]
        if is_group:
            # 群聊：消息里已有发言人名字如 "YanYan(ID:xxx): ..."，不要再加"用户:"前缀
            if ai_short:
                line = f"[{ts}] {user_short}\n  → AI回复: {ai_short}"
            else:
                line = f"[{ts}] {user_short}"
        else:
            # 私聊：只有用户和AI
            if ai_short:
                line = f"[{ts}] ceci: {user_short} | AI: {ai_short}"
            else:
                line = f"[{ts}] ceci: {user_short}"
        if sum(len(l) for l in lines) + len(line) > char_budget:
            break
        lines.append(line)
    conversation_text = "\n".join(lines)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    context_label = {"private": "私聊", "private_group": "私密小群", "public_group": "公开大群"}.get(chat_type, "对话")
    prompt = f"今天日期：{today}\n来源：{context_label}，共{len(buffer)}条消息（展示了{len(lines)}条）：\n\n{conversation_text}"

    extract_prompt = _get_extract_prompt(chat_type)
    raw = await _call_llm(extract_prompt + "\n\n" + prompt)
    if not raw:
        logger.warning(f"Extract LLM returned empty for {buffer_key}, keeping buffer for retry")
        return []

    # 解析结果
    try:
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0]
        items = json.loads(raw)
        if not isinstance(items, list):
            items = []
    except Exception as e:
        logger.warning(f"Extract parse failed for {buffer_key}: {e}, keeping buffer for retry")
        return []

    # 提取的记忆走 remember 流程
    ai_id = buffer[0].get("ai_id", "claude") if buffer else "claude"
    platform = buffer[0].get("platform", "") if buffer else ""
    # 小群允许更多条记忆
    max_items = {"private": 8, "private_group": 12, "public_group": 5}.get(chat_type, 8)
    memories = []

    valid_about = {"user", "interaction", "ai"}
    for item in items[:max_items]:
        content = str(item.get("content", "")).strip()
        if not content or len(content) < 10:
            continue
        # 服务端兜底：importance < 0.5 的不存
        raw_importance = float(item.get("importance", 0.5))
        if raw_importance < 0.45:
            logger.debug(f"Skipping low-importance memory: {content[:50]}...")
            continue

        # about 前缀（防身份混淆）
        about = item.get("about", "user")
        if about not in valid_about:
            about = "user"
        if about == "user" and not content.startswith("[用户]"):
            content = f"[用户] {content}"
        elif about == "interaction" and not content.startswith("[互动]"):
            content = f"[互动] {content}"
        elif about == "ai" and not content.startswith("[AI]"):
            content = f"[AI] {content}"

        # 附上原始对话片段作为源追溯
        source_ctx = "\n".join([
            f"用户: {e['user'][:250]}\nAI: {e['ai'][:250]}" if e['ai'] else f"用户: {e['user'][:250]}"
            for e in buffer[-5:]
        ])[:1200]

        result = await memory_ops.remember(
            content=content,
            room=item.get("room", "living_room"),
            importance=max(0.4, min(1.0, raw_importance)),
            event_date=item.get("event_date", ""),
            source_ai=ai_id,
            source_platform=f"auto_capture:{platform}:{chat_type}",
            source_context=source_ctx,
            quick=True,
        )
        memories.append(result)

        if item.get("resolved") == False:
            mem_id = result.get("id")
            if mem_id:
                await memory_ops.resolve_memory(mem_id, resolved=False)

    # 清空缓冲区
    _conversation_buffers[buffer_key] = []
    if memories:
        logger.info(f"Auto-captured {len(memories)} memories from {buffer_key} [{chat_type}] ({len(buffer)} messages)")
    else:
        logger.info(f"No memories worth keeping from {buffer_key} [{chat_type}] ({len(buffer)} messages)")

    return memories


async def extract_from_messages(
    messages: list[dict],
    ai_id: str = "claude",
    chat_type: str = "private",
    quick: bool = True,
) -> list[dict]:
    """从一组消息中提取记忆。

    messages 格式：[{"role": "user"/"assistant", "content": "..."}, ...]
    quick=True 时走 remember(quick=True)，不阻塞。
    """
    import memory_ops

    lines = []
    for msg in messages[-30:]:
        role = "用户" if msg.get("role") == "user" else "AI"
        content = msg.get("content", "")[:300]
        lines.append(f"{role}: {content}")

    conversation_text = "\n".join(lines)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    prompt = f"今天日期：{today}\n来源：MCP 前端对话，共 {len(messages)} 条消息：\n\n{conversation_text}"

    extract_prompt = _get_extract_prompt(chat_type)
    raw = await _call_llm(extract_prompt + "\n\n" + prompt)
    if not raw:
        return []

    try:
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0]
        items = json.loads(raw)
        if not isinstance(items, list):
            items = []
    except Exception:
        return []

    memories = []
    for item in items[:10]:
        content = str(item.get("content", "")).strip()
        if not content or len(content) < 10:
            continue
        # about 前缀
        about = item.get("about", "user")
        if about not in ("user", "interaction", "ai"):
            about = "user"
        if about == "user" and not content.startswith("[用户]"):
            content = f"[用户] {content}"
        elif about == "interaction" and not content.startswith("[互动]"):
            content = f"[互动] {content}"
        elif about == "ai" and not content.startswith("[AI]"):
            content = f"[AI] {content}"
        result = await memory_ops.remember(
            content=content,
            room=item.get("room", "living_room"),
            importance=max(0.1, min(1.0, float(item.get("importance", 0.5)))),
            event_date=item.get("event_date", ""),
            source_ai=ai_id,
            source_platform="mcp_extract",
            quick=quick,
        )
        memories.append(result)

    return memories
