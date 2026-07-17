"""
统一的记忆可见性判断。所有读取入口（smart_context / recall / list /
search_by_tags / detail / anchors / corridor / feed 聚合）复用同一套规则，
不允许各入口自己写一份过滤逻辑。

规则：
- layer == "shared"（或缺失，历史数据默认共享）→ 所有 AI 可见
- layer == "private" → 仅 owner_ai 本人（含别名）可见
- private 但 owner_ai 为空 → 回退用 source_ai 判断；两者都空则任何 AI 都不可见
  （宁可漏注入，不可串台；这类脏数据由 doctor 审计出来人工修）

过滤必须发生在候选生成阶段（排序/缓存/activation 更新之前），
输出阶段的过滤只是最后一道保险。
"""
from config import AI_ALIASES, AI_ALIAS_GROUPS


def viewer_ids(ai_id: str) -> set[str]:
    """请求者身份的全部等价 id（canonical + 别名，如 gpt/lucien、cloudy/claude）。"""
    if not ai_id:
        return set()
    canonical = AI_ALIASES.get(ai_id, ai_id)
    ids = set(AI_ALIAS_GROUPS.get(canonical, [canonical]))
    ids.add(ai_id)
    ids.add(canonical)
    return ids


def can_view(mem: dict, ai_id: str) -> bool:
    """判断 ai_id 是否有权看到这条记忆。

    注意：这是 AI 之间的隐私边界。人类主人通过 Hub 前端（HUB_SECRET 鉴权）
    查看全库不走此函数。
    """
    layer = (mem.get("layer") or "shared").strip()
    if layer != "private":
        return True
    ids = viewer_ids(ai_id)
    if not ids:
        return False
    owner = (mem.get("owner_ai") or "").strip()
    if owner:
        return owner in ids or AI_ALIASES.get(owner, owner) in ids
    source = (mem.get("source_ai") or "").strip()
    if source:
        return source in ids or AI_ALIASES.get(source, source) in ids
    return False


def filter_visible(mems, ai_id: str) -> list:
    """批量过滤，保持原顺序。"""
    return [m for m in mems if can_view(m, ai_id)]
