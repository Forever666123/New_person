"""共享测试夹具。"""

from __future__ import annotations

import random
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from newperson.clock import FakeClock
from newperson.config import Settings
from newperson.persona import Persona, load_persona
from newperson.rhythm import Rhythm

ROOT = Path(__file__).resolve().parent.parent
TZ = ZoneInfo("Asia/Shanghai")


@pytest.fixture
def persona() -> Persona:
    """示例人设（工作日 23:30–07:30 睡，09–12 / 13:30–18 上班；周末 00:30–09:30 睡）。"""
    return load_persona(ROOT / "persona" / "persona.example.yaml")


@pytest.fixture
def rhythm(persona: Persona) -> Rhythm:
    return Rhythm(persona.rhythm, persona.tz)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        discord_bot_token="x",
        owner_user_id=1,
        db_path=tmp_path / "t.db",
        downloads_dir=tmp_path / "dl",
        generated_dir=tmp_path / "gen",
        persona_path=ROOT / "persona" / "persona.example.yaml",
        photos_index=ROOT / "persona" / "photos" / "index.example.yaml",
    )


@pytest.fixture
def rng() -> random.Random:
    return random.Random(12345)


def at(y: int, mo: int, d: int, h: int, mi: int = 0, s: int = 0) -> datetime:
    """2026-09-10 是周四。"""
    return datetime(y, mo, d, h, mi, s, tzinfo=TZ)


@pytest.fixture
def thursday_noon() -> datetime:
    return at(2026, 9, 10, 12, 0)


@pytest.fixture
def clock(thursday_noon: datetime) -> FakeClock:
    return FakeClock(thursday_noon)
