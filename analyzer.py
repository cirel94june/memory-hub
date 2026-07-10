"""
记忆分析器：自动打标、合并、拆分
使用中转站的小模型（OpenAI 兼容格式）
支持运行时动态切换模型/中转站
"""
import json
import time
import logging
import httpx

import config
import activity_log

logger = logging.getLogger("memory_hub.analyzer")

# ── 运行时可变配置（可通过 API 动态修改）──
_runtime_config = {
    "llm_base_url": "",   # 空 = 用 config 默认值
    "llm_model": "",
    "llm_api_key": "",
}


def get_llm_config() -> dict:
    """获取当前生效的 LLM 配置"""
    return {
        "llm_base_url": _runtime_config["llm_base_url"] or config.LLM_BASE_URL,
        "llm_model": _runtime_config["llm_model"] or config.LLM_MODEL,
        "llm_api_key": _runtime_config["llm_api_key"] or config.LLM_API_KEY,
    }


def set_llm_config(base_url: str = "", model: str = "", api_key: str = ""):
    """运行时切换 LLM 配置（不重启服务）"""
    if base_url:
        _runtime_config["llm_base_url"] = base_url
    if model:
        _runtime_config["llm_model"] = model
    if api_key:
        _runtime_config["llm_api_key"] = api_key
    cfg = get_llm_config()
    activity_log.log_activity(
        "config", f"LLM 配置已更新: {cfg['llm_model']} @ {cfg['llm_base_url']}",
        model=cfg["llm_model"],
    )
    logger.info(f"LLM config updated: model={cfg['llm_model']}, base_url={cfg['llm_base_url']}")


def reset_llm_config():
    """重置为 .env 默认配置"""
    _runtime_config["llm_base_url"] = ""
    _runtime_config["llm_model"] = ""
    _runtime_config["llm_api_key"] = ""
    activity_log.log_activity("config", "LLM 配置已重置为默认值")


ANALYZE_PROMPT = """你是一个内容分析器。请分析以下文本，输出结构化的元数据。

分析规则：
1. domain（主题域）：选最精确的 1~3 个
   日常: ["饮食", "穿搭", "出行", "居家", "购物"]
   人际: ["家庭", "恋爱", "友谊", "社交"]
   成长: ["工作", "学习", "考试", "求职"]
   身心: ["健康", "心理", "睡眠", "运动"]
   兴趣: ["游戏", "影视", "音乐", "阅读", "创作", "手工"]
   数字: ["编程", "AI", "硬件", "网络"]
   事务: ["财务", "计划", "待办"]
   内心: ["情绪", "回忆", "梦境", "自省"]
2. valence（情感效价）：0.0~1.0，0=极度消极 → 0.5=中性 → 1.0=极度积极
3. arousal（情感分量）：0.0~1.0，衡量内容的情感重要性和深度，不是文字语气。0=无关紧要的琐事 → 0.3=日常信息 → 0.5=有一定情感意义 → 0.7=深层情感/创伤/重要关系 → 1.0=核心身份认同/生死攸关。注意：心理创伤、原生家庭伤害、深层恐惧等即使平静描述也应≥0.7
4. tags：先从原文精准提取 3~5 个核心词，再引申扩展 8~10 个语义相关词（近义词、上位词、关联场景词、用户可能用不同措辞搜索的词），合并为一个数组
5. suggested_category：10字以内的简短分类名

输出格式（纯 JSON，无其他内容）：
{
  "domain": ["主题域1", "主题域2"],
  "valence": 0.7,
  "arousal": 0.4,
  "tags": ["核心词1", "核心词2", "扩展词1", "扩展词2"],
  "suggested_category": "简短分类名"
}"""

MERGE_PROMPT = """你是一个信息合并专家。请将旧记忆与新内容合并为一份统一的简洁记录。

合并规则：
1. 新内容与旧记忆冲突时，以新内容为准
2. 去除重复信息
3. 保留所有重要事实
4. 总长度尽量不超过旧记忆的 120%

直接输出合并后的文本，不要加额外说明。"""

