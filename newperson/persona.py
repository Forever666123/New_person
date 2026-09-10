"""人物设定（persona.yaml）的加载与校验。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def parse_hhmm(value: str) -> tuple[int, int]:
    """把 ``"HH:MM"`` 解析成 ``(hour, minute)``，格式不对就抛 ``ValueError``。"""
    m = _HHMM.match(value.strip())
    if not m:
        raise ValueError(f"时间格式应为 HH:MM，收到 {value!r}")
    return int(m.group(1)), int(m.group(2))


class BusyBlock(BaseModel):
    start: str
    end: str
    title: str = "忙"

    @field_validator("start", "end")
    @classmethod
    def _check(cls, v: str) -> str:
        parse_hhmm(v)
        return v


class DaySchedule(BaseModel):
    sleep: tuple[str, str] = ("23:30", "07:30")
    """(入睡, 起床)，可跨午夜。"""
    busy: list[BusyBlock] = Field(default_factory=list)

    @field_validator("sleep")
    @classmethod
    def _check_sleep(cls, v: tuple[str, str]) -> tuple[str, str]:
        parse_hhmm(v[0])
        parse_hhmm(v[1])
        if v[0] == v[1]:
            raise ValueError("入睡时间和起床时间不能相同")
        return v

    @model_validator(mode="before")
    @classmethod
    def _coerce_busy(cls, data):  # type: ignore[no-untyped-def]
        # 允许 yaml 里写成 ["09:00", "12:00", "上班"] 或 ["09:00", "12:00"] 的简写
        if isinstance(data, dict) and isinstance(data.get("busy"), list):
            blocks = []
            for item in data["busy"]:
                if isinstance(item, (list, tuple)):
                    if len(item) == 2:
                        blocks.append({"start": item[0], "end": item[1]})
                    elif len(item) >= 3:
                        blocks.append({"start": item[0], "end": item[1], "title": item[2]})
                    else:
                        raise ValueError(f"busy 区间格式不对：{item!r}")
                else:
                    blocks.append(item)
            data = {**data, "busy": blocks}
        return data


class RhythmConfig(BaseModel):
    weekday: DaySchedule = Field(default_factory=DaySchedule)
    weekend: DaySchedule = Field(default_factory=lambda: DaySchedule(sleep=("00:30", "09:30")))
    winding_down_minutes: int = 60
    busy_status: Literal["idle", "dnd"] = "idle"


class TimingConfig(BaseModel):
    max_delay_hours: float = 10.0
    urgent_multiplier: float = 0.6
    typing_chars_per_second: float = 3.5


class ProactiveConfig(BaseModel):
    max_per_day: int = 3
    base_probability: float = 0.5
    reach_out_after_silent_days: int = 3
    random_chat_slots: int = 1


class MemoryConfig(BaseModel):
    recent_messages: int = 40
    summarize_after: int = 60


class OwnerInfo(BaseModel):
    nickname: str = "你"
    notes: str = ""


class RulesConfig(BaseModel):
    never_admit_ai: bool = True
    extra_rules: str = ""


class Persona(BaseModel):
    name: str
    timezone: str = "Asia/Shanghai"
    language: str = "zh-CN"
    background: str = ""
    speaking_style: str = ""
    relationship: str = ""
    owner: OwnerInfo = Field(default_factory=OwnerInfo)
    rhythm: RhythmConfig = Field(default_factory=RhythmConfig)
    timing: TimingConfig = Field(default_factory=TimingConfig)
    proactive: ProactiveConfig = Field(default_factory=ProactiveConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    rules: RulesConfig = Field(default_factory=RulesConfig)

    @field_validator("timezone")
    @classmethod
    def _check_tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except ZoneInfoNotFoundError as e:
            raise ValueError(f"未知时区 {v!r}") from e
        return v

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def placeholders(self) -> list[str]:
        """返回仍含【待填】占位的字段名，供 ``check`` 命令提示。"""
        out = []
        for name in ("background", "speaking_style", "relationship"):
            if "【待填】" in getattr(self, name):
                out.append(name)
        if "【待填】" in self.owner.notes:
            out.append("owner.notes")
        return out


def load_persona(path: str | Path) -> Persona:
    """从 yaml 文件加载人物设定。文件不存在或格式错误会抛异常。"""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"找不到人设文件 {p}。请复制 persona/persona.example.yaml 为 {p} 并填写。"
        )
    with p.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return Persona.model_validate(raw)
