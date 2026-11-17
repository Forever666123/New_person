"""催学：他把考前的学习安排放进一个单独的日历，到点她问一句。

日历里**每个事件都是一个学习块**，标题是"课号 + 做什么"。她读这个日历：

- **白天的块**（块开始时她醒着）：开始后问一句开始了没；他一直没回，再追问一次，更短；
  结束后问学到哪了。
- **晚上的块**（块开始时她在睡）：她睡前问一句今晚打算学到哪；第二天他那边早上、
  她醒着的时候问昨晚学到哪了。正好碰上一个"开始了没"，就并成一条。

分工跟别处一样：**代码只有机制，几点问、问几次、什么时候闭嘴全在 persona.yaml 的
``study_watch``**。几条不能破的线：

- 日历地址是密钥。不进日志、不进异常文本、不进体检。读失败只记异常的类型名。
- 块的标题只进给模型的 user 消息。日志、任务表、``study_log`` 里只有块的标识。
- 错过了就丢掉，不顺延。晚到的催促没有意义。
- 同一块的同一步一辈子只问一次：``study_log`` 的 (target, step) 唯一，发之前先抢。
- 不占平时主动消息的额度，他没回也不算被晾。不然她会越来越沉默。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import SecretStr

from .clock import Clock, later
from .memory import Memory
from .models import Job
from .persona import Persona, StudyWatchConfig, hhmm_to_minutes, parse_hhmm
from .rhythm import Rhythm
from .scheduler import Scheduler

log = logging.getLogger(__name__)

JOB_KIND = "study_check"
OFF_KEY = "study_off"
"""``!np study off`` 记在这里。"""
LOW_MOOD_KEY = "study_low_mood_at"
"""他最近一次说自己难受（触发了低气压模式）的时刻。只有时间。"""
OK_KEY = "study_cal_ok_at"
FAIL_SINCE_KEY = "study_cal_fail_since"
FAIL_KIND_KEY = "study_cal_fail_kind"
RECURRING_KEY = "study_cal_recurring_skipped"
SINCE_KEY = "study_since"
"""第一次真正开始盯日历的时刻。那之前就过去了的步骤不算"错过"。"""

STEPS_BLOCK = ("start", "nudge", "end")
"""对着某一块问的。"""
STEPS_NIGHT = ("evening", "morning")
"""对着某一晚问的（那一晚可能有好几块）。"""

FETCH_TIMEOUT = 20.0
MAX_ICS_BYTES = 5 * 1024 * 1024
FIRST_RETRY_MINUTES = 2.0
"""重启后还没读成过日历时，后台多久再读一次。"""
BUSY_RETRY_MINUTES = (3.0, 8.0)
"""到点时他还有话没回完：等这么久再看。"""


# ---------------------------------------------------------------------------
# iCal 解析（自己写，不引依赖）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StudyBlock:
    key: str
    """块的标识：UID 加开始时刻的摘要。改期之后是另一块，旧的那几步自然作废。"""
    start: datetime
    """UTC。"""
    end: datetime
    title: str


@dataclass
class ParsedCalendar:
    blocks: list[StudyBlock] = field(default_factory=list)
    recurring_skipped: int = 0
    """带 RRULE 的重复事件。展开它要一整套规则，先跳过，体检和 check 里提醒一句。"""
    all_day_skipped: int = 0
    cancelled_skipped: int = 0


_DT = re.compile(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})(Z?)$")
_DATE = re.compile(r"^\d{8}$")
_DURATION = re.compile(
    r"^([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$"
)
_ESCAPE = re.compile(r"\\([\\;,nN])")


def _unfold(text: str) -> list[str]:
    """RFC 5545 的行折叠：换行后面跟一个空格或制表符，就是上一行的接续。"""
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        elif raw:
            lines.append(raw)
    return lines


def _split_outside_quotes(text: str, sep: str) -> list[str]:
    out, buf, quoted = [], [], False
    for ch in text:
        if ch == '"':
            quoted = not quoted
        if ch == sep and not quoted:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    out.append("".join(buf))
    return out


def _content_line(line: str) -> tuple[str, dict[str, str], str] | None:
    """``NAME;PARAM=x:VALUE`` → (NAME, {PARAM: x}, VALUE)。冒号可能在带引号的参数里。"""
    quoted = False
    for i, ch in enumerate(line):
        if ch == '"':
            quoted = not quoted
        elif ch == ":" and not quoted:
            head, value = line[:i], line[i + 1 :]
            break
    else:
        return None
    pieces = _split_outside_quotes(head, ";")
    params: dict[str, str] = {}
    for piece in pieces[1:]:
        k, _, v = piece.partition("=")
        params[k.strip().upper()] = v.strip().strip('"')
    return pieces[0].strip().upper(), params, value


def unescape_text(value: str) -> str:
    """TEXT 类型的转义：``\\n`` 是换行，``\\,`` ``\\;`` ``\\\\`` 是字面量。"""

    def repl(m: re.Match[str]) -> str:
        ch = m.group(1)
        return "\n" if ch in "nN" else ch

    return _ESCAPE.sub(repl, value)


class _AllDay:
    """全天事件的标记。"""


_ALL_DAY = _AllDay()


def _parse_moment(
    value: str, params: dict[str, str], default_tz: ZoneInfo
) -> datetime | _AllDay | None:
    """DTSTART / DTEND 的三种写法：UTC 的 Z、带 TZID、浮动时间（按他的时区）。"""
    value = value.strip()
    if params.get("VALUE", "").upper() == "DATE" or _DATE.match(value):
        return _ALL_DAY
    m = _DT.match(value)
    if m is None:
        return None
    y, mo, d, h, mi, s = (int(g) for g in m.groups()[:6])
    try:
        if m.group(7):
            return datetime(y, mo, d, h, mi, s, tzinfo=UTC)
        tz = default_tz
        if tzid := params.get("TZID"):
            try:
                tz = ZoneInfo(tzid)
            except (ZoneInfoNotFoundError, ValueError):
                # 自定义的 VTIMEZONE 名字（Outlook 导出的那种）认不出来，按他的时区算。
                # Google 给的都是 IANA 名字，碰不到这里
                log.warning("[study] 日历里有认不出的时区，按他的时区算")
        return datetime(y, mo, d, h, mi, s, tzinfo=tz).astimezone(UTC)
    except ValueError:
        return None


def _parse_duration(value: str) -> timedelta | None:
    m = _DURATION.match(value.strip())
    if m is None:
        return None
    sign, w, d, h, mi, s = m.groups()
    delta = timedelta(
        weeks=int(w or 0), days=int(d or 0), hours=int(h or 0), minutes=int(mi or 0), seconds=int(s or 0)
    )
    return -delta if sign == "-" else delta


def block_key(uid: str, start: datetime) -> str:
    """块的标识。只是摘要，看不出标题。"""
    raw = f"{uid}|{start.astimezone(UTC).isoformat()}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def parse_ics(text: str, default_tz: ZoneInfo) -> ParsedCalendar:
    """把 iCal 文本解析成学习块。认不出的事件跳过，不抛异常。"""
    out = ParsedCalendar()
    stack: list[str] = []
    event: dict[str, tuple[dict[str, str], str]] | None = None
    for line in _unfold(text):
        parsed = _content_line(line)
        if parsed is None:
            continue
        name, params, value = parsed
        if name == "BEGIN":
            stack.append(value.strip().upper())
            if stack[-1] == "VEVENT":
                event = {}
            continue
        if name == "END":
            component = value.strip().upper()
            if stack and stack[-1] == component:
                stack.pop()
            if component == "VEVENT" and event is not None:
                _finish_event(event, default_tz, out)
                event = None
            continue
        # 只认 VEVENT 自己的属性。VTIMEZONE 里也有 DTSTART，VALARM 里也可能有别的
        if event is not None and stack and stack[-1] == "VEVENT":
            event.setdefault(name, (params, value))
    out.blocks.sort(key=lambda b: (b.start, b.key))
    return out


def _finish_event(
    event: dict[str, tuple[dict[str, str], str]], default_tz: ZoneInfo, out: ParsedCalendar
) -> None:
    if "RRULE" in event:
        out.recurring_skipped += 1
        return
    status = event.get("STATUS", ({}, ""))[1].strip().upper()
    if status == "CANCELLED":
        out.cancelled_skipped += 1
        return
    if "DTSTART" not in event:
        return
    start = _parse_moment(event["DTSTART"][1], event["DTSTART"][0], default_tz)
    if isinstance(start, _AllDay):
        out.all_day_skipped += 1
        return
    if start is None:
        return
    end: datetime | _AllDay | None = None
    if "DTEND" in event:
        end = _parse_moment(event["DTEND"][1], event["DTEND"][0], default_tz)
    elif "DURATION" in event:
        delta = _parse_duration(event["DURATION"][1])
        end = start + delta if delta is not None else None
    if not isinstance(end, datetime) or end <= start:
        return
    uid = event.get("UID", ({}, ""))[1].strip()
    title = unescape_text(event.get("SUMMARY", ({}, ""))[1]).strip()
    # 改期过的那一次（带 RECURRENCE-ID）是一个独立的块，照常算
    out.blocks.append(StudyBlock(block_key(uid or title, start), start, end, title))


# ---------------------------------------------------------------------------
# 取日历
# ---------------------------------------------------------------------------


class CalendarFetchError(Exception):
    """读日历失败。**消息里绝不带地址**，只有状态码或异常的类型名。"""


class CalendarSource(Protocol):
    async def fetch(self) -> str: ...


class HttpIcsSource:
    """用 discord.py 自带的 aiohttp 取私密 iCal 地址。"""

    session_factory: Callable[..., Any] | None = None
    """测试用：换成假的会话。None 就用 aiohttp.ClientSession。"""

    def __init__(self, url: SecretStr, timeout: float = FETCH_TIMEOUT) -> None:
        self._url = url
        self._timeout = timeout

    def __repr__(self) -> str:
        return "HttpIcsSource(***)"

    async def fetch(self) -> str:
        """读到底再返回。读不成抛 :class:`CalendarFetchError`，里面只有状态码或异常类名。

        **新异常在 except 块外面抛**：在里面抛的话，带地址的原异常会挂在
        ``__context__`` 上（``from None`` 只是不打印它，引用还在），
        谁把整条链打进日志就漏了。
        """
        import aiohttp

        failure: str | None = None
        chunks: list[bytes] = []
        try:
            factory = self.session_factory or aiohttp.ClientSession
            timeout = aiohttp.ClientTimeout(total=self._timeout)
            async with (
                factory(timeout=timeout) as session,
                session.get(self._url.get_secret_value(), allow_redirects=True) as resp,
            ):
                if resp.status != 200:
                    failure = f"HTTP {resp.status}"
                else:
                    # read(n) 只给当下缓冲里已有的那部分；分块传输时只解析到前半截，
                    # 后面的块会被当成"日历上没有了"作废。一直读到底
                    size = 0
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        size += len(chunk)
                        if size > MAX_ICS_BYTES:
                            failure = "日历太大"
                            break
                        chunks.append(chunk)
        except Exception as exc:  # noqa: BLE001 - aiohttp 的异常文本里常带着地址
            failure = type(exc).__name__
        if failure is not None:
            raise CalendarFetchError(failure)
        return b"".join(chunks).decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# 排期
# ---------------------------------------------------------------------------


@dataclass
class PlannedCheck:
    key: str
    """任务的去重键。"""
    run_at: datetime
    components: list[tuple[str, str]]
    """[(step, target)]。并成一条的时候不止一个。"""


@dataclass
class Prepared:
    """到点了、过完所有闸，真要问的那几步。"""

    components: list[tuple[str, str]]
    note: str
    planned: datetime | None


def _his_minutes(moment: datetime, tz: ZoneInfo) -> int:
    local = moment.astimezone(tz)
    return local.hour * 60 + local.minute


class StudyWatch:
    def __init__(
        self,
        persona: Persona,
        rhythm: Rhythm,
        memory: Memory,
        scheduler: Scheduler,
        clock: Clock,
        source: CalendarSource | None,
        conversation_id: str = "owner",
    ) -> None:
        self.persona = persona
        self.rhythm = rhythm
        self.memory = memory
        self.scheduler = scheduler
        self.clock = clock
        self.source = source
        self.conversation_id = conversation_id
        self._calendar: ParsedCalendar | None = None
        self._ok_at: datetime | None = None

    @property
    def cfg(self) -> StudyWatchConfig:
        return self.persona.study_watch

    @property
    def his_tz(self) -> ZoneInfo | None:
        return self.persona.owner_tz

    @property
    def configured(self) -> bool:
        """功能有没有开：人设开着、地址配了、知道他的时区。"""
        return bool(self.cfg.enabled and self.source is not None and self.his_tz is not None)

    # -- 随机 -------------------------------------------------------------

    def _rng(self, day: date, target: str, step: str) -> random.Random:
        """每块每步一个随机源，来自 (seed, 日期, 块, 哪一步)。

        不用全局 random：同一块重启前后抽出来的时刻一样，天与天、块与块之间互不相关。
        """
        return random.Random(f"{self.persona.seed}|{day.isoformat()}|{target}|{step}")

    def _delay(self, span: tuple[float, float], day: date, target: str, step: str) -> timedelta:
        lo, hi = span
        return timedelta(minutes=self._rng(day, target, step).uniform(lo, max(lo, hi)))

    # -- 日历 -------------------------------------------------------------

    def _fresh(self, now: datetime) -> bool:
        if self._calendar is None or self._ok_at is None:
            return False
        return now - self._ok_at <= timedelta(hours=self.cfg.stale_after_hours)

    def blocks(self, now: datetime) -> list[StudyBlock]:
        """现在认的学习块。日历太久没读成，就当一块都没有。"""
        if not self.configured or not self._fresh(now) or self._calendar is None:
            return []
        return list(self._calendar.blocks)

    @property
    def last_ok_at(self) -> datetime | None:
        return self._ok_at

    async def sync_once(self) -> bool:
        """读一次日历、重排。**不抛异常，不出声**：读失败沿用上一次的。

        整个包起来：库忙、排期出错，任何失败都只记一行不带内容的日志——
        催学坏了不能连累她正常收发消息。
        """
        try:
            return await self._sync()
        except Exception as exc:  # noqa: BLE001 - 见上
            log.warning("[study] 这一轮没同步成（%s），下一轮再来", type(exc).__name__)
            return False

    async def _sync(self) -> bool:
        if not self.configured or self.source is None or self.his_tz is None:
            return False
        now = self.clock.now()
        ok = False
        failure: str | None = None
        try:
            text = await self.source.fetch()
            parsed = parse_ics(text, self.his_tz)
        except Exception as exc:  # noqa: BLE001 - 读不成就是这会儿没看日历
            failure = str(exc) if isinstance(exc, CalendarFetchError) else type(exc).__name__
        if failure is not None:
            log.warning("[study] 日历没读成（%s），沿用上一次读到的", failure[:40])
            if not await self.memory.kv_get(FAIL_SINCE_KEY):
                await self.memory.kv_set(FAIL_SINCE_KEY, now.isoformat())
            await self.memory.kv_set(FAIL_KIND_KEY, failure[:40])
            if self._calendar is None:
                # 这个进程还没读成过：手里什么都没有，不能拿"没有"去作废重启前排好的任务。
                # 它们到点时自己再读一次、自己过闸
                return False
        else:
            ok = True
            self._calendar = parsed
            self._ok_at = now
            await self.memory.kv_set(OK_KEY, now.isoformat())
            await self.memory.kv_delete(FAIL_SINCE_KEY)
            await self.memory.kv_delete(FAIL_KIND_KEY)
            await self.memory.kv_set(RECURRING_KEY, str(parsed.recurring_skipped))
            if not await self.memory.kv_get(SINCE_KEY):
                await self.memory.kv_set(SINCE_KEY, now.isoformat())
            log.info(
                "[study] 日历读到 %d 块%s",
                len(parsed.blocks),
                f"，{parsed.recurring_skipped} 个重复事件跳过了" if parsed.recurring_skipped else "",
            )
        await self.apply_plan(now)
        return ok

    @property
    def waiting_for_calendar(self) -> bool:
        """这个进程还没读成过日历。重启前排下的催学先等等，别拿"没有"去判它。"""
        return self.configured and self._calendar is None

    async def run_forever(self) -> None:
        while True:
            try:
                # 还没读成过就隔两分钟再试：到点的催学在等它（它们自己不联网，见 prepare）
                minutes = FIRST_RETRY_MINUTES if self._calendar is None else self.cfg.poll_minutes
                await self.clock.sleep(max(minutes, 1.0) * 60)
                await self.sync_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 循环本身不能死
                log.warning("[study] 同步循环出意外（%s），接着跑", type(exc).__name__)

    async def in_use(self, now: datetime) -> bool:
        """此刻她该"知道"这件事：在用、没过截止日、他没关。"""
        return self.configured and not self.past_deadline(now) and not await self.is_owner_off()

    async def context_lines(self, now: datetime) -> list[str]:
        """给易变层（user 消息）的那几句：他把安排共享给她了，加上他今天排的块。

        不放 system prompt：没配地址、过了截止日的时候，她不该以为自己看得到他的日历；
        而且放进去就有易变内容，缓存永远不命中。
        """
        if not await self.in_use(now) or self.his_tz is None:
            return []
        until = await self.low_mood_until()
        if until is not None and now < until:
            # 他说过难受：这段时间一个字都不提学习，连"他今天排了什么"也别摆在她眼前
            return []
        lines = [self.cfg.knows.strip()] if self.cfg.knows.strip() else []
        today = now.astimezone(self.his_tz).date()
        todays = [b for b in self.blocks(now) if b.start.astimezone(self.his_tz).date() == today]
        if todays:
            lines.append("他今天排的学习：" + "；".join(self._when(b) for b in todays))
        return lines

    # -- 算时刻 -----------------------------------------------------------

    def _night_of(self, block: StudyBlock) -> tuple[datetime, datetime] | None:
        """块开始时她在睡的话，返回那一觉 (入睡, 起床)。"""
        return self.rhythm.sleep_window_containing(block.start)

    @staticmethod
    def night_id(window: tuple[datetime, datetime]) -> str:
        return "n" + window[0].astimezone(UTC).strftime("%Y%m%dT%H%M")

    def _his_date(self, moment: datetime) -> date:
        assert self.his_tz is not None
        return moment.astimezone(self.his_tz).date()

    def in_his_quiet(self, moment: datetime) -> bool:
        spec = self.cfg.his_quiet_hours.strip()
        if not spec or self.his_tz is None:
            return False
        start_s, _, end_s = spec.partition("-")
        start, end = hhmm_to_minutes(start_s), hhmm_to_minutes(end_s)
        minute = _his_minutes(moment, self.his_tz)
        if start <= end:
            return start <= minute < end
        return minute >= start or minute < end

    def _morning_moment(
        self, window: tuple[datetime, datetime], blocks: list[StudyBlock], nid: str
    ) -> tuple[datetime, datetime] | None:
        """第二天那一句排在哪：(最早可以的时刻, 抽出来的时刻)。

        他那边 morning_after 以后、她起床以后，再等一段。抽出来的时刻落在她睡着或者
        他的静默时段里，就往后找一个都醒着的时候，找不到就不问。
        """
        assert self.his_tz is not None
        last_end = max(b.end for b in blocks)
        h, m = parse_hhmm(self.cfg.morning_after)
        local_end = last_end.astimezone(self.his_tz)
        his_morning = datetime.combine(local_end.date(), time(h, m), tzinfo=self.his_tz)
        if his_morning.astimezone(UTC) < last_end:
            his_morning = datetime.combine(
                local_end.date() + timedelta(days=1), time(h, m), tzinfo=self.his_tz
            )
        earliest = max(window[1].astimezone(UTC), his_morning.astimezone(UTC))
        moment = earliest + self._delay(
            self.cfg.morning_delay_minutes, window[0].date(), nid, "morning"
        )
        for _ in range(48):
            if not self.rhythm.is_sleeping(moment) and not self.in_his_quiet(moment):
                return earliest, moment
            moment += timedelta(minutes=15)
        return None

    def plan(self, blocks: list[StudyBlock], now: datetime) -> list[PlannedCheck]:
        """这些块该在什么时候问什么。纯计算：同样的块、同样的人设，结果永远一样。"""
        if self.his_tz is None:
            return []
        cfg = self.cfg
        horizon = now + timedelta(hours=cfg.horizon_hours)
        items: list[PlannedCheck] = []
        nights: dict[str, tuple[tuple[datetime, datetime], list[StudyBlock]]] = {}
        for b in blocks:
            if b.end < now - timedelta(hours=36) or b.start > horizon:
                continue
            day = self._his_date(b.start)
            window = self._night_of(b)
            if window is None:
                items.append(
                    PlannedCheck(
                        f"study:start:{b.key}",
                        b.start + self._delay(cfg.start_delay_minutes, day, b.key, "start"),
                        [("start", b.key)],
                    )
                )
                items.append(
                    PlannedCheck(
                        f"study:end:{b.key}",
                        b.end + self._delay(cfg.end_delay_minutes, day, b.key, "end"),
                        [("end", b.key)],
                    )
                )
            else:
                nid = self.night_id(window)
                nights.setdefault(nid, (window, []))[1].append(b)

        merge = timedelta(minutes=cfg.merge_window_minutes)
        for nid, (window, night_blocks) in sorted(nights.items()):
            items.append(
                PlannedCheck(
                    f"study:evening:{nid}",
                    window[0] - self._delay(cfg.evening_lead_minutes, window[0].date(), nid, "evening"),
                    [("evening", nid)],
                )
            )
            found = self._morning_moment(window, night_blocks, nid)
            if found is None:
                continue
            earliest, morning = found
            # 前后 merge 分钟里正好有一个还没到点的"开始了没"，就搭它一起问。
            # 已经问过的那个搭不上了：那时候会单独问，study_log 保证不会问两遍
            hosts = [
                i for i in items
                if i.components[0][0] == "start"
                and i.run_at > now
                and i.run_at >= earliest
                and abs(i.run_at - morning) <= merge
            ]
            if hosts:
                host = min(hosts, key=lambda i: abs(i.run_at - morning))
                host.components.append(("morning", nid))
            else:
                items.append(PlannedCheck(f"study:morning:{nid}", morning, [("morning", nid)]))
        return sorted(items, key=lambda i: (i.run_at, i.key))

    async def apply_plan(self, now: datetime) -> int:
        """按最新的日历把任务排上，排着的、日历上已经没有的作废。返回新排了几个。"""
        blocks = self.blocks(now)
        planned = self.plan(blocks, now) if blocks else []
        wanted = {p.key: p for p in planned}

        for b in blocks:
            if now - timedelta(hours=36) <= b.end and b.start <= now + timedelta(
                hours=self.cfg.horizon_hours
            ):
                await self.memory.study_note_seen(b.key, b.start, now)

        for job in await self.memory.pending_jobs(JOB_KIND, self.conversation_id):  # type: ignore[arg-type]
            if job.payload.get("dynamic"):
                continue  # 追问是问完"开始了没"才排的，不在计划里，到点自己判断
            want = wanted.get(job.dedupe_key or "")
            if want is None:
                # 块取消了、改期了、日历太久没读成：作废，键让出来，万一又回来了还能排
                await self.scheduler.cancel(job.id or 0, "日历上没有了")
                await self.memory.release_dedupe_key(job.id or 0)
                continue
            fresh = [list(c) for c in want.components]
            if fresh != job.payload.get("components") or job.run_at != want.run_at:
                if want.run_at > now:
                    await self.scheduler.reschedule(
                        job.id or 0, want.run_at, {**job.payload, "components": fresh,
                                                   "planned": want.run_at.isoformat()}
                    )

        since_raw = await self.memory.kv_get(SINCE_KEY)
        since = datetime.fromisoformat(since_raw) if since_raw else now
        late = timedelta(minutes=self.cfg.max_late_minutes)
        count = 0
        for p in planned:
            todo = [c for c in p.components if await self.memory.study_logged(c[1], c[0]) is None]
            if not todo:
                continue
            if p.run_at < now - late:
                # 停机、日历晚到：这一步已经错过了。丢掉，不顺延
                if p.run_at >= since:
                    for step, target in todo:
                        await self.memory.study_mark_skipped(target, step, "missed", p.run_at, now)
                continue
            job_id = await self.scheduler.schedule(
                JOB_KIND,  # type: ignore[arg-type]
                max(p.run_at, now),
                conversation_id=self.conversation_id,
                payload={
                    "components": [list(c) for c in p.components],
                    "planned": p.run_at.isoformat(),
                },
                dedupe_key=p.key,
                reason=JOB_KIND,
            )
            count += bool(job_id)
        return count

    # -- 到点 -------------------------------------------------------------

    async def is_owner_off(self) -> bool:
        return bool(await self.memory.kv_get(OFF_KEY))

    async def note_low_mood(self, at: datetime) -> None:
        """他说了自己难受。只记时间，取最晚的。

        功能没在用（没配地址、过了截止日）就什么都不写：没配的时候库里不该多出任何东西。
        """
        if not self.configured or self.past_deadline(at):
            return
        raw = await self.memory.kv_get(LOW_MOOD_KEY)
        try:
            prev = datetime.fromisoformat(raw) if raw else None
        except ValueError:
            prev = None
        if prev is None or at > prev:
            await self.memory.kv_set(LOW_MOOD_KEY, at.isoformat())

    async def low_mood_until(self) -> datetime | None:
        raw = await self.memory.kv_get(LOW_MOOD_KEY)
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw) + timedelta(hours=self.cfg.low_mood_hours)
        except ValueError:
            return None

    def past_deadline(self, now: datetime) -> bool:
        return self.his_tz is not None and self._his_date(now) > self.cfg.until

    def _block(self, key: str, now: datetime) -> StudyBlock | None:
        return next((b for b in self.blocks(now) if b.key == key), None)

    def _night_blocks(self, nid: str, now: datetime) -> list[StudyBlock]:
        out = []
        for b in self.blocks(now):
            window = self._night_of(b)
            if window is not None and self.night_id(window) == nid:
                out.append(b)
        return out

    def _when(self, b: StudyBlock) -> str:
        """块的时间，按他那边的钟。**标题只出现在这里，只进给模型的 user 消息。**"""
        assert self.his_tz is not None
        s, e = b.start.astimezone(self.his_tz), b.end.astimezone(self.his_tz)
        title = b.title or "（没写标题）"
        return f"他那边 {s.strftime('%m-%d %H:%M')}–{e.strftime('%H:%M')}　{title}"

    async def prepare(
        self,
        job: Job,
        now: datetime,
        *,
        gates: list[tuple[str, bool]],
        he_spoke_since: Callable[[datetime], Awaitable[bool]],
    ) -> Prepared | None:
        """过闸、抢步骤、拼说明。返回 None 就是这次什么都不问（原因已经记进 study_log）。

        ``gates`` 是外面才知道的几道闸（暂停、请假、她刚说过要睡了……），
        按顺序，第一道拦住的就是原因。
        """
        comps = [(str(c[0]), str(c[1])) for c in job.payload.get("components", [])]
        raw_planned = job.payload.get("planned")
        planned = datetime.fromisoformat(raw_planned) if raw_planned else job.run_at
        todo = [c for c in comps if await self.memory.study_logged(c[1], c[0]) is None]
        if not todo:
            return None

        async def skip_all(why: str) -> None:
            log.info("[study] %s 不问了：%s", "、".join(s for s, _ in todo), why)
            for step, target in todo:
                await self.memory.study_mark_skipped(target, step, why, planned, now)

        reasons: list[tuple[str, bool]] = [
            ("off", not self.configured),
            ("owner_off", await self.is_owner_off()),
            ("past_deadline", self.past_deadline(now)),
            *gates,
            ("she_sleeps", self.rhythm.is_sleeping(now)),
            ("his_quiet", self.in_his_quiet(now)),
            ("too_late", now - planned > timedelta(minutes=self.cfg.max_late_minutes)),
            ("calendar_stale", not self._fresh(now)),
        ]
        until = await self.low_mood_until()
        reasons.insert(3, ("low_mood", until is not None and now < until))
        for why, hit in reasons:
            if hit:
                await skip_all(why)
                return None

        live: list[tuple[str, str]] = []
        lines: list[str] = []
        notes = self.cfg.notes
        for step, target in todo:
            if step in STEPS_BLOCK:
                block = self._block(target, now)
                if block is None:
                    await self.memory.study_mark_skipped(target, step, "block_gone", planned, now)
                    continue
                if step.startswith("nudge"):
                    after_raw = job.payload.get("after")
                    after = datetime.fromisoformat(after_raw) if after_raw else planned
                    if await he_spoke_since(after):
                        await self.memory.study_mark_skipped(target, step, "he_replied", planned, now)
                        continue
                    if now >= block.end:
                        await self.memory.study_mark_skipped(target, step, "block_over", planned, now)
                        continue
                note = {"start": notes.start, "end": notes.end}.get(step, notes.nudge)
                live.append((step, target))
                lines.append(f"{note.strip()}\n{self._when(block)}")
            else:
                blocks = self._night_blocks(target, now)
                if not blocks:
                    await self.memory.study_mark_skipped(target, step, "block_gone", planned, now)
                    continue
                note = notes.evening if step == "evening" else notes.morning
                live.append((step, target))
                lines.append(note.strip() + "\n" + "\n".join(self._when(b) for b in blocks))
        if not live:
            return None

        claimed = []
        for step, target in live:
            if await self.memory.study_claim(target, step, planned, now):
                claimed.append((step, target))
        if not claimed:
            return None
        if len(claimed) < len(live):
            keep = {c for c in claimed}
            lines = [line for c, line in zip(live, lines, strict=True) if c in keep]
        body = "\n\n".join(lines)
        if len(claimed) > 1:
            body = f"{notes.merged.strip()}\n\n{body}"
        return Prepared(components=claimed, note=body, planned=planned)

    async def release(self, prepared: Prepared) -> None:
        for step, target in prepared.components:
            await self.memory.study_release(target, step)

    async def skipped(self, prepared: Prepared, why: str) -> None:
        now = self.clock.now()
        for step, target in prepared.components:
            await self.memory.study_mark_skipped(target, step, why, prepared.planned, now)
        log.info("[study] %s 没问：%s", "、".join(s for s, _ in prepared.components), why)

    async def sent(self, prepared: Prepared, spoke_at: datetime) -> None:
        """问出口了：记账，问的是"开始了没"就排追问。"""
        for step, target in prepared.components:
            await self.memory.study_mark_sent(target, step, spoke_at)
        log.info("[study] 问了：%s", "、".join(s for s, _ in prepared.components))
        for step, target in prepared.components:
            n = 0 if step == "start" else _nudge_index(step)
            if n is None or n >= self.cfg.max_nudges:
                continue
            block = self._block(target, spoke_at)
            if block is None:
                continue
            nxt = "nudge" if n == 0 else f"nudge{n + 1}"
            run_at = later(
                spoke_at,
                self._delay(self.cfg.nudge_delay_minutes, self._his_date(block.start), target, nxt),
            )
            await self.scheduler.schedule(
                JOB_KIND,  # type: ignore[arg-type]
                run_at,
                conversation_id=self.conversation_id,
                payload={
                    "components": [[nxt, target]],
                    "planned": run_at.isoformat(),
                    "after": spoke_at.isoformat(),
                    "dynamic": True,
                },
                dedupe_key=f"study:{nxt}:{target}",
                reason=JOB_KIND,
            )

    def busy_retry_at(self, job: Job, now: datetime) -> datetime | None:
        """他有话没回完：过几分钟再看。再等就超过 max_late 的话就不等了。"""
        raw = job.payload.get("planned")
        planned = datetime.fromisoformat(raw) if raw else job.run_at
        rng = self._rng(planned.date(), str(job.id), f"busy{job.attempts}")
        run_at = later(now, timedelta(minutes=rng.uniform(*BUSY_RETRY_MINUTES)))
        if run_at - planned > timedelta(minutes=self.cfg.max_late_minutes):
            return None
        return run_at

    # -- 给人看 -----------------------------------------------------------

    async def status_lines(self, now: datetime) -> list[str]:
        """``!np status`` 里那几行。只有时间，没有标题。"""
        if not self.cfg.enabled:
            return []
        if self.source is None:
            return ["催学：人设里开着，但 .env 里没配日历地址"]
        lines = []
        if await self.is_owner_off():
            lines.append("催学：你用 `!np study off` 关着")
        elif self.past_deadline(now):
            lines.append(f"催学：过了截止日（{self.cfg.until}），不再问")
        tz = self.his_tz
        upcoming = [b for b in self.blocks(now) if b.end > now]
        if upcoming and tz is not None:
            nxt = upcoming[0]
            lines.append(
                f"下一个学习块：你那边 {nxt.start.astimezone(tz).strftime('%m-%d %H:%M')}"
                f"–{nxt.end.astimezone(tz).strftime('%H:%M')}"
            )
        elif self._fresh(now):
            lines.append("日历上接下来没有学习块")
        if self._ok_at is not None:
            mins = (now - self._ok_at).total_seconds() / 60
            ago = f"{mins:.0f} 分钟前" if mins < 120 else f"{mins / 60:.0f} 小时前"
            lines.append(f"学习日历上次读成功：{ago}")
        else:
            raw = await self.memory.kv_get(OK_KEY)
            lines.append(
                f"学习日历这次启动后还没读成功（上一次成功是 {raw[:16]}）" if raw
                else "学习日历还没读成功过"
            )
        return lines

    def describe(self) -> dict[str, Any]:
        """check 用的概况。不含地址、不含标题。"""
        cal = self._calendar
        return {
            "blocks": len(cal.blocks) if cal else 0,
            "recurring": cal.recurring_skipped if cal else 0,
            "all_day": cal.all_day_skipped if cal else 0,
        }


def _nudge_index(step: str) -> int | None:
    """nudge → 1，nudge2 → 2；别的步骤不是追问。"""
    if step == "nudge":
        return 1
    if step.startswith("nudge") and step[5:].isdigit():
        return int(step[5:])
    return None
