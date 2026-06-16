"""
一次性脚本：resolve 掉不应该是待办的 unresolved 记忆（如"天气怎么样"等）。
在 VPS 上运行：cd /opt/memory-hub && python3 fix_resolve_bogus.py
"""
import asyncio
import github_store as store
import memory_ops

BOGUS_KEYWORDS = ["天气怎么样", "天气如何"]

async def main():
    store.load_all_memories()
    all_mems = store.get_all_memories()

    fixed = 0
    for m in all_mems.values():
        if m.get("resolved") != False or m.get("status") != "active":
            continue
        content = m.get("content", "")
        is_bogus = any(kw in content for kw in BOGUS_KEYWORDS)
        # 也 resolve auto_capture 来源的 social 记忆
        is_social_auto = (m.get("room") == "social" and "auto_capture" in (m.get("source_platform") or ""))
        if is_bogus or is_social_auto:
            print(f"  Resolving {m['id']}: {content[:60]}")
            await memory_ops.resolve_memory(m["id"], resolved=True)
            fixed += 1

    await store.push_dirty()
    print(f"\nResolved {fixed} bogus unresolved memories.")

asyncio.run(main())
