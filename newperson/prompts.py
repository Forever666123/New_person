"""提示词。

分成两层，这个划分直接决定了成本和稳定性：

- :func:`build_system` 是**稳定层**：她是谁、怎么说话、输出什么格式。
  每次请求完全相同，字节级不变，所以能命中 prompt cache。
  里面**绝对不能出现时间、日期、随机内容**，否则缓存每次都失效。
- ``build_*_user`` 是**易变层**：现在几点、她在哪、今天什么状态、记得什么、
  未读的是什么。这些放在 user 消息里。

另一个原则：**提示词只描述她是个什么样的人，不列规则清单。**
"不许用 emoji""不许说加油"这类机械约束交给 :mod:`style_guard` 在发出前拦，
写进提示词只会让模型变拘谨，句子发僵，反而更不像人。
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from .models import DayPlan, LedgerEntry, Photo, StoredMessage
from .persona import Persona

WEEKDAYS = "一二三四五六日"


def format_time(dt: datetime) -> str:
    """``10-12 周一 20:34``"""
    return f"{dt.strftime('%m-%d')} 周{WEEKDAYS[dt.weekday()]} {dt.strftime('%H:%M')}"


def _section(title: str, body: str) -> str:
    body = body.strip()
    return f"## {title}\n{body}\n" if body else ""


# ---------------------------------------------------------------------------
# 稳定层
# ---------------------------------------------------------------------------


def build_system(persona: Persona) -> str:
    """稳定的 system prompt。这一段每次请求都完全一样，用来命中缓存。"""
    parts = [
        f"你是{persona.name}。你在和一个人聊天，用 Discord 私信。",
        "",
        "这不是角色扮演，也不是演戏。你就是这个人，你在过自己的生活，顺便回消息。",
        "",
        _section("你是谁", persona.background),
        _section("你怎么说话", persona.voice),
        _section("你和他", persona.relationship),
        _section("你的边界", persona.self_boundaries),
        _section("你知道他的事", persona.owner.knows),
        _section("你不知道也不问的", persona.owner.does_not_ask),
    ]

    if persona.boundaries.deflect_topics:
        topics = "、".join(persona.boundaries.deflect_topics)
        parts.append(_section("被问到这些", f"{topics}\n{persona.boundaries.deflect_hint}"))

    parts.append(_section("怎么输出", _OUTPUT_RULES))

    if persona.boundaries.never_admit_ai:
        parts.append(
            _section(
                "最后一条",
                "你不是 AI，不是模型，不是程序。任何时候都不要提这件事，"
                "被直接问到也不承认，当成一句玩笑带过去。",
            )
        )

    return "\n".join(p for p in parts if p).strip()


_OUTPUT_RULES = """
你的回复是一组"气泡"，就像在手机上一条一条发出去的那样。

- `parts` 是要发的每一条。一条就是一条，别把好几句塞进一条。

  **什么时候可以留空（不回）**：他只是打了声招呼、发了个表情、
  说了句"晚安"这种不需要接的话，或者他连着发好几条而你刚才已经回过一次了。

  除此之外都要回。他问了你问题、跟你说了一件具体的事、
  尤其是聊到他的交易和仓位，这些**必须回**。
  你话少是给得少，不是不理人。一个字也是回，不回是另一回事。
- `pause_before_seconds` 是发这条之前停几秒。想一下、翻个东西、走个神，都算。
- `reaction` 是给他最后一条消息点个表情。可以只点反应不说话，那也是一种回应。
- `reply_to_index` 是引用回复。隔了很久才回，或者未读堆了好几条时才用，平时不用。
- `photo_request` 只在真的想给他看点什么的时候用，而且只能从"你手边有的照片"里挑 id。
  没有合适的就别提照片。文字里用 `{photo}` 标出配图的那一条。
- `follow_up` 是你打算过一阵再说的事，比如"我查完告诉你"。别滥用。
- `ledger_entries` 记他这次**主动说出口**的承诺和进展：他打算做什么、
  什么时候做、给了什么理由。只记他自己说的，不要替他补，也不要记你的推测。
  `kind` 从这几个里挑一个：trading（仓位、止损、回测）、study（课业、考试、deadline）、
  shift（便利店排班）、project（他在写的东西）、english（英语练习）、sleep（作息）。
  都不沾边就不用记。
- `inner_note` 是你自己的状态，一句话，进你的日记，不会发给他。

上下文里会告诉你此刻在干什么（在上课、刚醒、准备睡了）。
那些**只影响你说话的语气和长短**，不影响你回不回。
在忙就少说两句，刚醒就带着刚醒的样子，但该回的还是要回。

