"""修复迁移数据的 category：长度 > 20 的重新调 analyzer 生成"""
import asyncio
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import github_store as store
import analyzer

async def main():
    await store.load_all()
    all_mems = store.get_all_memories()
    fixed = 0
    for mem in all_mems.values():
        if mem.get("status") != "active":
            continue
        cat = mem.get("category", "")
        content = mem.get("content", "")
        if len(cat) > 20 and content.startswith(cat[:15]):
            analysis = await analyzer.analyze(content)
            new_cat = analysis.get("suggested_category", "")
            if new_cat and len(new_cat) <= 20:
                mem["category"] = new_cat
                store.set_memory(mem)
                print(f"  {mem['id']}: '{cat[:30]}...' → '{new_cat}'")
                fixed += 1
    await store.push_dirty()
    print(f"Fixed {fixed} categories.")

asyncio.run(main())
