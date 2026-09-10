"""调度器的测试。重点是崩溃恢复、不并发跑同一段对话、失败不打扰用户。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from newperson.clock import FakeClock
from newperson.memory import Memory
from newperson.models import Job
from newperson.scheduler import MAX_ATTEMPTS, Scheduler

TZ = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 12, 20, 0, tzinfo=TZ)


@pytest.fixture
async def parts(tmp_path: Path):
    mem = Memory(tmp_path / "s.db")
    await mem.open()
    clock = FakeClock(NOW)
    yield mem, clock, Scheduler(mem, clock)
    await mem.close()


async def test_a_due_job_runs(parts) -> None:
    memory, clock, sched = parts
    seen = []
    sched.register("reply", lambda job: seen.append(job.id) or asyncio.sleep(0))
    await sched.schedule("reply", NOW, conversation_id="owner")
    assert await sched.run_due_once() == 1
    assert len(seen) == 1


async def test_a_future_job_waits(parts) -> None:
    memory, clock, sched = parts
    sched.register("reply", lambda job: asyncio.sleep(0))
    await sched.schedule("reply", NOW + timedelta(hours=1), conversation_id="owner")
    assert await sched.run_due_once() == 0
    clock.advance(hours=2)
    assert await sched.run_due_once() == 1


async def test_a_job_runs_only_once(parts) -> None:
    memory, clock, sched = parts
    calls = []
    sched.register("reply", lambda job: calls.append(1) or asyncio.sleep(0))
    await sched.schedule("reply", NOW, conversation_id="owner")
    await sched.run_due_once()
    await sched.run_due_once()
    assert len(calls) == 1


async def test_one_conversation_runs_one_job_at_a_time(parts) -> None:
    """回复和主动消息不能在同一个频道里交错发出。"""
    memory, clock, sched = parts
    overlap = []
    running = []

    async def slow(job: Job) -> None:
        overlap.append(len(running))
        running.append(job.id)
        await asyncio.sleep(0.02)
        running.remove(job.id)

    sched.register("reply", slow)
    sched.register("proactive", slow)
    await sched.schedule("reply", NOW, conversation_id="owner")
    await sched.schedule("proactive", NOW, conversation_id="owner")
    await asyncio.gather(sched.run_due_once(), sched.run_due_once())
    assert max(overlap) == 0, "同一段对话跑了两个任务"


async def test_failures_are_retried_then_given_up(parts) -> None:
    """模型调不通就当没看手机，过一阵再试，绝不把错误发给对方。"""
    memory, clock, sched = parts
    attempts = []

    async def always_fails(job: Job) -> None:
        attempts.append(job.attempts)
        raise RuntimeError("模型超时")

    sched.register("reply", always_fails)
    job_id = await sched.schedule("reply", NOW, conversation_id="owner")

    for _ in range(MAX_ATTEMPTS):
        await sched.run_due_once()
        clock.advance(hours=1)

    assert len(attempts) == MAX_ATTEMPTS
    assert (await memory.get_job(job_id)).status == "failed"


async def test_a_retry_is_not_immediate(parts) -> None:
    memory, clock, sched = parts

    async def fails(job: Job) -> None:
        raise RuntimeError("网络抖了一下")

    sched.register("reply", fails)
    job_id = await sched.schedule("reply", NOW, conversation_id="owner")
    await sched.run_due_once()
    assert (await memory.get_job(job_id)).run_at > NOW


async def test_a_crashed_job_is_picked_back_up(parts) -> None:
    """进程崩在任务中间，重启后任务要回到队列。"""
    memory, clock, sched = parts
    job_id = await sched.schedule("reply", NOW, conversation_id="owner")
    await memory.claim_job(job_id, clock.now(), lease_seconds=60)

    clock.advance(minutes=30)
    await sched.recover()
    assert (await memory.get_job(job_id)).status == "pending"


async def test_stale_proactive_messages_are_dropped(parts) -> None:
    """停机一天再启动，半夜想说的话不该中午冒出来。"""
    memory, clock, sched = parts
    stale = await sched.schedule("proactive", NOW, conversation_id="owner")
    fresh = await sched.schedule("reply", NOW, conversation_id="owner")

    clock.advance(hours=20)
    await sched.recover()

    assert (await memory.get_job(stale)).status == "cancelled"
    assert (await memory.get_job(fresh)).status == "pending", "堆积的回复要照常处理"


async def test_dedupe_key_blocks_a_second_copy(parts) -> None:
    memory, clock, sched = parts
    assert await sched.schedule("day_plan", NOW, dedupe_key="day_plan:2026-10-12") > 0
    assert await sched.schedule("day_plan", NOW, dedupe_key="day_plan:2026-10-12") == 0


async def test_a_job_without_a_handler_does_not_hang(parts) -> None:
    memory, clock, sched = parts
    job_id = await sched.schedule("reply", NOW, conversation_id="owner")
    await sched.run_due_once()
    assert (await memory.get_job(job_id)).status == "cancelled"


async def test_cancel_keeps_it_from_running(parts) -> None:
    memory, clock, sched = parts
    calls = []
    sched.register("proactive", lambda job: calls.append(1) or asyncio.sleep(0))
    job_id = await sched.schedule("proactive", NOW, conversation_id="owner")
    await sched.cancel(job_id, "她这会儿不想说话")
    await sched.run_due_once()
    assert not calls


async def test_run_forever_stops_cleanly(parts) -> None:
    memory, clock, sched = parts
    done = asyncio.Event()

    async def handler(job: Job) -> None:
        done.set()

    sched.register("reply", handler)
    await sched.schedule("reply", NOW, conversation_id="owner")
    task = asyncio.create_task(sched.run_forever())
    await asyncio.wait_for(done.wait(), timeout=2)
    sched.stop()
    await asyncio.wait_for(task, timeout=2)


async def test_the_loop_survives_a_database_hiccup(parts) -> None:
    """循环本身出意外不能让她从此彻底不说话。

    那种故障没有任何征兆，你只会觉得她再也不理你了，比崩溃还难查。
    """
    memory, clock, sched = parts
    calls = []

    async def handler(job: Job) -> None:
        calls.append(job.id)

    sched.register("reply", handler)
    await sched.schedule("reply", NOW, conversation_id="owner")

    boom = [True]
    original = memory.due_jobs

    async def flaky(*args, **kwargs):
        if boom[0]:
            boom[0] = False
            raise RuntimeError("数据库这会儿读不了")
        return await original(*args, **kwargs)

    memory.due_jobs = flaky
    task = asyncio.create_task(sched.run_forever())
    for _ in range(100):
        # 真的等一小会儿：aiosqlite 在工作线程里跑，光 yield 事件循环推不动它
        await asyncio.sleep(0.01)
        if calls:
            break
    sched.stop()
    await asyncio.wait_for(task, timeout=2)
    assert calls, "第一次出意外之后循环就死了"
