"""一次性记忆审计：批量修复 about 前缀、检测重复、标记过时"""
import asyncio
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import github_store as store

from config import LLM_BASE_URL, LLM_API_KEY, LLM_MODEL
import httpx

AUDIT_PROMPT = """你是记忆审计专家。以下是一批记忆条目，请逐条审计并返回修正建议。

## 审计规则：

### 1. about 前缀（最重要）
每条记忆必须以 [用户]、[互动] 或 [AI] 开头：
- [用户] = 关于用户(小猫)的事实：工作、经历、偏好、情绪、人际关系、健康
- [互动] = 用户和AI之间的互动：一起做了什么、讨论了什么、AI被赋予的角色
- [AI] = AI自己的感悟/自省（极少）
如果已有正确前缀就保留，没有就加上。

### 2. 内容修正
- 把"小猫"统一（如果用了其他称呼）
- 如果内容包含"---更新---"分隔符，合并成一段流畅的文字
- 如果内容太长(>200字)，提取核心信息缩短
- 修正明显的时态问题：如果是过去的事要用"曾经"

### 3. 重复检测
如果发现两条记忆说的是同一件事（即使措辞不同），在较差的那条标记 duplicate=true

### 4. 过时/错误检测
如果记忆内容看起来可能过时或有问题，标记 needs_review=true 并说明原因

输入格式：每条记忆一行，格式为 ID|房间|来源AI|内容

输出纯 JSON 数组，每条对应输入的一条记忆：
[
  {
    "id": "原始ID",
    "action": "fix" 或 "keep" 或 "duplicate" 或 "review",
    "new_content": "修正后的内容（action=fix时必填，其他可省略）",
    "reason": "修改原因（简短）"
  }
]

只输出 JSON，不要解释。"""


async def call_llm(prompt: str) -> str:
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{LLM_BASE_URL.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {LLM_API_KEY}"},
            json={
                "model": LLM_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "max_tokens": 4000,
            },
        )
        data = resp.json()
        return data["choices"][0]["message"]["content"]


async def audit_batch(batch: list[dict]) -> list[dict]:
    lines = []
    for m in batch:
        content = m["content"].replace("\n", " ").replace("|", "｜")
        lines.append(f'{m["id"]}|{m.get("room","")}|{m.get("source_ai","")}|{content[:300]}')

    input_text = "\n".join(lines)
    prompt = AUDIT_PROMPT + "\n\n---\n" + input_text

    raw = await call_llm(prompt)
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0]
    return json.loads(raw)


async def main():
    await store.load_all()
    all_mems = store.get_all_memories()
    active = [m for m in all_mems.values() if m.get("status") == "active"]
    active.sort(key=lambda x: x.get("room", ""))

    print(f"审计 {len(active)} 条 active 记忆...")

    batch_size = 12
    total_fixed = 0
    total_duplicated = 0
    total_review = 0

    for i in range(0, len(active), batch_size):
        batch = active[i:i+batch_size]
        batch_num = i // batch_size + 1
        total_batches = (len(active) + batch_size - 1) // batch_size
        print(f"\n--- 批次 {batch_num}/{total_batches} ({len(batch)} 条) ---")

        try:
            results = await audit_batch(batch)
        except Exception as e:
            print(f"  批次失败: {e}")
            continue

        for result in results:
            mem_id = result.get("id", "")
            action = result.get("action", "keep")
            mem = store.get_memory(mem_id)
            if not mem:
                continue

            if action == "fix":
                new_content = result.get("new_content", "")
                if new_content and new_content != mem["content"]:
                    old_short = mem["content"][:60]
                    mem["content"] = new_content
                    store.set_memory(mem)
                    print(f"  FIX {mem_id}: {old_short}... → {new_content[:60]}...")
                    total_fixed += 1

            elif action == "duplicate":
                mem["status"] = "archived"
                store.set_memory(mem)
                print(f"  DUP {mem_id}: {mem['content'][:80]}...")
                total_duplicated += 1

            elif action == "review":
                reason = result.get("reason", "")
                print(f"  REVIEW {mem_id}: {reason} | {mem['content'][:80]}...")
                total_review += 1

    if total_fixed or total_duplicated:
        await store.push_dirty()

    print(f"\n===== 审计完成 =====")
    print(f"修复: {total_fixed}, 去重: {total_duplicated}, 待人工审核: {total_review}")


asyncio.run(main())
