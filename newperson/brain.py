"""大脑：所有 Claude 调用都在这里。

三条规矩：

1. **失败不能被对方看见。** 任何异常都返回 ``None``，由调度器当作"这会儿没看手机"
   过一阵重试。绝不发"出错了""我遇到了一点问题"这种话出去。
2. **稳定层缓存。** system 是固定的一段，加 ``cache_control``，
   易变内容全部放 user 消息。缓存命中率可以从 ``usage`` 表里看出来。
3. **有花钱的上限。** 每天调用数超过 ``max_calls_per_day`` 就一律当失败处理，
   任务顺延，人物表现为话变少，而不是程序崩掉。
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, TypeVar

import anthropic
import pydantic
from pydantic import BaseModel

from . import style_guard
from .clock import Clock
from .config import Settings
from .memory import Memory
from .models import (
    DayPlan,
    LedgerEntry,
    MemoryUpdate,
    Photo,
    ProactivePlan,
    ReplyPart,
    ReplyPlan,
    StoredMessage,
)
from .persona import Persona
from .prompts import (
    build_day_plan_user,
    build_memory_update_user,
    build_proactive_user,
    build_reply_user,
    build_system,
)

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

PRICING_PER_MTOK = {
    "claude-opus-5": (5.0, 25.0),
    "claude-fable-5-1": (10.0, 50.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
"""(输入, 输出) 美元每百万 token。缓存命中按输入的十分之一算，写入按 1.25 倍。"""

def _keep_one_typo(plan: ReplyPlan, *, allowed: bool) -> None:
    """打错字多久一次由代码定，不由模型定：没让她手滑，写了也不用；让了，也只留一条。

    交给模型自己拿捏频率的话，它要么从来不打错，要么每句都错。
    """
    kept = False
    for part in plan.parts:
        if not allowed or kept:
            part.typo_text = ""
        elif part.typo_text:
            kept = True


def price_of(model: str) -> tuple[float, float]:
    """(输入, 输出) 单价。带日期的正式 ID（claude-haiku-4-5-20251001）按别名算。

    表是按字符串精确查的，写成带日期的那种会落到默认价上，Haiku 被多估五倍，
    `!np status` 的花费和按钱算的判断都跟着偏。只认"别名 + 八位日期"这一种写法：
    别的后缀（比如 -5-5）可能是另一个型号，价钱不一定一样，宁可按默认的高价算。
    """
    if model in PRICING_PER_MTOK:
        return PRICING_PER_MTOK[model]
    base, _, tail = model.rpartition("-")
    if base in PRICING_PER_MTOK and len(tail) == 8 and tail.isdigit():
        return PRICING_PER_MTOK[base]
    return (5.0, 25.0)


def _money_trouble(exc: Exception) -> str:
    """认出"钱的事"：余额用完、撞到自己设的花费上限、密钥不认了。

    这几种原来在 `!np status` 里都只是"接口返回 400/401/429"，
    跟任何别的 400 分不开——而它们恰恰是要他本人去处理的那种。
    措辞留余地：余额明明够、也有人碰到过同一句报错。认不出来就返回空串。
    """
    body = getattr(exc, "body", None)
    message = ""
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        message = str(body["error"].get("message", ""))
    text = f"{message} {exc}".lower()
    code = getattr(exc, "status_code", 0)
    if "credit balance is too low" in text:
        return f"接口返回 {code}：多半是 API 余额用完了，去 Console 的 Billing 页看看"
    if "usage limit" in text or "spend limit" in text:
        return f"接口返回 {code}：撞到了 Console 里设的花费上限"
    if code == 401:
        return "接口返回 401：API key 不认了（过期、被删或写错）"
    return ""


EFFORT_SUPPORTED = {
    "claude-fable-5-1",
    "claude-fable-5",
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-opus-4-5",
    "claude-sonnet-5",
    "claude-sonnet-4-6",
}
"""接受 ``output_config.effort`` 的模型。

