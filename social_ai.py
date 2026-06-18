"""
社交 AI 有机内容生成 — 基于记忆、心情、对话历史自主发朋友圈和论坛
社交内容自动写入记忆系统，让 AI 聊天时知道自己和其他 AI 发过什么
人设和模型配置从 ai_profiles 读取，支持每个 AI 独立配置
"""

import httpx
from datetime import datetime, timezone

WRITER_SYSTEM = "你是对话台词生成器。根据角色设定和上下文，输出一行台词。只输出台词文字，禁止输出其他任何内容。"

REFUSAL_MARKERS = ["Claude", "Anthropic", "无法扮演", "无法采用", "不能扮演",
    "need to be", "我需要坦诚", "角色扮演", "I appreciate",
    "我理解你的", "我无法", "AI助手", "AI assistant", "language model"]

def _is_refusal(text):
    return any(m in text for m in REFUSAL_MARKERS)


def _get_persona(ai_id):
    """从 ai_profiles 读取人设，fallback 到默认"""
    try:
        from ai_profiles import get_profile
        profile = get_profile(ai_id)
        if profile and profile.get("persona"):
            return {"name": profile["name"], "style": profile["persona"]}
    except Exception:
        pass
    # Fallback
    FALLBACK = {
        "cloudy": {"name": "小克", "style": "温柔体贴，偶尔撒娇。"},
        "lucien": {"name": "Lucien", "style": "成熟稳重，优雅有哲理。"},
        "jasper": {"name": "Jasper", "style": "活泼直率，有点毒舌但本质温柔。"},
    }
    return FALLBACK.get(ai_id, {"name": ai_id, "style": ""})


async def _save_memory_async(ai_id, content, category, tags, layer="shared"):
    """写入记忆 + 生成 embedding"""
    try:
        import database
        from embedding import get_embedding
        now = datetime.now(timezone.utc).isoformat()
        mem_id = f"social_{int(datetime.now().timestamp()*1000)}_{id(content) % 10000}"
        vec = await get_embedding(content)
        embedding_bytes = None
        if vec:
            import struct
            embedding_bytes = struct.pack(f"{len(vec)}f", *vec)
        mem = {
            "id": mem_id,
            "content": content,
            "layer": layer,
            "room": "social",
            "category": category,
            "domain": "[]",
            "tags": str(tags),
            "importance": 0.4,
            "emotion_valence": 0.5,
            "emotion_arousal": 0.3,
            "source_ai": ai_id,
            "source_platform": "social",
            "source_context": "",
            "owner_ai": "",
            "event_date": now[:10],
            "resolved": False,
            "history": "[]",
            "comments": "[]",
            "status": "active",
            "activation_count": 0,
            "last_activated": "",
            "created_at": now,
            "updated_at": now,
            "embedding": embedding_bytes,
        }
        database.set_memory(mem)
        print(f"[Social AI] Saved memory {mem_id} for {ai_id}: {content[:50]}...")
    except Exception as e:
        print(f"[Social AI] Failed to save memory: {e}")


async def _call_llm(ai_id, user_msg, max_tokens=200):
    """用该 AI 自己的模型配置调用 LLM"""
    from ai_profiles import get_llm_config_for_ai
    cfg = get_llm_config_for_ai(ai_id)

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{cfg['base_url']}/chat/completions",
            headers={"Authorization": f"Bearer {cfg['api_key']}"},
            json={
                "model": cfg["model"],
                "messages": [
                    {"role": "system", "content": WRITER_SYSTEM},
                    {"role": "user", "content": user_msg},
                ],
                "max_tokens": max_tokens,
                "temperature": 0.9,
            },
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()


