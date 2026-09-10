"""说话风格的最后一道关。

为什么需要它：如果把"不许用 emoji、不许打句号、不许说加油"这类规则一条条塞进提示词，
模型会写得非常拘谨，句子变得僵硬，反而更不像人。所以分工是：

- **提示词**负责描述她是个什么样的人，让模型自由发挥。
- **这里**负责在消息发出去之前，把不符合她习惯的地方拦下来。

拦下来之后分两种处理：

- 能机械修的（句尾句号、emoji、感叹号）直接修掉，不惊动模型。
- 不能机械修的（说了"加油"、整句英文、句子太长）返回违规清单，
  由 ``brain`` 带着这份清单让模型重写一次。重写还不过就退回到机械修剪。
"""

from __future__ import annotations

import re

from .models import ReplyPart, StyleViolation
from .persona import Boundaries, StyleConfig

_EMOJI = re.compile(
    "["
    "\U0001f300-\U0001faff"
    "\U00002600-\U000027bf"
    "\U0001f1e6-\U0001f1ff"
    "\U00002b00-\U00002bff"
    "\U0000fe0f"
    "\U00002190-\U000021ff"
    "\U00002300-\U000023ff"
    "]+"
)
_CJK = re.compile(r"[一-鿿]")
_LATIN_WORD = re.compile(r"[A-Za-z]{2,}")
_SENTENCE_SPLIT = re.compile(r"[。！？!?\n]+")
_TRAILING_PUNCT = re.compile(r"[。\.！!\s]+$")


def strip_emoji(text: str) -> str:
    return _EMOJI.sub("", text)


def normalize_punctuation(text: str, style: StyleConfig) -> str:
    """按她的习惯清理标点：不打句号，不用感叹号。

    句中的句号和感叹号变成空格（等于她换一口气继续说），句尾的直接去掉。
    英文句点只动句尾，免得把小数和缩写改坏。
    """
    out = text
    if style.forbid_exclamation:
        out = out.replace("！", " ").replace("!", " ")
    if style.strip_trailing_period:
        out = out.replace("。", " ")
        out = _TRAILING_PUNCT.sub("", out)
    # 清理连续空格，但保留换行
    out = re.sub(r"[ \t]{2,}", " ", out)
    return out.strip()


def sanitize(text: str, style: StyleConfig) -> str:
    """机械清理。不改变语义，只改标点和 emoji。"""
    out = text
    if style.forbid_emoji:
        out = strip_emoji(out)
    out = normalize_punctuation(out, style)
    return out


def sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT.split(text) if s.strip()]


def is_full_english(text: str, style: StyleConfig) -> bool:
    """整句英文才算违规，词级夹杂是她的正常说话方式。"""
    if not style.forbid_full_english_sentence:
        return False
    if _CJK.search(text):
        return False
    return len(_LATIN_WORD.findall(text)) >= 3


def check(
    parts: list[ReplyPart], style: StyleConfig, boundaries: Boundaries
) -> list[StyleViolation]:
    """检查一组气泡，返回违规清单。``fixable=False`` 的需要模型重写。"""
    issues: list[StyleViolation] = []

    if len(parts) > style.max_parts + 1:
        issues.append(
            StyleViolation(
                kind="too_many_parts",
                detail=f"一次发了 {len(parts)} 条，她一般最多 {style.max_parts} 条",
                fixable=True,
            )
        )

    all_sentences = 0
    long_sentences: list[tuple[int, str]] = []

    for i, part in enumerate(parts):
        text = part.text

        for phrase in boundaries.never_say:
            if phrase in text:
                issues.append(
                    StyleViolation(
                        kind="banned_phrase",
                        detail=f"用了她不会说的话：{phrase}",
                        part_index=i,
                        fixable=False,
                    )
                )

        if style.forbid_emoji and _EMOJI.search(text):
            issues.append(
                StyleViolation(kind="emoji", detail="出现 emoji", part_index=i, fixable=True)
            )

        if style.forbid_exclamation and ("！" in text or "!" in text):
            issues.append(
                StyleViolation(kind="exclamation", detail="出现感叹号", part_index=i, fixable=True)
            )

        if is_full_english(text, style):
            issues.append(
                StyleViolation(
                    kind="full_english",
                    detail="整句英文，她只做词级夹杂",
                    part_index=i,
                    fixable=False,
                )
            )

        for sentence in sentences(text):
            all_sentences += 1
            if len(sentence) > style.long_sentence_chars:
                long_sentences.append((i, sentence))

    # 长句本身不违规，超出预算才违规。她偶尔说重话，那是有意义的。
    budget = max(1, int(all_sentences * style.long_sentence_budget + 0.999))
    if len(long_sentences) > budget:
        for i, sentence in long_sentences[budget:]:
            issues.append(
                StyleViolation(
                    kind="too_long",
                    detail=f"句子太长（{len(sentence)} 字）：{sentence[:30]}…",
                    part_index=i,
                    fixable=False,
                )
            )

    return issues


def apply_fixes(parts: list[ReplyPart], style: StyleConfig) -> list[ReplyPart]:
    """把能机械修的都修掉，并丢弃修完变空的气泡。"""
    fixed: list[ReplyPart] = []
    for part in parts:
        text = sanitize(part.text, style)
        if not text and "{photo}" not in part.text:
            continue
        fixed.append(ReplyPart(text=text, pause_before_seconds=part.pause_before_seconds))
    if len(fixed) > style.max_parts + 1:
        # 超出的合并进最后一条，而不是直接丢掉内容
        head = fixed[: style.max_parts]
        tail = " ".join(p.text for p in fixed[style.max_parts :]).strip()
        if tail:
            head[-1] = ReplyPart(
                text=f"{head[-1].text} {tail}".strip(),
                pause_before_seconds=head[-1].pause_before_seconds,
            )
        fixed = head
    return fixed


def enforce(
    parts: list[ReplyPart], style: StyleConfig, boundaries: Boundaries
) -> tuple[list[ReplyPart], list[StyleViolation]]:
    """一次完整的把关。

    返回 ``(清理后的气泡, 需要模型重写的违规)``。第二项为空表示可以直接发。
    """
    issues = check(parts, style, boundaries)
    fixed = apply_fixes(parts, style)
    needs_rewrite = [v for v in issues if not v.fixable]
    return fixed, needs_rewrite


def filter_reaction(reaction: str | None, style: StyleConfig) -> str | None:
    """不用 emoji 的人物也不会去点表情反应。"""
    if not reaction or not style.allow_reactions:
        return None
    return reaction


def describe_for_rewrite(violations: list[StyleViolation]) -> str:
    """把违规清单写成一段给模型看的话，用于要求重写。"""
    if not violations:
        return ""
    lines = ["刚才那版不像你说的话，问题在这几处："]
    for v in violations:
        lines.append(f"- {v.detail}")
    lines.append("重写一遍。别解释，直接给新的。")
    return "\n".join(lines)
