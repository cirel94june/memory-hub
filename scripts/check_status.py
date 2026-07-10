"""Quick status check script - run on VPS"""
import json, sys

with open("/tmp/mems.json") as f:
    d = json.load(f)

print(f"Total memories: {d['total']}")
print()

rooms = {}
layers = {}
empty_room = []
long_mems = []

for m in d["items"]:
    r = m.get("room") or "(empty)"
    rooms[r] = rooms.get(r, 0) + 1
    l = m.get("layer", "")
    layers[l] = layers.get(l, 0) + 1
    if not m.get("room"):
        empty_room.append(m["id"])
    if len(m.get("content", "")) > 500:
        long_mems.append((m["id"], m.get("room", ""), len(m["content"])))

print("By room:")
for k, v in sorted(rooms.items(), key=lambda x: -x[1]):
    print(f"  {k}: {v}")

print(f"\nBy layer:")
for k, v in sorted(layers.items(), key=lambda x: -x[1]):
    print(f"  {k}: {v}")

if empty_room:
    print(f"\nEmpty room ({len(empty_room)}): {empty_room}")

if long_mems:
    print(f"\nLong memories (>500 chars): {len(long_mems)}")
    for mid, room, length in long_mems[:5]:
        print(f"  {mid} [{room}] {length} chars")
