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


def test_removes_emoji_and_exclamation(persona: Persona) -> None:
    assert sg.sanitize("好啊！😄", persona.style) == "好啊"


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


def test_full_english_sentence_is_not(persona: Persona) -> None:
    _, needs = sg.enforce(parts("i think you should stop"), persona.style, persona.boundaries)
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


def test_reactions_are_off_for_her(persona: Persona) -> None:
    """她不用 emoji，表情反应也一并关掉。"""
    assert sg.filter_reaction("😂", persona.style) is None
