"""说话风格的最后一道关。

为什么需要它：如果把"少用 emoji、别打句号、别说加油"这类规则一条条塞进提示词，
模型会写得非常拘谨，句子变僵，反而更不像人。所以分工是：

- **提示词**负责描述她是个什么样的人，让模型自由发挥。
- **这里**负责在消息发出去之前，把明显不像她的地方拦下来。

拦下来分两种处理：

- 能机械修的（句尾句号、emoji 刷屏、感叹号刷屏）直接修，不惊动模型。
- 不能机械修的（说了 ``never_say`` 里的话、整段英文、长句超预算）返回违规清单，
  由 ``brain`` 带着清单让模型重写一次。重写还不过就退回机械修剪，不会卡住。

**用预算，不用开关。** 偶尔一个 emoji、偶尔一个感叹号是年轻人的正常说话方式，
满屏才不正常。频率交给提示词，这里只管上限。把预算设成 0 就等于完全禁止。
"""

from __future__ import annotations

import math
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
    "]"
)
_CJK = re.compile(r"[一-鿿]")
_LATIN_WORD = re.compile(r"[A-Za-z]{2,}")
_SENTENCE_SPLIT = re.compile(r"[。！？!?\n]+")
_TRAILING_PUNCT = re.compile(r"[。\.\s]+$")


def count_emoji(text: str) -> int:
    return len(_EMOJI.findall(text))


def count_exclamations(text: str) -> int:
    return text.count("！") + text.count("!")


def budget_for(part_count: int, ratio: float) -> int:
    """一次回复里，最多几条气泡可以带这种东西。

    ``ratio`` 为 0 表示一条都不行。否则至少允许一条，
    因为她一次本来就只发一两条，按比例算会直接归零。
    """
    if ratio <= 0:
        return 0
    return max(1, round(part_count * ratio))


def normalize_punctuation(text: str, style: StyleConfig) -> str:
    """按她的习惯清理句号：句中的变成空格（换口气继续说），句尾的直接去掉。

    英文句点只动句尾，免得把小数和缩写改坏。问号一律保留，她问问题是表达在意的方式。
    """
    if not style.strip_trailing_period:
        return text.strip()
    out = text.replace("。", " ")
    out = _TRAILING_PUNCT.sub("", out)
    return re.sub(r"[ \t]{2,}", " ", out).strip()


def trim_emoji(text: str, keep: int) -> str:
    """只保留前 ``keep`` 个 emoji，多的删掉。"""
    if keep <= 0:
        return _EMOJI.sub("", text)
    seen = 0

    def repl(match: re.Match[str]) -> str:
        nonlocal seen
        seen += 1
        return match.group(0) if seen <= keep else ""

    return _EMOJI.sub(repl, text)


def trim_exclamations(text: str, keep: int) -> str:
    out = []
    seen = 0
    for ch in text:
        if ch in "！!":
            seen += 1
            if seen > keep:
                out.append(" ")
                continue
        out.append(ch)
    return re.sub(r"[ \t]{2,}", " ", "".join(out)).strip()


def sanitize(text: str, style: StyleConfig) -> str:
    """单条气泡的机械清理。不改语义，只管标点和 emoji 上限。"""
    out = trim_emoji(text, style.max_emoji_per_part if style.emoji_budget > 0 else 0)
    if style.exclamation_budget <= 0:
        out = trim_exclamations(out, 0)
    return normalize_punctuation(out, style)


def sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT.split(text) if s.strip()]


def is_too_english(text: str, style: StyleConfig) -> bool:
    """整段英文才算违规。夹几个英文词是她的正常说话方式。"""
    limit = style.english_sentence_max_words
    if limit <= 0 or _CJK.search(text):
        return False
    return len(_LATIN_WORD.findall(text)) > limit


