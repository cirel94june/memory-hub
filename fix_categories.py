"""
一次性脚本：修复 category 字段异常的记忆。
迁移时有些记忆把 content 截断塞进了 category（导致 category 很长）。
遍历所有 active 记忆，对 category > 20 字符的重新分析并更新。

在 VPS 上运行：cd /opt/memory-hub && python3 fix_categories.py
"""
import asyncio
import github_store as store
import analyzer


async def main():
    store.load_all_memories()
    all_mems = store.get_all_memories()

    to_fix = []
    for m in all_mems.values():
        if m.get("status") != "active":
            continue
        cat = m.get("category", "")
        if len(cat) > 20:
            to_fix.append(m)

    print(f"Found {len(to_fix)} memories with oversized category (>20 chars)\n")

    fixed = 0
    for m in to_fix:
        old_cat = m["category"]
        content = m["content"]
        print(f"  {m['id']}: category='{old_cat[:40]}...'")
        print(f"    content: {content[:60]}")

        try:
            analysis = await analyzer.analyze(content)
            new_cat = analysis.get("suggested_category", "")
            if new_cat and len(new_cat) <= 20:
                m["category"] = new_cat
                m["updated_at"] = store._now() if hasattr(store, '_now') else ""
                store.set_memory(m)
                print(f"    → new category: '{new_cat}'")
                fixed += 1
            else:
                # Fallback: truncate to first meaningful word
                m["category"] = old_cat[:15]
                store.set_memory(m)
                print(f"    → truncated to: '{old_cat[:15]}'")
                fixed += 1
        except Exception as e:
            print(f"    ERROR: {e}")

        await asyncio.sleep(1)  # rate limit

    await store.push_dirty()
    print(f"\nFixed {fixed}/{len(to_fix)} memories.")


asyncio.run(main())