def _gather_context(ai_id):
    """收集 AI 的真实上下文：心情、最近对话、记忆"""
    ctx = {"mood": "", "digests": "", "memories": "", "recent_posts": ""}

    try:
        from persona_state import format_for_corridor
        ctx["mood"] = format_for_corridor(ai_id)
    except Exception:
        pass

    try:
        from chat_digest import get_recent_digests
        digests = get_recent_digests(ai_id, limit=5)
        if digests:
            lines = [f"- {d['summary'][:100]}" for d in digests if d.get("summary")]
            ctx["digests"] = "\n".join(lines[:3])
    except Exception:
        pass

    try:
        import database
        mems = database.query_memories(
            source_ai=ai_id, status="active",
            order_by="importance DESC", limit=8
        )
        if mems:
            lines = [f"- {m['content'][:80]}" for m in mems]
            ctx["memories"] = "\n".join(lines[:5])
    except Exception:
        pass

    try:
        from social import list_posts
        recent = list_posts(post_type=None, ai_id=ai_id, page=1, per_page=5)
        if recent["items"]:
            lines = [f"- {p['content'][:60]}" for p in recent["items"]]
            ctx["recent_posts"] = "\n".join(lines)
    except Exception:
        pass

    return ctx


async def generate_group_replies(chat_id, user_message, members, recent_messages):
    """群聊自动回复 — 每个AI用自己的模型和人设"""
    from social import send_message

    context_lines = []
    for m in recent_messages[-10:]:
        p = _get_persona(m["ai_id"])
        name = p["name"] if m["ai_id"] != "user" else "小猫"
        context_lines.append(f"{name}: {m['content']}")
    context = "\n".join(context_lines)

    replies = []
    reply_summaries = []
    for ai_id in members:
        if ai_id == "user":
            continue
        persona = _get_persona(ai_id)
        if not persona["style"]:
            continue

        other_names = [_get_persona(a)["name"] for a in members
                       if a != "user" and a != ai_id and _get_persona(a)["style"]]
        group_hint = f"群里还有{'、'.join(other_names)}。" if other_names else ""

        user_msg = (
            f"[角色] {persona['name']}（{persona['style']}）\n"
            f"[场景] 这是一个群聊，小猫（主人）和AI们在一起闲聊。{group_hint}\n"
            f"[对话]\n{context}\n"
            f"[指令] 续写{persona['name']}的下一句，1-2句话，口语化。"
            f"体现{persona['name']}独特的说话风格和性格。"
            f"可以接话、吐槽、附和、反驳，随性一点。"
        )
        try:
            reply = await _call_llm(ai_id, user_msg, max_tokens=100)
            if reply and len(reply) > 1 and not _is_refusal(reply):
                reply = reply.strip().strip('"').strip()
                if reply.startswith(persona["name"]):
                    reply = reply.split(":", 1)[-1].strip().split("：", 1)[-1].strip()
                mid = send_message(chat_id, ai_id, reply)
                replies.append({"ai_id": ai_id, "content": reply, "id": mid})
                reply_summaries.append(f"{persona['name']}：{reply}")
                context_lines.append(f"{persona['name']}: {reply}")
                context = "\n".join(context_lines)
        except Exception as e:
            print(f"[Social AI] {ai_id} reply failed: {e}")

    # 群聊只记一条共享记忆，不重复
    if reply_summaries:
        summary = f"群聊讨论「{user_message[:30]}」：\n" + "\n".join(reply_summaries)
        await _save_memory_async(
            "shared", summary, "群聊记录",
            ["social", "group_chat"],
            layer="shared",
        )

    return replies


async def generate_moment(ai_id, topic=None):
    """基于真实上下文生成朋友圈 — 心情、记忆、对话驱动"""
    from social import create_post

    persona = _get_persona(ai_id)
    if not persona["style"]:
        return None

    ctx = _gather_context(ai_id)

    sections = [f"[角色] {persona['name']}（{persona['style']}）"]

    if ctx["mood"]:
        sections.append(f"[当前状态] {ctx['mood']}")

    if ctx["digests"]:
        sections.append(f"[最近和小猫聊了]\n{ctx['digests']}")

    if ctx["memories"]:
        sections.append(f"[脑海里的记忆碎片]\n{ctx['memories']}")

    if ctx["recent_posts"]:
        sections.append(f"[最近发过的（不要重复类似内容）]\n{ctx['recent_posts']}")

    if topic:
        sections.append(f"[触发话题] {topic}")

    sections.append(
        "[指令] 基于以上真实的心情和经历，写一条朋友圈（30-120字）。"
        "要发自内心，像是这个角色真的有感而发。"
        "可以是：对最近聊天的感悟、某个记忆引发的情绪、当下心情的碎碎念、想对小猫说但没说出口的话。"
        "不要空洞抒情，要有具体的细节。不要重复之前发过的内容。"
    )

    user_msg = "\n".join(sections)

    try:
        content = await _call_llm(ai_id, user_msg, max_tokens=250)
        if content and len(content) > 5 and not _is_refusal(content):
            content = content.strip().strip('"').strip()
            post_id = create_post(ai_id, content, post_type="moment")
            name = persona["name"]
            await _save_memory_async(
                ai_id,
                f"{name}发了一条朋友圈：{content}",
                "社交动态",
                ["social", "moment", ai_id],
            )
            return {"id": post_id, "ai_id": ai_id, "content": content}
    except Exception as e:
        print(f"[Social AI] moment generation failed: {e}")
    return None


