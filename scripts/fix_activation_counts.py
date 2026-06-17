"""一次性修复：把所有 activation_count > 50 的 active 记忆重置为 10"""
import asyncio
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import github_store as store

async def main():
    await store.load_all()
    all_mems = store.get_all_memories()
    fixed = 0
    for mem in all_mems.values():
        if mem.get("status") == "active" and mem.get("activation_count", 0) > 50:
            old = mem["activation_count"]
            mem["activation_count"] = 10
            store.set_memory(mem)
            print(f"  {mem['id']}: {old} → 10")
            fixed += 1
    await store.push_dirty()
    print(f"Done. Fixed {fixed} memories.")

asyncio.run(main())