DIGEST_PROMPT = """你是一个日记整理专家。用户会发送一段包含各种事情的文本（可能很杂乱），请你将其拆分成多个独立的记忆条目。

整理规则：
1. 每个条目应该是一个独立的主题/事件
2. 去除无意义的口水话和重复信息，保留核心内容
3. 同一主题的零散信息应合并为一个条目
4. 单个条目内容不少于50字，过短的零碎信息合并到最相关的条目中
5. 总条目数控制在 2~6 个

输出格式（纯 JSON 数组，无其他内容）：
[
  {
    "name": "条目标题（10字以内）",
    "content": "整理后的内容",
    "domain": ["主题域1"],
    "valence": 0.7,
    "arousal": 0.4,
    "tags": ["核心词1", "核心词2", "扩展词1", "扩展词2"],
    "importance": 5,
    "room": "建议房间ID"
  }
]

tags 规则：先从原文精准提取 3~5 个核心词，再引申扩展 5~8 个语义相关词，合并为一个数组。

主题域可选（选最精确的 1~3 个）：
  日常: ["饮食", "穿搭", "出行", "居家", "购物"]
  人际: ["家庭", "恋爱", "友谊", "社交"]
  成长: ["工作", "学习", "考试", "求职"]
  身心: ["健康", "心理", "睡眠", "运动"]
  兴趣: ["游戏", "影视", "音乐", "阅读", "创作", "手工"]
  数字: ["编程", "AI", "硬件", "网络"]
  事务: ["财务", "计划", "待办"]
  内心: ["情绪", "回忆", "梦境", "自省"]

room 可选（选最合适的一个）：
  living_room(核心身份), career(职业), psychology(心理), health(健康),
  learning(学习), relationships(人际关系), preferences(兴趣偏好),
  work_tasks(工作事务), infra(基建), infra_changelog(基建日志),
  diary(AI日记), dreams(梦境/自省), relationship(和用户关系), personality(AI自我认知)

importance: 1-10 归一化到 0-1（输出 0.1~1.0）
valence: 0~1（0=消极, 0.5=中性, 1=积极）
arousal: 0~1（情感分量，不是语气。0=琐事, 0.3=日常, 0.5=有意义, 0.7=深层情感/创伤, 1=核心身份）"""


CLASSIFY_RELATION_PROMPT = """你是一个记忆关系分析器。给你一条新记忆和若干旧记忆候选，请判断新记忆和旧记忆的关系。

关系类型：
- updates: 新记忆是对旧记忆的更新/后续（如"换了工作"更新"在XX公司上班"）
- contradicts: 新记忆与旧记忆矛盾（以新为准）
- supplements: 新记忆补充了旧记忆的细节
- same_topic: 同一主题但各自独立
- unrelated: 无关

输出纯 JSON：
{
  "relations": [
    {
      "target_id": "旧记忆ID",
      "relation": "updates/contradicts/supplements/same_topic/unrelated",
      "confidence": 0.8,
      "should_supersede": true,
      "reason": "简短原因"
    }
  ]
}

规则：
- should_supersede=true 表示旧记忆已过时，应被新记忆取代
- updates+should_supersede=true: 旧记忆标记为 superseded
- contradicts+should_supersede=true: 旧记忆标记为 superseded
- supplements: 不取代，但建立关联
- 最多输出 3 条关系

⚠️ 特别注意否定性更新：
- "不再是XX"、"已经不是XX了"、"不做XX了" → 如果旧记忆说"是XX"，这是 contradicts + should_supersede=true
- "用户纠正：我不是XX" → contradicts + should_supersede=true（用户主动纠正的优先级最高）
- "曾经做过XX" + 旧记忆说"正在做XX" → updates + should_supersede=true
- 只输出 JSON"""


async def _call_llm(system_prompt: str, user_content: str, temperature: float = 0.1) -> str:
    """调用中转站小模型（OpenAI 兼容格式），带活动日志"""
    cfg = get_llm_config()
    api_key = cfg["llm_api_key"]
    base_url = cfg["llm_base_url"]
    model = cfg["llm_model"]

    if not api_key:
        logger.warning("LLM_API_KEY not set, skipping LLM call")
        return ""

    url = f"{base_url}/chat/completions"
    t0 = time.time()

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_content},
                    ],
                    "temperature": temperature,
                    "max_tokens": 4096,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            if not content or not content.strip():
                logger.warning(f"LLM returned empty content, status={resp.status_code}, "
                             f"finish_reason={data['choices'][0].get('finish_reason')}")
                activity_log.log_activity(
                    "error", f"LLM 返回空内容 (finish={data['choices'][0].get('finish_reason')})",
                    model=model, duration_ms=int((time.time() - t0) * 1000), success=False,
                )
            return content

    except Exception as e:
        duration_ms = int((time.time() - t0) * 1000)
        activity_log.log_activity(
            "error", f"LLM 调用失败: {str(e)[:200]}",
            model=model, duration_ms=duration_ms, success=False,
        )
        raise


