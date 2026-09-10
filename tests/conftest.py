"""共享测试夹具。"""

from __future__ import annotations

import random
from datetime import datetime
from pathlib import Path

import pytest

from newperson.clock import FakeClock
from newperson.persona import Persona, load_persona
from newperson.rhythm import Rhythm

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def persona() -> Persona:
    """沈亦宁的真实人设。"""
    return load_persona(ROOT / "persona" / "persona.yaml")


@pytest.fixture
def rhythm(persona: Persona) -> Rhythm:
    return Rhythm(persona.rhythm, persona.tz, persona.seed)


@pytest.fixture
def rng() -> random.Random:
    return random.Random(12345)


@pytest.fixture
def clock(persona: Persona) -> FakeClock:
    return FakeClock(datetime(2026, 9, 14, 20, 0, tzinfo=persona.tz))
