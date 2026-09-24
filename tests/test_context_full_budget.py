"""context(mode=full) must not hard-cut the wake-up text mid-sentence at 3000 chars."""
import os
import sys

os.environ.setdefault("ALLOW_DEFAULT_HUB_SECRET", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from smart_context import trim_full_context


def test_full_keeps_sections_beyond_3000_chars():
    body = "【关于主人】\n" + "\n".join(f"· 第{i}条客厅记忆，内容比较长一些。" for i in range(150))
    text = body + "\n\n【待办/未完成】\n· 10月要交房租。"
    assert 3000 < len(text) < 8000
    assert trim_full_context(text, 3000) == text


def test_full_truncates_at_boundary_with_marker():
    text = "\n".join(f"· 这是一条很长的记忆内容编号{i}，结尾有句号。" for i in range(1000))
    out = trim_full_context(text, 3000)
    assert len(out) <= 8000
    assert "上下文已截断" in out
    last_line = out.split("\n...(")[0].rsplit("\n", 1)[-1]
    assert last_line.endswith("。")