def _parse_json(text: str):
    if not text or not text.strip():
        raise ValueError("LLM returned empty response")
    text = text.strip()
    # 去掉 markdown code block 包裹
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        text = text.rsplit("```", 1)[0]
    text = text.strip()
    # 有些模型会在 JSON 前后加说明文字，尝试提取 JSON 部分
    if not text.startswith(("{", "[")):
        # 找第一个 { 或 [
        for i, c in enumerate(text):
            if c in ("{", "["):
                text = text[i:]
                break
    if text.endswith(("}", "]")):
        # 找最后一个 } 或 ]
        for i in range(len(text) - 1, -1, -1):
            if text[i] in ("}", "]"):
                text = text[:i+1]
                break
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        logger.warning(f"JSON parse failed, raw text (first 300 chars): {text[:300]}")
        raise


def _glossary_suffix() -> str:
    """人物速查：防止打标模型把"小猫/狗蛋"等人名当成宠物或动物话题。"""
    try:
        import identity_registry
        return "\n\n" + identity_registry.glossary_text() + \
            "\n注意：以上人名即使字面像动物（如小猫、狗蛋），也都是**人**，domain/tags 不要打成宠物、动物类。"
    except Exception:
        return ""


async def analyze(content: str) -> dict:
    """自动分析内容，返回 domain/valence/arousal/tags/category"""
    t0 = time.time()
    cfg = get_llm_config()
    try:
        raw = await _call_llm(ANALYZE_PROMPT + _glossary_suffix(), content)
        if not raw:
            return {"domain": [], "valence": 0.5, "arousal": 0.3, "tags": [], "suggested_category": ""}
        result = _parse_json(raw)
        parsed = {
            "domain": result.get("domain", [])[:3],
            "valence": max(0.0, min(1.0, float(result.get("valence", 0.5)))),
            "arousal": max(0.0, min(1.0, float(result.get("arousal", 0.3)))),
            "tags": result.get("tags", [])[:15],
            "suggested_category": result.get("suggested_category", ""),
        }
        activity_log.log_activity(
            "analyze",
            f"分析: {content[:60]}... → {parsed['suggested_category']}",
            model=cfg["llm_model"],
            duration_ms=int((time.time() - t0) * 1000),
            extra={"domain": parsed["domain"], "category": parsed["suggested_category"]},
        )
        return parsed
    except Exception as e:
        logger.warning(f"Analyze failed: {e}")
        activity_log.log_activity(
            "analyze", f"分析失败: {str(e)[:200]}",
            model=cfg["llm_model"],
            duration_ms=int((time.time() - t0) * 1000),
            success=False,
        )
        return {"domain": [], "valence": 0.5, "arousal": 0.3, "tags": [], "suggested_category": ""}


async def merge(old_content: str, new_content: str) -> str:
    """合并新旧记忆内容。安全校验：合并结果不能比原文短太多。"""
    t0 = time.time()
    cfg = get_llm_config()
    try:
        prompt = f"=== 旧记忆 ===\n{old_content}\n\n=== 新内容 ===\n{new_content}"
        merged = await _call_llm(MERGE_PROMPT, prompt)
        if not merged:
            logger.warning("Merge LLM returned empty, keeping original")
            activity_log.log_activity(
                "merge", "合并返回空结果，保留原文",
                model=cfg["llm_model"],
                duration_ms=int((time.time() - t0) * 1000),
                success=False,
            )
            return f"{old_content}\n\n---更新---\n{new_content}"

        max_original = max(len(old_content), len(new_content))
        if len(merged) < max_original * 0.5:
            logger.warning(
                f"Merge result too short ({len(merged)} chars vs original {max_original} chars), "
                f"rejecting merge to prevent data loss"
            )
            activity_log.log_activity(
                "merge",
                f"合并结果过短被拒绝: {len(merged)}字 vs 原文{max_original}字",
                model=cfg["llm_model"],
                duration_ms=int((time.time() - t0) * 1000),
                success=False,
                extra={"merged_len": len(merged), "original_len": max_original},
            )
            return f"{old_content}\n\n---更新---\n{new_content}"

        activity_log.log_activity(
            "merge",
            f"合并成功: {len(old_content)}字+{len(new_content)}字 → {len(merged)}字",
            model=cfg["llm_model"],
            duration_ms=int((time.time() - t0) * 1000),
        )
        return merged
    except Exception as e:
        logger.warning(f"Merge failed: {e}")
        activity_log.log_activity(
            "merge", f"合并失败: {str(e)[:200]}",
            model=cfg["llm_model"],
            duration_ms=int((time.time() - t0) * 1000),
            success=False,
        )
        return f"{old_content}\n\n---更新---\n{new_content}"


