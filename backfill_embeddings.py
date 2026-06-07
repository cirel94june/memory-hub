"""
给缺少 embedding 的记忆补上向量。
在 VPS 上运行：cd /opt/memory-hub && python3 backfill_embeddings.py
Gemini 免费层有 rate limit，每分钟约15次，脚本会自动限速。
"""
import json, os, time, httpx

BASE = "http://localhost:8888"
SECRET = "588887pa"
HEADERS = {"Authorization": f"Bearer {SECRET}", "Content-Type": "application/json"}

# Get all memories
r = httpx.get(f"{BASE}/api/memory/list?per_page=200", headers=HEADERS, timeout=30)
data = r.json()
mems = data["items"]
print(f"Total: {len(mems)} memories to backfill\n")

success = 0
failed = 0
for i, m in enumerate(mems):
    label = m["content"][:50].replace("\n", " ")
    print(f"[{i+1}/{len(mems)}] {m['id']} [{m.get('room','')}] {label}...", end=" ")
    try:
        r = httpx.put(
            f"{BASE}/api/memory/{m['id']}",
            headers=HEADERS,
            json={"content": m["content"], "changed_by": "backfill"},
            timeout=30,
        )
        if r.status_code == 200:
            success += 1
            print("OK")
        else:
            print(f"FAIL({r.status_code})")
            failed += 1
    except Exception as e:
        print(f"ERR: {e}")
        failed += 1

    # Rate limit (HF API is more generous than Gemini)
    time.sleep(1)

print(f"\nDone: {success} OK, {failed} failed")
