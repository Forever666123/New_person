"""调度器的测试。重点是崩溃恢复、不并发跑同一段对话、失败不打扰用户。"""

from __future__ import annotations

import asyncio
import random
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


async def test_a_handler_that_reschedules_itself_is_not_marked_done(parts) -> None:
    """handler 自己把任务推后了，调度器不能再盖成 done。

    暂停期间就是这个路径：回复被推后十分钟，但返回后被标成 done，
    resume 之后那条回复永远发不出去，消息一直躺在未读里。
    """
    memory, clock, sched = parts

    async def defers(job: Job) -> None:
        await sched.reschedule(job.id or 0, clock.now() + timedelta(minutes=10))

    sched.register("reply", defers)
    job_id = await sched.schedule("reply", NOW, conversation_id="owner")
    await sched.run_due_once()

    got = await memory.get_job(job_id)
    assert got.status == "pending", "被推后的任务不该变成 done"
    assert got.run_at > NOW


async def test_a_backlog_does_not_all_come_out_at_once_after_a_restart(parts) -> None:
    """停机期间攒下的任务，不能在进程起来那一分钟里一起涌出来。

    这是整个系统里最容易露馅的一幕：部署或者机器重启要几分钟，
    期间到点的回复会在 recover() 之后的第一轮全部执行——
    于是她在进程起来三十秒后，回了一条你三小时前发的消息。
    没有人是这样的。一个人重新拿起手机，是过一会儿才看到的。
    """
    memory, clock, sched = parts
    sched.rng = random.Random(5)
    for i in range(4):
        await sched.schedule(
            "reply", NOW - timedelta(hours=3), conversation_id=f"c{i}", dedupe_key=f"k{i}"
        )

    await sched.recover()

    jobs = await memory.pending_jobs()
    assert len(jobs) == 4
    for job in jobs:
        gap = (job.run_at - NOW).total_seconds()
        assert gap > 60, f"{job.conversation_id} 只往后挪了 {gap:.0f} 秒，起来就发跟没挪一样"
    # 而且不能全挪到同一刻，那又是另一种整齐
    assert len({j.run_at for j in jobs}) > 1


async def test_a_job_that_just_came_due_still_runs_immediately(parts) -> None:
    """刚到点的不算积压。

    把"轮到它了"也当成积压去打散的话，每一条回复都要平白多等几分钟，
    她会显得比设计的还要慢。
    """
    memory, clock, sched = parts
    await sched.schedule("reply", NOW, conversation_id="owner")

    await sched.recover()

    jobs = await memory.pending_jobs()
    assert len(jobs) == 1
    assert jobs[0].run_at == NOW


async def test_the_staleness_clock_survives_repeated_recovery(parts) -> None:
    """过期判定按最初排的时刻算，不按被打散之后的 run_at 算。

    _spread_overdue_jobs 每次 recover 都会改写 run_at，于是"过期了多久"
    被一次次清零。机器每小时重启一次的话，一条凌晨的 sign_off 可以一路
    被推到中午还是 pending——而它两点半就该没意义了。
    """
    memory, clock, sched = parts
    await sched.schedule("sign_off", NOW, conversation_id="owner", dedupe_key="s1")

    for step in range(1, 8):
        clock.set(NOW + timedelta(minutes=25 * step))
        await sched.recover()

    jobs = await memory.pending_jobs()
    assert jobs == [], f"停机快三小时了还没作废：{[(j.kind, j.run_at) for j in jobs]}"


async def test_a_cancelled_one_shot_can_be_scheduled_again(parts) -> None:
    """作废的行不能永远占着去重键。

    dedupe_key 是全表唯一的，作废的行照样占着它，于是同一个键再也排不进来。
    对"一辈子一次"的开场来说，那等于永久销毁：kv 标记还在，
    ensure_opener 下次启动直接跳过，那句话再也不会有了。
    """
    memory, clock, sched = parts
    await memory.kv_set("opener", "2026-10-12T20:00:00+00:00")
    first = await sched.schedule("proactive", NOW, conversation_id="c", dedupe_key="opener")
    assert first

    clock.set(NOW + timedelta(hours=5))
    await sched.recover()
    assert await memory.pending_jobs() == []
    assert await memory.kv_get("opener") is None, "键让出来了，标记也该清掉"

    second = await sched.schedule("proactive", clock.now(), conversation_id="c", dedupe_key="opener")
    assert second, "作废之后应该能重新排"