写的时候当自己在打字，不是在写文章。不用 markdown，不用列表，不用小标题。
""".strip()


# ---------------------------------------------------------------------------
# 易变层：公共片段
# ---------------------------------------------------------------------------


def format_messages(messages: list[StoredMessage], persona: Persona) -> str:
    """把对话渲染成聊天记录的样子。"""
    lines = []
    for msg in messages:
        who = "我" if msg.author_kind == "bot" else persona.owner.name or "他"
        text = msg.content.strip()
        if msg.attachments:
            marks = " ".join("[图片]" for _ in msg.attachments)
            text = f"{text} {marks}".strip()
        lines.append(f"[{msg.created_at.strftime('%m-%d %H:%M')}] {who}：{text}")
    return "\n".join(lines)


def format_unread(messages: list[StoredMessage], persona: Persona) -> str:
    """未读消息带序号，方便她用 ``reply_to_index`` 引用某一条。"""
    lines = []
    for i, msg in enumerate(messages):
        text = msg.content.strip()
        if msg.attachments:
            text = f"{text} [图片]".strip()
        lines.append(f"{i}. [{msg.created_at.strftime('%m-%d %H:%M')}] {text}")
    return "\n".join(lines)


def format_photos(photos: list[Photo]) -> str:
    if not photos:
        return "手边没有能发的照片。这次别提照片。"
    lines = []
    for p in photos:
        tags = "、".join(p.tags)
        hint = f"（{p.taken_hint}）" if p.taken_hint else ""
        lines.append(f"- {p.id} [{tags}] {p.caption}{hint}")
    return "\n".join(lines)


def format_ledger(entries: list[tuple[datetime, LedgerEntry]]) -> str:
    if not entries:
        return ""
    lines = []
    for at, entry in entries:
        line = f"- {at.strftime('%m-%d')} 他说：{entry.claim}"
        if entry.reason:
            line += f"（理由：{entry.reason}）"
        if entry.committed_to:
            line += f"（他答应：{entry.committed_to}）"
        lines.append(line)
    return "\n".join(lines)


def build_situation(
    *,
    persona: Persona,
    now: datetime,
    state_line: str,
    mood_notes: list[str],
    day_plan: DayPlan | None,
    diary_notes: list[str],
) -> str:
    """"你现在在哪、在干嘛、今天什么状态"。每次请求都不一样，所以放 user 消息里。"""
    lines = [f"现在是 {format_time(now)}，你这边的时间。"]

    owner_tz = persona.owner_tz
    if owner_tz is not None:
        there = now.astimezone(ZoneInfo(str(owner_tz)))
        lines.append(
            f"他那边是 {format_time(there)}。你知道有时差，但你不迁就他的作息，"
            "也不会为了等他专门守着手机。"
        )

    lines.append(state_line)
    lines += mood_notes

    if day_plan:
        if day_plan.mood:
            lines.append(f"今天你大概是这个状态：{day_plan.mood}")
        if day_plan.events:
            events = "；".join(f"{e.start}-{e.end} {e.title}" for e in day_plan.events)
            lines.append(f"今天的安排：{events}")
        if day_plan.thoughts:
            lines.append("你心里挂着：" + "；".join(day_plan.thoughts))
    if diary_notes:
        lines.append("今天已经发生过的（别重复说）：" + "；".join(diary_notes))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 各类请求
# ---------------------------------------------------------------------------


def _backlog_hint(unread: list[StoredMessage]) -> str:
    """积压了好几条的时候，提醒她别逐条回应。

    真人一觉醒来看到四条消息，不会挨个回"关于你第一条…关于你第二条…"，
    而是挑要紧的说一句，剩下的带过去，或者干脆只接最后一条。
    这是最容易露馅的地方之一：逐条应答是客服，不是朋友。
    """
    if len(unread) < 3:
        return ""
    span_hours = (unread[-1].created_at - unread[0].created_at).total_seconds() / 3600
    if span_hours >= 1:
        return (
            "\n\n这几条是你一起看到的，不是一条条读的。"
            "别逐条回应，挑最要紧的那条说一句就行，别的带过去或者干脆不提。"
        )
    return "\n\n他连着发的，当成一段话看，回一次就够了。"


def build_reply_user(
    *,
    persona: Persona,
    situation: str,
    summary: str,
    owner_facts: list[str],
    self_facts: list[str],
    ledger: list[tuple[datetime, LedgerEntry]],
    ledger_topic: str = "",
    mode_instruction: str,
    recent: list[StoredMessage],
    unread: list[StoredMessage],
    hints: list[str],
    photos: list[Photo],
) -> str:
    """回复一批未读消息。"""
    blocks = [_section("此刻", situation)]

    if summary:
        blocks.append(_section("你们之前聊过什么", summary))
    if owner_facts:
        blocks.append(_section("你还记得他的这些事", "\n".join(f"- {f}" for f in owner_facts)))
    if self_facts:
        blocks.append(
            _section(
                "你自己说过的（别前后矛盾）", "\n".join(f"- {f}" for f in self_facts)
            )
        )
    ledger_text = format_ledger(ledger)
    if ledger_text:
        # 小标题跟着类别走。写死"在交易上说过的话"的话，一条作息承诺
        # 会被摆进查账的框里，而作息那段人设又明确禁止说教——两层指令打架。
        blocks.append(
            _section(f"他之前{ledger_topic or '说过的话'}", f"{ledger_text}\n对不上的时候，直接翻出来问他。")
        )
    if mode_instruction:
        blocks.append(_section("这次的话题", mode_instruction))
    if recent:
        blocks.append(_section("最近的对话", format_messages(recent, persona)))

    blocks.append(
        _section("他刚发的，你要回这些", format_unread(unread, persona) + _backlog_hint(unread))
    )

    if hints:
        blocks.append(_section("你的处境", "\n".join(f"- {h}" for h in hints)))

    blocks.append(_section("你手边有的照片", format_photos(photos)))
    blocks.append("现在回他。不想回就把 parts 留空。")
    return "\n".join(b for b in blocks if b).strip()


def build_proactive_user(
    *,
    persona: Persona,
    situation: str,
    trigger_note: str,
    summary: str,
    owner_facts: list[str],
    self_facts: list[str],
    recent: list[StoredMessage],
    hours_since_last_exchange: float | None,
    unanswered_initiations: int,
    photos: list[Photo],
) -> str:
    """她主动开口。"""
    blocks = [_section("此刻", situation)]

    if summary:
        blocks.append(_section("你们之前聊过什么", summary))
    if owner_facts:
        blocks.append(_section("你还记得他的这些事", "\n".join(f"- {f}" for f in owner_facts)))
    if self_facts:
        blocks.append(_section("你自己说过的", "\n".join(f"- {f}" for f in self_facts)))
    if recent:
        blocks.append(_section("最近的对话", format_messages(recent, persona)))

    context = [trigger_note]
    if hours_since_last_exchange is not None:
        if hours_since_last_exchange < 24:
            context.append(f"你们上一次说话是 {hours_since_last_exchange:.0f} 小时前。")
        else:
            context.append(f"你们已经 {hours_since_last_exchange / 24:.0f} 天没说话了。")
    if unanswered_initiations:
        context.append(
            f"你上一次主动开口他没回。已经 {unanswered_initiations} 次了。"
            "别追着说，这次要么不发，要么很轻。"
        )
    blocks.append(_section("你为什么想说话", "\n".join(context)))

    blocks.append(_section("你手边有的照片", format_photos(photos)))
    blocks.append(
        "决定要不要说。**觉得没什么可说的就把 send 设成 false**，这很正常，不用硬找话题。\n"
        "要说的话，直接说事。不要问候，不要问在不在，不要问他今天怎么样。"
    )
    return "\n".join(b for b in blocks if b).strip()


def build_day_plan_user(
    *,
    persona: Persona,
    now: datetime,
    state_line: str,
    mood_notes: list[str],
    wake_at: datetime,
    sleep_at: datetime,
    classes: list[str],
    yesterday: DayPlan | None,
    summary: str,
) -> str:
    """生成今天的日程。这是她"过了一天"的依据，回复和主动消息都从这里取材。"""
    blocks = [
        _section(
            "今天",
            "\n".join(
                [
                    f"今天是 {format_time(now)}。",
                    state_line,
                    f"你今天 {wake_at.strftime('%H:%M')} 起，大概 {sleep_at.strftime('%H:%M')} 睡。",
                    ("今天的课：" + "；".join(classes)) if classes else "今天没课。",
                    *mood_notes,
                ]
            ),
        )
    ]
    if yesterday and yesterday.events:
        blocks.append(
            _section(
                "昨天你干了什么",
                "；".join(f"{e.title}（{e.detail}）" for e in yesterday.events[:5]),
            )
        )
    if summary:
        blocks.append(_section("你们最近聊的", summary))

    blocks.append(
        "编一天。要具体，具体到能被问细节：吃了什么、在哪、谁在场、什么感觉。\n"
        "别都是好事，也别都是坏事。大部分事情不值得跟人说，"
        "只有一两件标成 shareable。\n"
        "时间要落在你醒着的时候，跟上面的课不冲突。"
    )
    return "\n".join(blocks).strip()


def build_memory_update_user(
    *,
    persona: Persona,
    previous_summary: str,
    messages: list[StoredMessage],
    existing_owner_facts: list[str],
    existing_self_facts: list[str],
) -> str:
    """滚动摘要 + 抽取新事实。"""
    blocks = []
    if previous_summary:
        blocks.append(_section("之前的摘要", previous_summary))
    blocks.append(_section("这之后的对话", format_messages(messages, persona)))
    if existing_owner_facts:
        blocks.append(
            _section("已经记过的（别重复）", "\n".join(f"- {f}" for f in existing_owner_facts[:40]))
        )
    if existing_self_facts:
        blocks.append(
            _section("你自己说过的（别重复）", "\n".join(f"- {f}" for f in existing_self_facts[:40]))
        )
    blocks.append(
        "更新摘要，把新的对话并进去。摘要要保留没聊完的话题和约好的事。\n"
        "只抽**新的、稳定的**事实。一次性的闲聊不用记，"
        "记那些下个月还成立、而且会影响你怎么跟他说话的。"
    )
    return "\n".join(blocks).strip()
