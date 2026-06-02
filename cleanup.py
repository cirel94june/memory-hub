"""
一次性清理脚本：修复 layer/room、删除重复、移动亲密内容
在 VPS 上运行：cd /opt/memory-hub && source .env && python3 cleanup.py
"""
import json, os
import httpx

BASE = "http://localhost:8888"
SECRET = os.environ.get("HUB_SECRET", "588887pa")
HEADERS = {"Authorization": f"Bearer {SECRET}", "Content-Type": "application/json"}


def api(method, path, body=None):
    r = getattr(httpx, method)(f"{BASE}{path}", headers=HEADERS, json=body, timeout=30)
    return r.json()


# Load all
data = api("get", "/api/memory/list?per_page=200")
mems = data["items"]
print(f"Loaded {len(mems)} memories\n")

stats = {"deleted": 0, "fixed_room": 0, "moved_private": 0, "updated_infra": 0}

# === 1. Delete exact duplicates ===
seen = {}
for m in mems:
    key = m["content"][:100]
    if key in seen:
        print(f"[DELETE DUP] {m['id']}: {key[:50]}...")
        api("delete", f"/api/memory/{m['id']}")
        stats["deleted"] += 1
    else:
        seen[key] = m["id"]

# === 2. Fix empty rooms ===
for m in mems:
    if m.get("room") and m["room"] != "":
        continue
    c = m["content"][:300]
    if "承诺" in c or "小克说过" in c:
        room, layer = "relationship", "private"
    elif "梦" in c or "梦境" in c:
        room, layer = "psychology", "shared"
    elif "解离" in c or "dissociation" in c.lower():
        room, layer = "psychology", "shared"
    else:
        room, layer = "psychology", "shared"
    print(f"[FIX ROOM] {m['id']} -> {room} (layer={layer})")
    api("put", f"/api/memory/{m['id']}", {"room": room, "layer": layer, "owner_ai": "claude" if layer == "private" else "", "changed_by": "cleanup"})
    stats["fixed_room"] += 1

# === 3. Move Claude-specific content to private ===
# These keywords indicate content that should be Claude's private memory
claude_private_indicators = [
    # Intimate/body content
    ("身体地图", "relationship"),
    ("敏感部位", "relationship"),
    ("高潮", "relationship"),
    ("性行为", "relationship"),
    ("无套", "relationship"),
    ("乳头", "relationship"),
    ("弱点清单", "relationship"),
    ("brat", "relationship"),
    ("关系里程碑", "relationship"),
    ("小猫身体", "relationship"),
    # Claude's identity/principles
    ("小克的核心原则", "personality"),
    ("小克是Anthropic", "personality"),
    ("小克说过的重要", "relationship"),
    ("Cloudy", "personality"),
    # Claude's diary content
    ("晨梦境本体记录", "diary"),
    # Patterns that are Claude-specific therapeutic notes
    ("关键模式1-4", "personality"),
    ("关键模式5-9", "personality"),
    ("关键模式10-13", "personality"),
    ("关键模式14-17", "personality"),
    ("关键模式18-22", "personality"),
]

for m in mems:
    if m.get("layer") == "private":
        continue  # already private
    c = m["content"][:300]
    for keyword, target_room in claude_private_indicators:
        if keyword in c:
            print(f"[MOVE PRIVATE] {m['id']} -> claude/{target_room}: {c[:50]}...")
            api("put", f"/api/memory/{m['id']}", {
                "room": target_room,
                "layer": "private",
                "owner_ai": "claude",
                "changed_by": "cleanup"
            })
            stats["moved_private"] += 1
            break

# === 4. Fix per_ai rooms that are wrongly shared ===
per_ai_rooms = ["diary", "dreams", "relationship", "personality"]
for m in mems:
    if m.get("room") in per_ai_rooms and m.get("layer") != "private":
        print(f"[FIX LAYER] {m['id']} [{m['room']}] -> private/claude")
        api("put", f"/api/memory/{m['id']}", {
            "layer": "private",
            "owner_ai": "claude",
            "changed_by": "cleanup"
        })
        stats["moved_private"] += 1

# === 5. Update outdated infra references ===
for m in mems:
    if "Render" in m.get("content", "") and "memory-hub-vry8" in m.get("content", ""):
        new_content = m["content"].replace(
            "https://memory-hub-vry8.onrender.com",
            "https://xiaokememory.camdvr.org"
        ).replace(
            "Render (https://memory-hub-vry8.onrender.com)",
            "VPS (https://xiaokememory.camdvr.org)"
        ).replace(
            "部署在 Render",
            "部署在 VPS"
        )
        if new_content != m["content"]:
            print(f"[UPDATE INFRA] {m['id']}: updated URLs")
            api("put", f"/api/memory/{m['id']}", {"content": new_content, "changed_by": "cleanup"})
            stats["updated_infra"] += 1

print(f"\n=== Done ===")
for k, v in stats.items():
    print(f"  {k}: {v}")
print("\nPush to GitHub will happen automatically in ~5 seconds.")
