"""
静态同义词表（29 组）：search_raw 查询时自动展开，提高召回率。
只覆盖高频口语↔书面语场景，不引入外部 NLP 库。
"""

# 每组是一个 frozenset，组内任意词命中时展开为整组。
_GROUPS: list[frozenset[str]] = [
    # ── 家人称呼 ──
    frozenset({"妈妈", "母亲", "妈", "老妈", "娘"}),
    frozenset({"爸爸", "父亲", "爸", "老爸"}),
    frozenset({"奶奶", "祖母", "阿嬷"}),
    frozenset({"爷爷", "祖父", "阿公"}),
    frozenset({"外婆", "姥姥", "外祖母"}),
    frozenset({"外公", "姥爷", "外祖父"}),
    frozenset({"老公", "丈夫", "老伴", "先生"}),
    frozenset({"老婆", "妻子", "太太", "媳妇"}),
    frozenset({"哥哥", "兄弟", "哥"}),
    frozenset({"姐姐", "姐", "姊姊"}),
    frozenset({"弟弟", "弟"}),
    frozenset({"妹妹", "妹"}),
    frozenset({"儿子", "孩子", "小孩"}),
    frozenset({"女儿", "闺女"}),

    # ── 日常动作 ──
    frozenset({"吃饭", "聚餐", "吃东西", "用餐", "进食"}),
    frozenset({"睡觉", "入睡", "睡了", "休息"}),
    frozenset({"上班", "工作", "上工", "打工"}),
    frozenset({"回家", "到家", "回去"}),
    frozenset({"出门", "出去", "外出"}),
    frozenset({"买东西", "购物", "买了"}),
    frozenset({"看病", "就医", "去医院", "看医生"}),

    # ── 情绪词 ──
    frozenset({"开心", "高兴", "快乐", "幸福", "愉快"}),
    frozenset({"难过", "伤心", "悲伤", "不开心", "郁闷"}),
    frozenset({"生气", "愤怒", "发火", "恼火"}),
    frozenset({"害怕", "恐惧", "担心", "焦虑", "紧张"}),
    frozenset({"喜欢", "爱", "心动", "钟意"}),
    frozenset({"讨厌", "烦", "厌恶", "反感"}),
    frozenset({"累", "疲惫", "疲倦", "没力气"}),
    frozenset({"无聊", "无趣", "没意思"}),
]

_INDEX: dict[str, frozenset[str]] = {}
for group in _GROUPS:
    for word in group:
        _INDEX[word] = group


def expand(keyword: str) -> list[str]:
    """返回关键词及其同义词列表。无同义词时返回 [keyword]。"""
    keyword = keyword.strip()
    if not keyword:
        return []
    group = _INDEX.get(keyword)
    if group:
        return sorted(group)
    return [keyword]