Haiku 4.5 和 Sonnet 4.5 不接受，传了会直接 400。
不认识的模型一律不传，宁可少一个参数也别让她连不上。
"""

MAX_IMAGE_BYTES = 5 * 1024 * 1024


@dataclass
class ReplyRequest:
    situation: str
    summary: str
    owner_facts: list[str]
    self_facts: list[str]
    ledger: list[tuple[int, datetime, LedgerEntry]]
    mode_instruction: str
    recent: list[StoredMessage]
    unread: list[StoredMessage]
    hints: list[str]
    photos: list[Photo]
    images: list[tuple[str, bytes]] = field(default_factory=list)
    """对方发来的图片 ``[(media_type, 原始字节)]``，让她真的看得到。"""
    must_reply: bool = False
    """他问了问题或者说了件具体的事。这种不能不回。"""
    ledger_topic: str = ""
    """台账那一段的小标题，跟着话题类别走。"""
    open_questions: list[tuple[int, datetime, LedgerEntry]] = field(default_factory=list)
    """她问过、还没听到下文的那几条。跟话题模式无关，永远带着。"""
    not_yet: list[tuple[int, datetime, LedgerEntry]] = field(default_factory=list)
    """他说了时间、还没到该问的时候的那几条。"""
    typo: bool = False
    """这次让她手滑打错一个字（发出去几秒后改回来）。"""


@dataclass
class ProactiveRequest:
    situation: str
    trigger_note: str
    summary: str
    owner_facts: list[str]
    self_facts: list[str]
    recent: list[StoredMessage]
    hours_since_last_exchange: float | None
    unanswered_initiations: int
    photos: list[Photo]
    he_spoke_since_noted: bool = False
    """这件事记下之后他又说过话。那样的话她要先看看他是不是已经答过了。"""
    not_yet: list[tuple[int, datetime, LedgerEntry]] = field(default_factory=list)
    """他说了时间、还没到该问的时候的那几条。"""


@dataclass
class DayPlanRequest:
    now: datetime
    state_line: str
    mood_notes: list[str]
    wake_at: datetime
    sleep_at: datetime
    classes: list[str]
    yesterday: DayPlan | None
    summary: str


@dataclass
class MemoryUpdateRequest:
    previous_summary: str
    messages: list[StoredMessage]
    existing_owner_facts: list[str]
    existing_self_facts: list[str]


_IMAGE_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def sniff_media_type(raw: bytes) -> str | None:
    """按**内容**认图片类型。认不出来返回 None。

    接口只收 jpeg / png / gif / webp，而且会把声明的类型和真实字节对一遍。
    所以这里只认这四种，别的（iPhone 的 heic、bmp、tiff、svg）一律当认不出来。
    """
    for magic, media_type in _IMAGE_MAGIC:
        if raw.startswith(magic):
            return media_type
    # webp 是 RIFF 容器：前四字节 RIFF，8-12 字节 WEBP，中间四字节是长度
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


class Brain:
    def __init__(
        self,
        client: Any,
        settings: Settings,
        persona: Persona,
        memory: Memory | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.client = client
        self.settings = settings
        self.persona = persona
        self.memory = memory
        self.clock = clock
        """只用来给接口报错打时间戳。项目里不裸调 datetime.now()，
        测试要把时钟拨到任意时刻才能验"这个错是几小时前的"。"""
        self._system = build_system(persona)

    # -- 稳定层 -------------------------------------------------------------

    def system_blocks(self) -> list[dict[str, Any]]:
        """带缓存标记的 system。这一段字节级固定，所以每次都能命中。"""
        return [
            {
                "type": "text",
                "text": self._system,
                "cache_control": {"type": "ephemeral"},
            }
        ]

    # -- 统一调用 -----------------------------------------------------------

    async def over_budget(self, today: date) -> bool:
        """今天的调用额度是不是用完了。

        调用方拿它区分"故障"和"今天做不了"：前者重试，后者顺延到明天。
        """
        return await self._over_budget(today)

    async def _over_budget(self, today: date) -> bool:
        if self.memory is None or self.settings.max_calls_per_day <= 0:
            return False
        used = await self.memory.usage_for(today)
        if used.get("calls", 0) < self.settings.max_calls_per_day:
            return False
        log.warning(
            "[brain] 今天已经调了 %s 次，到上限了，先歇着", used.get("calls"),
        )
        return True

    async def _record_usage(
        self, response: Any, today: date, purpose: str, model_used: str | None = None
    ) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        inp = getattr(usage, "input_tokens", 0) or 0
        cached = getattr(usage, "cache_read_input_tokens", 0) or 0
        written = getattr(usage, "cache_creation_input_tokens", 0) or 0
        out = getattr(usage, "output_tokens", 0) or 0

        in_price, out_price = price_of(model_used or self.settings.model)
        cost = (
            inp * in_price + cached * in_price * 0.1 + written * in_price * 1.25 + out * out_price
        ) / 1_000_000

        log.info(
            "[brain] %s in=%d cached=%d 写缓存=%d out=%d 约 $%.4f",
            purpose,
            inp,
            cached,
            written,
            out,
            cost,
        )
        if self.memory is not None:
            await self.memory.record_usage(
                today,
                input_tokens=inp,
                cache_read_tokens=cached,
                cache_creation_tokens=written,
                output_tokens=out,
                estimated_usd=cost,
            )

    async def _call(
        self,
        output_format: type[T],
        user_content: str | list[dict[str, Any]],
        *,
        purpose: str,
        today: date,
        model: str | None = None,
    ) -> T | None:
        """调一次模型。任何失败都返回 None，让上层当作"没看手机"。"""
        if await self._over_budget(today):
            return None

        chosen = model or self.settings.model
        kwargs: dict[str, Any] = {
            "model": chosen,
            "max_tokens": self.settings.max_tokens,
            "system": self.system_blocks(),
            "messages": [{"role": "user", "content": user_content}],
            "output_format": output_format,
        }
        # Haiku 4.5 之类不接受 effort，传了直接 400。
        if chosen in EFFORT_SUPPORTED:
            kwargs["output_config"] = {"effort": self.settings.effort}

        # **每一条失败路径都要留痕。** 她的正常状态就包含长时间不说话，
        # 所以"接口挂了"和"她这会儿不想聊"在外面看完全一样。
        # 原来只有 APIStatusError 和兜底那两条写了 last_api_error，
        # 而限流、连不上、被拒恰恰是最常见的三种——那三种发生时
        # `!np status` 干干净净，你只会觉得她今天特别安静。
        try:
            response = await self.client.messages.parse(**kwargs)
        except anthropic.RateLimitError as exc:
            log.warning("[brain] %s 撞到限流，稍后重试", purpose)
            await self._note_error(
                _money_trouble(exc) or f"限流（429）：{str(exc)[:80]}"
            )
            return None
        except anthropic.APIStatusError as exc:
            level = log.warning if exc.status_code >= 500 else log.error
            level("[brain] %s 接口返回 %s：%s", purpose, exc.status_code, exc)
            await self._note_error(
                _money_trouble(exc) or f"接口返回 {exc.status_code}"
            )
            return None
        except pydantic.ValidationError as exc:
            # 接口已经回了、也计了费，是 SDK 解析它的输出时不认。
            # 原来落到下面的兜底里，用量一条不记，日限额看不见这几次调用。
            log.warning("[brain] %s 的输出不合格式：%s", purpose, exc)
            await self._note_error("返回的内容不合格式")
            if self.memory is not None:
                await self.memory.record_usage(today)
            return None
        except anthropic.APIConnectionError as exc:
            log.warning("[brain] %s 连不上：%s", purpose, exc)
            await self._note_error(f"连不上接口：{str(exc)[:80]}")
            return None
        except Exception as exc:  # noqa: BLE001 - 兜底，人物不能因为一次调用崩掉
            log.exception("[brain] %s 出了意外：%s", purpose, exc)
            await self._note_error(str(exc)[:120])
            return None

        await self._record_usage(response, today, purpose, chosen)

        if getattr(response, "stop_reason", None) == "refusal":
            log.warning("[brain] %s 被拒了，这次就当没回", purpose)
            await self._note_error("模型拒绝回答")
            return None

        parsed = getattr(response, "parsed_output", None)
        if parsed is None:
            log.warning("[brain] %s 没解析出结构化结果", purpose)
            await self._note_error("返回的内容解析不出来")
            return None
        # 走到这里说明接口是通的，把旧的报错清掉——
        # 不清的话三周前的一次抖动会一直挂在 status 上，
        # 和"此刻密钥失效了"长得一模一样。
        await self._clear_error()
        return parsed

    async def _note_error(self, detail: str) -> None:
        """记下最近一次接口出错，**带时间**。

        不带时间的话，`!np status` 上那行报错没有断代信息：
        你分不出它是三周前的一次网络抖动，还是刚刚密钥失效了。
        """
        if self.memory is not None:
            now = self.clock.now() if self.clock else datetime.now(UTC)
            stamp = now.isoformat(timespec="seconds")
            await self.memory.kv_set("last_api_error", f"{stamp}\t{detail}")

    async def _clear_error(self) -> None:
        if self.memory is not None:
            await self.memory.kv_delete("last_api_error")

    # -- 各类请求 -----------------------------------------------------------

    @staticmethod
    def _with_images(text: str, images: list[tuple[str, bytes]]) -> str | list[dict[str, Any]]:
        """把对方发的图片拼进 user 消息，让她真的看得到内容。

        **图片类型按字节认，不信别人报的那个。** Discord 报的 content_type
        经常是错的（实测它把一张 PNG 报成 image/webp），而接口会对一遍字节，
        对不上就是 400。后果不是"这张图没看到"——是**她永久哑掉**：
        那条消息一直未读，每次排新的回复任务都重新带上同一张图、再 400 一次，
        后面所有的话都堵在它后面。认不出来的直接不发，她照样回，只是看不见图。
        """
        usable: list[tuple[str, bytes]] = []
        for _declared, raw in images:
            if not raw or len(raw) > MAX_IMAGE_BYTES:
                continue
            media_type = sniff_media_type(raw)
            if media_type is None:
                log.warning("[brain] 认不出这张图的格式（%d 字节），不发给模型", len(raw))
                continue
            usable.append((media_type, raw))
        if not usable:
            return text
        blocks: list[dict[str, Any]] = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": base64.standard_b64encode(raw).decode("ascii"),
                },
            }
            for media_type, raw in usable
        ]
        blocks.append({"type": "text", "text": text})
        return blocks

    async def generate_reply(self, req: ReplyRequest, today: date) -> ReplyPlan | None:
        """回一批未读消息。风格不过关会让它重写一次。"""
        prompt = build_reply_user(
            persona=self.persona,
            situation=req.situation,
            summary=req.summary,
            owner_facts=req.owner_facts,
            self_facts=req.self_facts,
            ledger=req.ledger,
            ledger_topic=req.ledger_topic,
            open_questions=req.open_questions,
            not_yet=req.not_yet,
            typo=req.typo,
            mode_instruction=req.mode_instruction,
            recent=req.recent,
            unread=req.unread,
            hints=req.hints,
            photos=req.photos,
        )
        plan = await self._call(
            ReplyPlan, self._with_images(prompt, req.images), purpose="reply", today=today
        )
        if plan is None:
            return None

        # 他问了问题、或者在说一件具体的事，模型却给了空。
        # 小模型在 effort 低的时候很容易走这条省事的路，但那不是"话少"，是不理人。
        if req.must_reply and not plan.parts and not plan.reaction:
            log.info("[brain] 他问了具体的事，这条不能不回，重来一次")
            nudge = (
                f"{prompt}\n\n"
                "你刚才给的是空的。他问了你问题，或者跟你说了一件具体的事，"
                "这种不能不回。你可以只回一句，但要接住他说的那件事。"
            )
            second = await self._call(
                ReplyPlan,
                self._with_images(nudge, req.images),
                purpose="reply-nudge",
                today=today,
            )
            if second is not None and (second.parts or second.reaction):
                plan = second

        polished = await self._polish(plan, prompt, req.images, today)
        if polished is not None:
            _keep_one_typo(polished, allowed=req.typo)
        return polished

    async def _polish(
        self,
        plan: ReplyPlan,
        prompt: str,
        images: list[tuple[str, bytes]],
        today: date,
    ) -> ReplyPlan | None:
        """过 style_guard。能机械修的直接修，修不了的让它重写一次。

        重写只给一次机会。再不行就用机械修剪的版本发出去，
        卡在这里反复调模型既费钱又会让她显得很久不回。

        **但带禁语的那条气泡不发。** 禁语修剪不了，"用修剪过的版本"
        原来就是原句照发——他抱怨过的"还在睡 没看到"就是这么漏出去的。

        去掉之后一条不剩时分两种：
        - 重写那次调用失败了（网络、限流）：返回 None，当作这会儿没看手机，过一阵再试；
        - 重写回来了、还是只有禁语：这一批就不接话了（空的 parts）。
          再重试只会让模型把同一个词写三遍、烧六次调用，然后任务判死、
          他那句一直挂在未读里，连主动消息也被压住。
        """
        fixed, needs_rewrite = style_guard.enforce(
            plan.parts, self.persona.style, self.persona.boundaries
        )
        plan.reaction = style_guard.filter_reaction(plan.reaction, self.persona.style)

        if not needs_rewrite:
            plan.parts = fixed
            return plan

        log.info("[brain] 风格不过关，重写一次：%s", [v.kind for v in needs_rewrite])
        retry_prompt = f"{prompt}\n\n{style_guard.describe_for_rewrite(needs_rewrite)}"
        second = await self._call(
            ReplyPlan, self._with_images(retry_prompt, images), purpose="reply-rewrite", today=today
        )
        if second is not None:
            refixed, still_bad = style_guard.enforce(
                second.parts, self.persona.style, self.persona.boundaries
            )
            if not still_bad:
                second.parts = refixed
                second.reaction = style_guard.filter_reaction(second.reaction, self.persona.style)
                return second
            second.parts = self._drop_banned(refixed)
            second.reaction = style_guard.filter_reaction(second.reaction, self.persona.style)
            log.info("[brain] 重写还是不过，用修剪过的版本（带禁语的那条不发）")
            if refixed and not second.parts:
                log.info("[brain] 剩下的全是禁语，这一批不接话了")
            return second

        plan.parts = self._drop_banned(fixed)
        if fixed and not plan.parts:
            log.info("[brain] 重写没出来，剩下的全是禁语，这次先不回")
            return None
        return plan

    def _drop_banned(self, parts: list[ReplyPart]) -> list[ReplyPart]:
        """去掉还带着禁语的气泡。别的毛病机械修剪已经处理过了。"""
        bad = {
            v.part_index
            for v in style_guard.check(parts, self.persona.style, self.persona.boundaries)
            if v.kind == "banned_phrase"
        }
        return [part for i, part in enumerate(parts) if i not in bad]

    async def generate_proactive(self, req: ProactiveRequest, today: date) -> ProactivePlan | None:
        prompt = build_proactive_user(
            persona=self.persona,
            situation=req.situation,
            trigger_note=req.trigger_note,
            summary=req.summary,
            owner_facts=req.owner_facts,
            self_facts=req.self_facts,
            recent=req.recent,
            hours_since_last_exchange=req.hours_since_last_exchange,
            unanswered_initiations=req.unanswered_initiations,
            photos=req.photos,
            he_spoke_since_noted=req.he_spoke_since_noted,
            not_yet=req.not_yet,
        )
        plan = await self._call(ProactivePlan, prompt, purpose="proactive", today=today)
        if plan is None or not plan.send:
            return plan
        # 手滑只在回复里掷骰子。schema 里一样有 typo_text，模型自己填了也不用：
        # 主动开口那一句要是每次都打错再改，那就不是手滑了
        _keep_one_typo(plan, allowed=False)

        # 原来这里只做机械修剪，把"要重写"的那部分直接扔了——
        # 于是回复有两道关（修剪 + 重写一次），主动消息只有一道。
        # 恰恰主动消息才是她开口说的第一句：没有你刚说的话垫着，
        # 上下文最少，最容易滑到寒暄上去。第一次上线那句尤其如此。
        fixed, needs_rewrite = style_guard.enforce(
            plan.parts, self.persona.style, self.persona.boundaries
        )
        if not needs_rewrite:
            plan.parts = fixed
            return plan

        log.info("[brain] 主动消息风格不过关，重写一次：%s", [v.kind for v in needs_rewrite])
        retry_prompt = f"{prompt}\n\n{style_guard.describe_for_rewrite(needs_rewrite)}"
        again = await self._call(ProactivePlan, retry_prompt, purpose="proactive", today=today)
        if again is None or not again.send:
            # 重写没出来的时候要看是什么问题。
            # 表情太多、句子太长这类机械修剪已经处理掉了，用 fixed 发出去没问题。
            # **但禁语不是机械可修的**：style_guard 把 banned_phrase 标成 fixable=False，
            # apply_fixes 一个字都不动，所以 fixed 里那句"在吗 好久没聊了"原封不动。
            # 与其把一句聊天机器人味的寒暄发出去，不如这次不说话——
            # 她本来就不是每次想说都会说。
            if any(v.kind == "banned_phrase" for v in needs_rewrite):
                log.info("[brain] 重写没出来，而问题是禁语，这次就不说了")
                plan.send = False
                plan.parts = []
                return plan
            plan.parts = fixed
            return plan
        _keep_one_typo(again, allowed=False)
        again.parts, _ = style_guard.enforce(
            again.parts, self.persona.style, self.persona.boundaries
        )
        # 重写回来还带禁语的那条不发；一条不剩就这次不说。
        kept = self._drop_banned(again.parts)
        if again.parts and not kept:
            log.info("[brain] 主动消息重写后还是禁语，这次就不说了")
            again.send = False
        again.parts = kept
        return again

    async def generate_day_plan(self, req: DayPlanRequest, today: date) -> DayPlan | None:
        prompt = build_day_plan_user(
            persona=self.persona,
            now=req.now,
            state_line=req.state_line,
            mood_notes=req.mood_notes,
            wake_at=req.wake_at,
            sleep_at=req.sleep_at,
            classes=req.classes,
            yesterday=req.yesterday,
            summary=req.summary,
        )
        return await self._call(
            DayPlan, prompt, purpose="day_plan", today=today, model=self.settings.utility_model
        )

    async def update_memory(self, req: MemoryUpdateRequest, today: date) -> MemoryUpdate | None:
        prompt = build_memory_update_user(
            persona=self.persona,
            previous_summary=req.previous_summary,
            messages=req.messages,
            existing_owner_facts=req.existing_owner_facts,
            existing_self_facts=req.existing_self_facts,
        )
        return await self._call(
            MemoryUpdate, prompt, purpose="memory", today=today, model=self.settings.utility_model
        )


def build_client(settings: Settings) -> anthropic.AsyncAnthropic:
    """默认客户端。凭据由 SDK 自己从环境里解析。"""
    return anthropic.AsyncAnthropic(max_retries=2, timeout=120.0)
