"""Resolve-pattern detection: 判断文本是否 POSITIVELY 断言"某事已完成"。

抽取自 memory_ops.py（Phase 2.0 Step 0-A）以避免 database.py 反向 import
memory_ops.py 造成循环依赖——database.check_auto_resolve_atomic 与
memory_ops._check_auto_resolve 需要复用同一份 pattern 逻辑。

现有语义完整保留：中英词表、否定/条件/疑问分句检测、NFKC 归一。
"""
import re
import unicodedata


_RESOLVE_PATTERNS_ZH = (
    "已完成", "搞定了", "做完了", "已解决", "完成了",
    "已经做了", "办好了", "改了", "改完了", "好了",
    "弄好了", "处理了", "处理完了", "OK了", "ok了",
    "搞好了", "修好了", "解决了", "已经弄好",
)
_RESOLVE_PATTERNS_EN = (
    "Done", "done", "finished", "fixed",
)
_RESOLVE_NEGATION_ZH = re.compile(r"(?:没|不|未|还没|没有|别|不要|还不|并没)")
_RESOLVE_CONDITIONAL_ZH = re.compile(r"(?:如果|要是|假如|若|万一|是不是|不确定|可能|也许|或许|大概)")
_RESOLVE_DOUBT_ZH = re.compile(r"(?:吗|吧|呢|么|嘛|？|\?)")

_RESOLVE_NEGATION_EN = re.compile(
    r"\b(?:not|never|haven'?t|hasn'?t|didn'?t|don'?t|doesn'?t|isn'?t|wasn'?t|un)\b",
    re.IGNORECASE,
)
_RESOLVE_CONDITIONAL_EN = re.compile(
    r"\b(?:if|whether|wonder|maybe|perhaps|might|could|would|should|possibly|probably|unsure|not sure)\b",
    re.IGNORECASE,
)


# Split on strong clause boundaries and English/Chinese transitional connectives.
# NOTE: applied AFTER NFKC normalization, so ，；() all become halfwidth.
# Transitional connectives split whether or not a comma precedes them — they
# introduce a new independent clause either way.
_CLAUSE_SPLIT_RE = re.compile(
    r"[.!。！\n;]"
    r"|\b(?:but|however)\b\s+"                   # English: word-bounded
    # Chinese transitionals — 但是/可是/然而 split unconditionally (no common
    # substring collisions with normal usage). 不过 is deliberately restrictive
    # because it forms many legit compounds (不过滤/不过期/不过夜/不过分/
    # 不过是/只不过, and "结果，不过是..." patterns where preceding punctuation
    # does not disambiguate). 不过 only splits when followed by an explicit
    # continuation marker — preferring false negatives over false positives,
    # since a missed auto-resolve is cheap but a wrong auto-resolve is not.
    r"|(?:但是|可是|然而"
    r"|不过(?=现在|如今|后来|最终|这次|目前|终于|真的|之前|以前))",
    re.IGNORECASE,
)


def _matches_resolve_pattern(text: str) -> bool:
    """Check if text POSITIVELY asserts completion — not just mentions it.

    Splits on strong clause boundaries (. ! 。 ！ ; ；) and transitional
    connectives (but / 但是 / 不过 / 可是) so a positive clause after a
    negative/conditional one is still recognized. Each clause must:
      - contain no question mark (globally rejects doubt);
      - contain no negation of the resolve verb;
      - contain no conditional/uncertainty marker before the verb.
    NFKC-normalized; English uses word boundaries.
    """
    normalized = unicodedata.normalize("NFKC", text)

    clauses = _CLAUSE_SPLIT_RE.split(normalized)
    for clause in clauses:
        if not clause or not clause.strip():
            continue
        if "?" in clause or "？" in clause:
            continue

        matched = False
        for pat in _RESOLVE_PATTERNS_EN:
            for m in re.finditer(r'\b' + re.escape(pat) + r'\b', clause, re.IGNORECASE):
                before = clause[:m.start()]
                if _RESOLVE_NEGATION_EN.search(before):
                    continue
                if _RESOLVE_CONDITIONAL_EN.search(before):
                    continue
                matched = True
                break
            if matched:
                break
        if matched:
            return True

        for pat in _RESOLVE_PATTERNS_ZH:
            for m in re.finditer(re.escape(pat), clause):
                before = clause[:m.start()]
                after = clause[m.end():m.end() + 3]
                if _RESOLVE_NEGATION_ZH.search(before):
                    continue
                if _RESOLVE_CONDITIONAL_ZH.search(before):
                    continue
                if _RESOLVE_DOUBT_ZH.search(after):
                    continue
                matched = True
                break
            if matched:
                break
        if matched:
            return True

    return False