async def classify_relation(new_content: str, candidates: list[dict]) -> dict:
    """判断新记忆和旧记忆候选的关系"""
    if not candidates:
        return {"relations": []}
    t0 = time.time()
    cfg = get_llm_config()
    try:
        candidate_text = "\n".join([
            f"[{c['id']}] {c['content'][:200]}"
            for c in candidates[:5]
        ])
        prompt = f"=== 新记忆 ===\n{new_content}\n\n=== 旧记忆候选 ===\n{candidate_text}"
        raw = await _call_llm(CLASSIFY_RELATION_PROMPT, prompt)
        if not raw:
            return {"relations": []}
        result = _parse_json(raw)
        relations = []
        for r in result.get("relations", [])[:3]:
            if r.get("relation") == "unrelated":
                continue
            relations.append({
                "target_id": str(r.get("target_id", "")),
                "relation": r.get("relation", "same_topic"),
                "confidence": max(0.0, min(1.0, float(r.get("confidence", 0.5)))),
                "should_supersede": bool(r.get("should_supersede", False)),
                "reason": str(r.get("reason", ""))[:100],
            })
        activity_log.log_activity(
            "relation",
            f"关系分类: {len(relations)} 条关系 (候选{len(candidates)}条)",
            model=cfg["llm_model"],
            duration_ms=int((time.time() - t0) * 1000),
            extra={"relations": [r["relation"] for r in relations]},
        )
        return {"relations": relations}
    except Exception as e:
        logger.warning(f"Classify relation failed: {e}")
        activity_log.log_activity(
            "relation", f"关系分类失败: {str(e)[:200]}",
            model=cfg["llm_model"],
            duration_ms=int((time.time() - t0) * 1000),
            success=False,
        )
        return {"relations": []}


async def digest(content: str) -> list[dict]:
    """把长文本拆分成多条独立记忆"""
    t0 = time.time()
    cfg = get_llm_config()
    try:
        raw = await _call_llm(DIGEST_PROMPT, content, temperature=0.0)
        if not raw:
            return []
        items = _parse_json(raw)
        if not isinstance(items, list):
            return []
        result = []
        for item in items[:6]:
            item_content = str(item.get("content", ""))
            if not item_content or len(item_content) < 10:
                continue
            result.append({
                "name": str(item.get("name", ""))[:20],
                "content": item_content,
                "domain": (item.get("domain") or [])[:3],
                "valence": max(0.0, min(1.0, float(item.get("valence", 0.5)))),
                "arousal": max(0.0, min(1.0, float(item.get("arousal", 0.3)))),
                "tags": (item.get("tags") or [])[:15],
                "importance": max(0.1, min(1.0, float(item.get("importance", 0.5)))),
                "room": str(item.get("room", "living_room")),
            })
        activity_log.log_activity(
            "digest",
            f"拆分完成: {len(content)}字 → {len(result)} 条记忆",
            model=cfg["llm_model"],
            duration_ms=int((time.time() - t0) * 1000),
            extra={"input_len": len(content), "output_count": len(result)},
        )
        return result
    except Exception as e:
        logger.warning(f"Digest failed: {e}")
        activity_log.log_activity(
            "digest", f"拆分失败: {str(e)[:200]}",
            model=cfg["llm_model"],
            duration_ms=int((time.time() - t0) * 1000),
            success=False,
        )
        return []
