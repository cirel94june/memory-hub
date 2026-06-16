"""
一次性脚本：重置 activation_count 异常高的记忆。
在 VPS 上运行：cd /opt/memory-hub && python3 fix_activation_counts.py
"""
import httpx

BASE = "http://localhost:8888"
SECRET = "588887pa"
HEADERS = {"Authorization": f"Bearer {SECRET}", "Content-Type": "application/json"}
THRESHOLD = 50
RESET_TO = 10

r = httpx.get(f"{BASE}/api/memory/list?per_page=500", headers=HEADERS, timeout=30)
data = r.json()
mems = data["items"]
print(f"Total: {len(mems)} memories")

fixed = 0
for m in mems:
    ac = m.get("activation_count", 0)
    if ac > THRESHOLD:
        print(f"  {m['id']} activation_count={ac} → {RESET_TO}  [{m['content'][:40]}]")
        r = httpx.put(
            f"{BASE}/api/memory/{m['id']}",
            headers=HEADERS,
            json={"content": m["content"], "changed_by": "fix_activation"},
            timeout=30,
        )
        if r.status_code == 200:
            fixed += 1
        else:
            print(f"    FAIL: {r.status_code}")

print(f"\nIdentified {fixed} memories with inflated counts.")
print("NOTE: The HTTP API update doesn't directly set activation_count.")
print("Run the following on the VPS to patch them directly:")
print("""
python3 -c "
import github_store as store
store.load_all_memories()
for m in store.get_all_memories().values():
    if m.get('activation_count', 0) > 50:
        print(f'  Reset {m[\"id\"]} from {m[\"activation_count\"]}')
        m['activation_count'] = 10
        store.set_memory(m)
import asyncio
asyncio.run(store.push_dirty())
print('Done')
"
""")
