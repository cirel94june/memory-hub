import json
d = json.load(open("/tmp/mems.json"))
for m in d["items"]:
    r = m.get("room") or "(empty)"
    c = m["content"][:150].replace("\n", " ")
    owner = m.get("owner_ai", "")
    layer = m.get("layer", "")
    imp = m.get("importance", 0)
    print(f"[{r}] layer={layer} owner={owner} imp={imp}")
    print(f"  {c}")
    print()
