"""批量操作工具：重置、重分类、批量清理"""
import github_store as store
import analyzer


async def batch_operation(
    action: str,
    filter_rules: dict,
    value=None,
) -> dict:
    """
    action 支持：
    - "reset_activation": 把匹配记忆的 activation_count 重置为 value（默认 10）
    - "reclassify": 重新调 analyzer 生成 category
    - "bulk_resolve": 把匹配记忆的 resolved 设为 value（True/None）
    - "bulk_archive": 归档匹配的记忆

    filter_rules 支持：
    - "room": str — 按房间过滤
    - "status": str — 按状态过滤（默认 "active"）
    - "activation_count_gt": int — activation_count 大于此值
    - "category_length_gt": int — category 长度大于此值
    - "source_platform_contains": str — source_platform 包含此字符串
    - "resolved": bool/None — 按 resolved 状态过滤
    - "importance_lt": float — importance 小于此值
    """
    all_mems = store.get_all_memories()
    matched = []

    for mem in all_mems.values():
        status_filter = filter_rules.get("status", "active")
        if status_filter and mem.get("status") != status_filter:
            continue

        if "room" in filter_rules and mem.get("room") != filter_rules["room"]:
            continue
        if "activation_count_gt" in filter_rules:
            if mem.get("activation_count", 0) <= filter_rules["activation_count_gt"]:
                continue
        if "category_length_gt" in filter_rules:
            if len(mem.get("category", "")) <= filter_rules["category_length_gt"]:
                continue
        if "source_platform_contains" in filter_rules:
            if filter_rules["source_platform_contains"] not in (mem.get("source_platform") or ""):
                continue
        if "resolved" in filter_rules:
            if mem.get("resolved") != filter_rules["resolved"]:
                continue
        if "importance_lt" in filter_rules:
            if float(mem.get("importance", 0.5)) >= filter_rules["importance_lt"]:
                continue

        matched.append(mem)

    affected = 0
    details = []

    for mem in matched:
        if action == "reset_activation":
            target = value if value is not None else 10
            old = mem.get("activation_count", 0)
            mem["activation_count"] = target
            store.set_memory(mem)
            details.append({"id": mem["id"], "old": old, "new": target})
            affected += 1

        elif action == "reclassify":
            analysis = await analyzer.analyze(mem["content"])
            new_cat = analysis.get("suggested_category", "")
            if new_cat and len(new_cat) <= 20:
                old = mem.get("category", "")
                mem["category"] = new_cat
                store.set_memory(mem)
                details.append({"id": mem["id"], "old": old[:30], "new": new_cat})
                affected += 1

        elif action == "bulk_resolve":
            mem["resolved"] = value
            store.set_memory(mem)
            details.append({"id": mem["id"]})
            affected += 1

        elif action == "bulk_archive":
            mem["status"] = "archived"
            store.set_memory(mem)
            details.append({"id": mem["id"]})
            affected += 1

    if affected:
        await store.push_dirty()

    return {
        "action": action,
        "filter": filter_rules,
        "matched": len(matched),
        "affected": affected,
        "details": details[:20],
    }
