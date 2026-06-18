import httpx, time

BASE = "http://localhost:8888"
SECRET = "xiaoke588887"
HEADERS = {"Authorization": f"Bearer {SECRET}", "Content-Type": "application/json"}

all_mems = []
page = 1
while True:
    r = httpx.get(f"{BASE}/api/memory/list?per_page=50&page={page}", headers=HEADERS, timeout=30)
    data = r.json()
    items = data.get("items", [])
    all_mems.extend(items)
    if len(items) < 50:
        break
    page += 1

print(f"Total: {len(all_mems)} memories to backfill")

success = 0
failed = 0
for i, m in enumerate(all_mems):
    mid = m["id"]
    label = m["content"][:50].replace("\n", " ")
    print(f"[{i+1}/{len(all_mems)}] {mid} {label}...", end=" ", flush=True)
    try:
        r = httpx.put(
            f"{BASE}/api/memory/{mid}",
            headers=HEADERS,
            json={"content": m["content"], "changed_by": "backfill_zh"},
            timeout=60,
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
    time.sleep(0.5)

print(f"\nDone: {success} OK, {failed} failed")
