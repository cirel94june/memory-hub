"""
记忆正文完整性检查。

历史上存在存库前就残缺的正文（如旧版 max_tokens 过小时被截断的梦，
停在「像是有个什么系统管」这种半句）。这类残缺发生在生成/写库阶段，
输出侧的边界截断管不到它们。

原则：不自动补写内容。只打 content_incomplete 标签，
在召回中降权、在最近动态中跳过，保留原文待人工审计。
"""

# 正常的句尾/闭合字符（宽松集合：宁可漏判，不可把正常内容误标）
_TERMINAL_CHARS = set(
    "。！？…”’』」）)】]〕》>\"'~♪—"
    "!?.;；:："
)

_BRACKET_PAIRS = [
    ("（", "）"), ("(", ")"),
    ("【", "】"), ("[", "]"),
    ("《", "》"), ("「", "」"), ("『", "』"),
]


def looks_incomplete(content: str) -> tuple[bool, str]:
    """启发式判断正文是否残缺。返回 (是否残缺, 原因)。"""
    text = (content or "").strip()
    if len(text) < 30:
        # 短文本（标签式记忆、外号等）不判，误伤率太高
        return False, ""

    if text.count("```") % 2 == 1:
        return True, "unclosed_code_fence"

    for opener, closer in _BRACKET_PAIRS:
        if text.count(opener) > text.count(closer):
            return True, f"unclosed_bracket:{opener}"

    # 中文引号成对（“ 与 ” 是不同字符）
    if text.count("“") > text.count("”"):
        return True, "unclosed_quote"

    if text[-1] not in _TERMINAL_CHARS:
        return True, "no_terminal_punctuation"

    return False, ""
