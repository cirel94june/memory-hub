"""
记忆分析器：自动打标、合并、拆分
使用中转站的小模型（OpenAI 兼容格式）
"""
import json
import logging
import httpx

from config import LLM_API_KEY, LLM_MODEL, LLM_BASE_URL

logger = logging.getLogger("memory_hub.analyzer")

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
3. arousal（情感唤醒度）：0.0~1.0，0=非常平静 → 0.5=普通 → 1.0=非常激动
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
arousal: 0~1（0=平静, 0.5=普通, 1=激动）"""


async def _call_llm(system_prompt: str, user_content: str, temperature: float = 0.1) -> str:
    """调用中转站小模型（OpenAI 兼容格式）"""
    if not LLM_API_KEY:
        logger.warning("LLM_API_KEY not set, skipping LLM call")
        return ""
    url = f"{LLM_BASE_URL}/chat/completions"
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            url,
            headers={"Authorization": f"Bearer {LLM_API_KEY}"},
            json={
                "model": LLM_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                "temperature": temperature,
                "max_tokens": 4096,
            },
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


def _parse_json(text: str):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        text = text.rsplit("```", 1)[0]
    return json.loads(text.strip())


async def analyze(content: str) -> dict:
    """自动分析内容，返回 domain/valence/arousal/tags/category"""
    try:
        raw = await _call_llm(ANALYZE_PROMPT, content)
        if not raw:
            return {"domain": [], "valence": 0.5, "arousal": 0.3, "tags": [], "suggested_category": ""}
        result = _parse_json(raw)
        return {
            "domain": result.get("domain", [])[:3],
            "valence": max(0.0, min(1.0, float(result.get("valence", 0.5)))),
            "arousal": max(0.0, min(1.0, float(result.get("arousal", 0.3)))),
            "tags": result.get("tags", [])[:15],
            "suggested_category": result.get("suggested_category", ""),
        }
    except Exception as e:
        logger.warning(f"Analyze failed: {e}")
        return {"domain": [], "valence": 0.5, "arousal": 0.3, "tags": [], "suggested_category": ""}


async def merge(old_content: str, new_content: str) -> str:
    """合并新旧记忆内容。安全校验：合并结果不能比原文短太多。"""
    try:
        prompt = f"=== 旧记忆 ===\n{old_content}\n\n=== 新内容 ===\n{new_content}"
        merged = await _call_llm(MERGE_PROMPT, prompt)
        if not merged:
            logger.warning("Merge LLM returned empty, keeping original")
            return f"{old_content}\n\n---更新---\n{new_content}"

        # 安全校验：合并结果不能比两段原文中较长的那段还短超过50%
        max_original = max(len(old_content), len(new_content))
        if len(merged) < max_original * 0.5:
            logger.warning(
                f"Merge result too short ({len(merged)} chars vs original {max_original} chars), "
                f"rejecting merge to prevent data loss"
            )
            return f"{old_content}\n\n---更新---\n{new_content}"

        return merged
    except Exception as e:
        logger.warning(f"Merge failed: {e}")
        return f"{old_content}\n\n---更新---\n{new_content}"


async def digest(content: str) -> list[dict]:
    """把长文本拆分成多条独立记忆"""
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
                continue  # 跳过空的或过短的拆分结果
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
        return result
    except Exception as e:
        logger.warning(f"Digest failed: {e}")
        return []
