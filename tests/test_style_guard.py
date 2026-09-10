"""说话风格拦截的测试。"""

from __future__ import annotations

from newperson import style_guard as sg
from newperson.models import ReplyPart
from newperson.persona import Persona


def parts(*texts: str) -> list[ReplyPart]:
    return [ReplyPart(text=t) for t in texts]


def test_strips_trailing_period(persona: Persona) -> None:
    assert sg.sanitize("知道了。", persona.style) == "知道了"


def test_mid_sentence_period_becomes_a_breath(persona: Persona) -> None:
    assert sg.sanitize("知道了。你先睡。", persona.style) == "知道了 你先睡"


def test_keeps_one_emoji(persona: Persona) -> None:
    """年轻人不可能一个 emoji 都不用。偶尔一个是正常的。"""
    fixed, needs = sg.enforce(parts("好啊 😄"), persona.style, persona.boundaries)
    assert not needs
    assert "😄" in fixed[0].text


def test_trims_emoji_spam_in_one_bubble(persona: Persona) -> None:
    """一条里塞五个就不像她了，削到一个。"""
    fixed, _ = sg.enforce(parts("哈哈😂😂😂🤣😅"), persona.style, persona.boundaries)
    assert sg.count_emoji(fixed[0].text) == 1


def test_only_one_bubble_carries_emoji(persona: Persona) -> None:
    """一次回复里最多一条带 emoji，后面的削掉。"""
    fixed, _ = sg.enforce(parts("行😄", "那我先去洗澡😴"), persona.style, persona.boundaries)
    assert sum(sg.count_emoji(p.text) for p in fixed) == 1


def test_emoji_can_be_switched_off_entirely(persona: Persona) -> None:
    """把预算设成 0 就是完全不用，给别的人设留的口子。"""
    style = persona.style.model_copy(update={"emoji_budget": 0.0})
    fixed, _ = sg.enforce(parts("好啊 😄"), style, persona.boundaries)
    assert sg.count_emoji(fixed[0].text) == 0


def test_keeps_decimals_intact(persona: Persona) -> None:
    """句尾句号要去，小数点不能动。"""
    assert "3.5" in sg.sanitize("回撤 3.5 个点", persona.style)


def test_keeps_question_marks(persona: Persona) -> None:
    """她问问题，问号要留着。"""
    assert sg.sanitize("你记了吗？", persona.style) == "你记了吗？"


def test_banned_phrase_needs_a_rewrite(persona: Persona) -> None:
    _, needs = sg.enforce(parts("加油"), persona.style, persona.boundaries)
    assert [v.kind for v in needs] == ["banned_phrase"]


def test_soft_comfort_phrases_are_caught(persona: Persona) -> None:
    for text in ("你可以的", "早点休息", "我相信你", "抱歉刚在忙", "你今天怎么样"):
        _, needs = sg.enforce(parts(text), persona.style, persona.boundaries)
        assert needs, f"{text} 应该被拦下来"


def test_word_level_english_is_fine(persona: Persona) -> None:
    _, needs = sg.enforce(parts("deadline 周五 whatever"), persona.style, persona.boundaries)
    assert not needs


def test_clean_reply_keeps_its_question_mark(persona: Persona) -> None:
    fixed, _ = sg.enforce(parts("记了吗？"), persona.style, persona.boundaries)
    assert fixed[0].text == "记了吗？"


def test_a_short_english_line_is_fine(persona: Persona) -> None:
    """她在美国待了好几年，冒一句短的很正常。"""
    _, needs = sg.enforce(parts("yeah my bad"), persona.style, persona.boundaries)
    assert not needs


def test_a_whole_english_paragraph_is_not(persona: Persona) -> None:
    _, needs = sg.enforce(
        parts("i really think you should stop trading this week and take a break"),
        persona.style,
        persona.boundaries,
    )
    assert [v.kind for v in needs] == ["full_english"]


def test_one_long_sentence_is_allowed(persona: Persona) -> None:
    """偶尔一句重话是人设的一部分，不能一刀切。"""
    long = "你上次也是这么说的然后又没记下来这次别再来问我了"
    _, needs = sg.enforce(parts(long), persona.style, persona.boundaries)
    assert not needs


def test_many_long_sentences_are_not(persona: Persona) -> None:
    long = "你上次也是这么说的然后又没记下来这次别再来问我了"
    _, needs = sg.enforce(parts(long, long, long), persona.style, persona.boundaries)
    assert any(v.kind == "too_long" for v in needs)


def test_extra_bubbles_are_merged_not_dropped(persona: Persona) -> None:
    fixed, _ = sg.enforce(parts("一", "二", "三", "四", "五"), persona.style, persona.boundaries)
    assert len(fixed) <= persona.style.max_parts
    assert "五" in "".join(p.text for p in fixed)


def test_photo_placeholder_survives_an_empty_text(persona: Persona) -> None:
    fixed, _ = sg.enforce(parts("{photo}"), persona.style, persona.boundaries)
    assert len(fixed) == 1


def test_clean_reply_passes_through(persona: Persona) -> None:
    fixed, needs = sg.enforce(parts("嗯", "什么时候的事"), persona.style, persona.boundaries)
    assert not needs
    assert [p.text for p in fixed] == ["嗯", "什么时候的事"]


def test_reaction_is_allowed_but_optional(persona: Persona) -> None:
    """她极少点表情反应，但这条路是通的。频率由提示词控制，不在这里一刀切。"""
    assert sg.filter_reaction("😂", persona.style) == "😂"
    assert sg.filter_reaction(None, persona.style) is None


def test_reaction_can_be_turned_off(persona: Persona) -> None:
    """完全不用 emoji 的人物，把 allow_reactions 关掉就一个都不会点。"""
    style = persona.style.model_copy(update={"allow_reactions": False})
    assert sg.filter_reaction("😂", style) is None


def test_she_never_explains_where_she_was(persona: Persona) -> None:
    """人设里写死了她不解释行踪。这几种说法都要拦下来。"""
    for text in ("还在睡 没看到", "刚看到", "刚醒", "刚忙完", "才看到", "睡过头了"):
        _, needs = sg.enforce(parts(text), persona.style, persona.boundaries)
        assert needs, f"{text} 应该被拦下来"


def test_talking_about_his_stuff_is_fine(persona: Persona) -> None:
    """别把正常内容误伤了。"""
    for text in ("soxl那个新闻出来跌了还是涨了", "止损设了没", "成本多少"):
        _, needs = sg.enforce(parts(text), persona.style, persona.boundaries)
        assert not needs, f"{text} 不该被拦"
