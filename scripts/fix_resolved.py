"""修复被错标为 unresolved 的社交记忆"""
import asyncio
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import github_store as store

async def main():
    await store.load_all()
    all_mems = store.get_all_memories()
    fixed = 0
    for mem in all_mems.values():
        if (mem.get("status") == "active"
            and mem.get("room") == "social"
            and mem.get("resolved") == False):
            mem["resolved"] = None
            store.set_memory(mem)
            fixed += 1
    await store.push_dirty()
    print(f"Fixed {fixed} social memories.")

asyncio.run(main())