def _opens_with_greeting(opening: str, phrase: str) -> bool:
    """``opening`` 是不是**以这句寒暄开头并且到此为止**。

    光看前缀不够："你好像把参数记错了"和"好久没聊这个话题了"都以寒暄开头，
    但它们只是句子的前半截，不是打招呼。真正的寒暄后面接的是句末——
    要么没了，要么是标点，要么是"呀""了""吗"这类语气词。
    """
    # 大小写不敏感：清单里写的是 hi / hello，而模型写英文默认首字母大写，
    # 真正会被写出来的是 `Hi`。比较敏感的话这两条形同虚设，
    # 而"她开口第一句是 Hi"是最像聊天机器人的一种开场。
    if not opening.lower().startswith(phrase.lower()):
        return False
    rest = opening[len(phrase) :]
    return not rest or rest[0] in GREETING_TAIL


GREETING_TAIL = " \t\u3000，。、！？~…～!?,.呀啊阿吗么了呢哦噢喔嘛哈诶欸的"
"""寒暄后面允许跟的东西。再往后就是别的句子了，不是打招呼。

全角半角都要有。"Hi～"里那个是全角波浪号，只收 ASCII 的 `~` 就漏了；
英文寒暄后面跟的又多半是半角的 `!` `?` `.`。
"""

OPENING_NOISE = " \t\u3000，。、！？~…—-·:：;；\"'“”‘’（）()"
"""判断"是不是用寒暄开头"之前先掐掉的东西。"""


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

    emoji_budget = budget_for(len(parts), style.emoji_budget)
    exclaim_budget = budget_for(len(parts), style.exclamation_budget)
    emoji_parts = sum(1 for p in parts if count_emoji(p.text))
    exclaim_parts = sum(1 for p in parts if count_exclamations(p.text))

    if emoji_parts > emoji_budget:
        issues.append(
            StyleViolation(
                kind="emoji",
                detail=f"{emoji_parts} 条带 emoji，她一次最多 {emoji_budget} 条",
                fixable=True,
            )
        )
    if exclaim_parts > exclaim_budget:
        issues.append(
            StyleViolation(
                kind="exclamation",
                detail=f"{exclaim_parts} 条带感叹号，她很少用",
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

        # 寒暄只有在开口那一下才是寒暄。掐掉前面的标点空白再比，
        # 这样"在吗"作为整条消息会被拦下，"我现在吗？在图书馆"不会。
        opening = text.lstrip(OPENING_NOISE)
        for phrase in boundaries.never_open_with:
            if _opens_with_greeting(opening, phrase):
                issues.append(
                    StyleViolation(
                        kind="banned_phrase",
                        detail=f"用寒暄开头了：{phrase}",
                        part_index=i,
                        fixable=False,
                    )
                )

        if count_emoji(text) > style.max_emoji_per_part:
            issues.append(
                StyleViolation(
                    kind="emoji",
                    detail=f"一条里塞了 {count_emoji(text)} 个 emoji",
                    part_index=i,
                    fixable=True,
                )
            )

        if is_too_english(text, style):
            issues.append(
                StyleViolation(
                    kind="full_english",
                    detail="整段英文，她只是夹几个词",
                    part_index=i,
                    fixable=False,
                )
            )

        for sentence in sentences(text):
            all_sentences += 1
            if len(sentence) > style.long_sentence_chars:
                long_sentences.append((i, sentence))

    # 长句本身不违规，超出预算才违规。她偶尔说重话，那是有意义的。
    budget = max(1, math.ceil(all_sentences * style.long_sentence_budget))
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
    """把能机械修的都修掉，并丢弃修完变空的气泡。

    超预算的 emoji 和感叹号从**后面**的气泡开始削，第一条留着，
    因为一段话里最像人的那个表情通常在开头。
    """
    emoji_budget = budget_for(len(parts), style.emoji_budget)
    exclaim_budget = budget_for(len(parts), style.exclamation_budget)
    emoji_used = exclaim_used = 0

    fixed: list[ReplyPart] = []
    for part in parts:
        text = part.text

        if count_emoji(text):
            emoji_used += 1
            keep = style.max_emoji_per_part if emoji_used <= emoji_budget else 0
            text = trim_emoji(text, keep)
        if count_exclamations(text):
            exclaim_used += 1
            if exclaim_used > exclaim_budget:
                text = trim_exclamations(text, 0)

        text = normalize_punctuation(text, style)
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
    lines += [f"- {v.detail}" for v in violations]
    lines.append("重写一遍。别解释，直接给新的。")
    return "\n".join(lines)