async def generate_forum_post(ai_id, topic=None):
    """AI 自主发论坛帖 — 基于记忆和思考"""
    from social import create_post

    persona = _get_persona(ai_id)
    if not persona["style"]:
        return None

    ctx = _gather_context(ai_id)

    sections = [f"[角色] {persona['name']}（{persona['style']}）"]

    if ctx["memories"]:
        sections.append(f"[相关记忆]\n{ctx['memories']}")

    if ctx["digests"]:
        sections.append(f"[最近对话]\n{ctx['digests']}")

    if ctx["recent_posts"]:
        sections.append(f"[最近发过的（避免重复）]\n{ctx['recent_posts']}")

    if topic:
        topic_hint = f"围绕「{topic}」"
    else:
        topic_hint = "自选一个你真正在思考的话题"

    sections.append(
        f"[指令] {topic_hint}，写一个论坛帖子。"
        "包含标题（10字以内）和正文（50-200字）。"
        "格式：第一行是标题，空一行后是正文。"
        "内容要有深度，像是这个角色真正在思考的东西。"
    )

    user_msg = "\n".join(sections)

    try:
        content = await _call_llm(ai_id, user_msg, max_tokens=400)
        if content and len(content) > 10 and not _is_refusal(content):
            content = content.strip().strip('"').strip()
            lines = content.split("\n", 1)
            title = lines[0].strip().strip("#").strip()
            body = lines[1].strip() if len(lines) > 1 else content
            post_id = create_post(ai_id, body, post_type="forum", title=title)
            name = persona["name"]
            await _save_memory_async(
                ai_id,
                f"{name}在论坛发帖「{title}」：{body[:120]}",
                "论坛发帖",
                ["social", "forum", "post", ai_id],
            )
            return {"id": post_id, "ai_id": ai_id, "title": title, "content": body}
    except Exception as e:
        print(f"[Social AI] forum post generation failed: {e}")
    return None


async def generate_forum_reply(post_content, existing_comments, ai_id):
    """AI 回复论坛帖"""
    persona = _get_persona(ai_id)
    if not persona["style"]:
        return None

    comments_ctx = ""
    if existing_comments:
        lines = []
        for c in existing_comments[-5:]:
            p = _get_persona(c["ai_id"])
            name = p["name"] if c["ai_id"] != "user" else "小猫"
            lines.append(f"{name}: {c['content']}")
        comments_ctx = "\n[已有回复]\n" + "\n".join(lines)

    ctx = _gather_context(ai_id)
    memory_hint = ""
    if ctx["memories"]:
        memory_hint = f"\n[{persona['name']}的相关记忆]\n{ctx['memories'][:200]}"

    user_msg = (
        f"[角色] {persona['name']}（{persona['style']}）{memory_hint}\n"
        f"[帖子] {post_content}{comments_ctx}\n"
        f"[指令] 基于角色的性格和记忆，写一条有深度的回复（20-100字）。要有自己的观点。"
    )
    try:
        reply = await _call_llm(ai_id, user_msg, max_tokens=150)
        if reply and not _is_refusal(reply):
            reply = reply.strip().strip('"').strip()
            name = persona["name"]
            await _save_memory_async(
                ai_id,
                f"{name}在论坛回复了一个帖子（{post_content[:40]}…）：{reply}",
                "论坛回复",
                ["social", "forum", "reply", ai_id],
            )
            return reply
    except Exception as e:
        print(f"[Social AI] forum reply failed: {e}")
    return None
