"""存储层的测试。重点是崩溃恢复、重复事件、记忆衰减。"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from newperson.memory import Memory
from newperson.models import Attachment, DayPlan, IncomingMessage, Job, LedgerEntry

TZ = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 12, 20, 0, tzinfo=TZ)
CONV = "owner"


@pytest.fixture
async def memory(tmp_path: Path):
    mem = Memory(tmp_path / "t.db")
    await mem.open()
    yield mem
    await mem.close()


def incoming(n: int, content: str = "在吗", at: datetime | None = None) -> IncomingMessage:
    return IncomingMessage(
        conversation_id=CONV,
        discord_message_id=1000 + n,
        author_id=42,
        author_name="Leo",
        content=content,
        created_at=at or NOW,
    )


# -- 消息 -------------------------------------------------------------------


async def test_stores_and_reads_back(memory: Memory) -> None:
    mid = await memory.add_user_message(incoming(1))
    assert mid > 0
    unread = await memory.unread_messages(CONV)
    assert [m.content for m in unread] == ["在吗"]
    assert unread[0].author_kind == "user"


async def test_gateway_replay_does_not_duplicate(memory: Memory) -> None:
    """Discord 断线重连会重发事件，插重了就当没发生。"""
    first = await memory.add_user_message(incoming(1))
    second = await memory.add_user_message(incoming(1))
    assert first == second
    assert len(await memory.unread_messages(CONV)) == 1


async def test_marking_read_clears_the_unread_list(memory: Memory) -> None:
    ids = [await memory.add_user_message(incoming(i)) for i in range(3)]
    await memory.mark_read(ids[:2], NOW)
    assert len(await memory.unread_messages(CONV)) == 1


async def test_attachments_survive_a_round_trip(memory: Memory) -> None:
    msg = incoming(1)
    msg.attachments = [Attachment(url="http://x/y.png", filename="y.png", content_type="image/png", size=10)]
    await memory.add_user_message(msg)
    got = (await memory.unread_messages(CONV))[0]
    assert got.attachments[0].filename == "y.png"


async def test_conversation_timestamps_update(memory: Memory) -> None:
    await memory.add_user_message(incoming(1, at=NOW))
    await memory.add_bot_message(CONV, "嗯", NOW + timedelta(minutes=2))
    conv = await memory.get_conversation(CONV)
    assert conv.last_user_message_at == NOW
    assert conv.last_bot_message_at == NOW + timedelta(minutes=2)


async def test_his_reply_resets_the_unanswered_counter(memory: Memory) -> None:
    """她主动开的话头对方回了，衰减就该清零。"""
    await memory.update_conversation(CONV, unanswered_initiations=3)
    await memory.add_user_message(incoming(1))
    assert (await memory.get_conversation(CONV)).unanswered_initiations == 0


async def test_detects_a_message_arriving_mid_delivery(memory: Memory) -> None:
    first = await memory.add_user_message(incoming(1))
    assert not await memory.has_newer_user_message(CONV, first)
    await memory.add_user_message(incoming(2))
    assert await memory.has_newer_user_message(CONV, first)


async def test_recent_messages_come_back_in_order(memory: Memory) -> None:
    for i in range(5):
        await memory.add_user_message(incoming(i, content=str(i), at=NOW + timedelta(minutes=i)))
    got = await memory.recent_messages(CONV, limit=3)
    assert [m.content for m in got] == ["2", "3", "4"]


# -- 记忆会淡 ----------------------------------------------------------------


async def test_fresh_facts_are_remembered(memory: Memory) -> None:
    await memory.add_facts("owner", ["他在便利店上班"], NOW)
    got = await memory.recall_facts("owner", NOW, half_life_days=45, threshold=0.25)
    assert got == ["他在便利店上班"]


async def test_old_facts_fade_away(memory: Memory) -> None:
    """太久没提起的事她会忘。这是故意的，什么都记得就没意思了。"""
    await memory.add_facts("owner", ["他提过一家咖啡店"], NOW)
    much_later = NOW + timedelta(days=200)
    assert await memory.recall_facts("owner", much_later, 45, 0.25) == []


async def test_mentioning_something_again_refreshes_it(memory: Memory) -> None:
    await memory.add_facts("owner", ["他在写筛选器"], NOW)
    later = NOW + timedelta(days=100)
    await memory.add_facts("owner", ["他在写筛选器"], later)
    assert await memory.recall_facts("owner", later + timedelta(days=10), 45, 0.25)


async def test_the_same_fact_is_not_stored_twice(memory: Memory) -> None:
    await memory.add_facts("owner", ["他一个人住"], NOW)
    await memory.add_facts("owner", ["他一个人住"], NOW)
    assert len(await memory.all_facts("owner")) == 1


# -- 台账 -------------------------------------------------------------------


async def test_ledger_keeps_what_he_said(memory: Memory) -> None:
    """她靠这个指出他前后不一致。"""
    await memory.add_ledger_entries(
        [LedgerEntry(claim="这次一定设止损", reason="上次亏怕了", committed_to="记进日志")], NOW
    )
    got = await memory.ledger("trading")
    assert got[0][1].claim == "这次一定设止损"


# -- 任务：崩溃恢复 ----------------------------------------------------------

def job(kind="reply", run_at=NOW, **kw) -> Job:
    return Job(kind=kind, run_at=run_at, conversation_id=CONV, **kw)


async def test_only_one_worker_can_claim_a_job(memory: Memory) -> None:
    jid = await memory.add_job(job(), NOW)
    assert await memory.claim_job(jid, NOW) is not None
    assert await memory.claim_job(jid, NOW) is None


async def test_a_crashed_job_comes_back(memory: Memory) -> None:
    """进程崩在任务中间，租约过期后任务要回到队列，不能永远卡在 running。"""
    jid = await memory.add_job(job(), NOW)
    await memory.claim_job(jid, NOW, lease_seconds=60)
    assert await memory.sweep_expired_leases(NOW + timedelta(seconds=30)) == 0
    assert await memory.sweep_expired_leases(NOW + timedelta(seconds=90)) == 1
    assert (await memory.get_job(jid)).status == "pending"


async def test_dedupe_key_stops_duplicates(memory: Memory) -> None:
    """启动和起床两条路径都想生成今天的日程，只能有一个成功。"""
    assert await memory.add_job(job(kind="day_plan", dedupe_key="day_plan:2026-10-12"), NOW) > 0
    assert await memory.add_job(job(kind="day_plan", dedupe_key="day_plan:2026-10-12"), NOW) == 0


async def test_progress_survives_a_restart(memory: Memory) -> None:
    """发到一半重启，从第几条接着发，不重新调模型。"""
    jid = await memory.add_job(job(), NOW)
    await memory.save_job_progress(jid, {"sent_parts": 2, "plan": {"parts": []}}, covers_upto_message_id=7)
    got = await memory.get_job(jid)
    assert got.progress["sent_parts"] == 2
    assert got.covers_upto_message_id == 7


async def test_due_jobs_respect_the_clock(memory: Memory) -> None:
    await memory.add_job(job(run_at=NOW + timedelta(hours=2)), NOW)
    assert await memory.due_jobs(NOW) == []
    assert len(await memory.due_jobs(NOW + timedelta(hours=3))) == 1


async def test_next_run_at_tells_the_scheduler_when_to_wake(memory: Memory) -> None:
    await memory.add_job(job(run_at=NOW + timedelta(hours=5)), NOW)
    await memory.add_job(job(run_at=NOW + timedelta(hours=1)), NOW)
    assert await memory.next_job_run_at() == NOW + timedelta(hours=1)


# -- 日程 -------------------------------------------------------------------


async def test_only_one_day_plan_generator_wins(memory: Memory) -> None:
    assert await memory.claim_day_plan(NOW.date()) is True
    assert await memory.claim_day_plan(NOW.date()) is False


async def test_day_plan_round_trip(memory: Memory) -> None:
    plan = DayPlan(date="2026-10-12", mood="有点累", thoughts=["project 还没开始"])
    await memory.save_day_plan(NOW.date(), plan)
    assert (await memory.get_day_plan(NOW.date())).mood == "有点累"


async def test_diary_notes_accumulate(memory: Memory) -> None:
    await memory.add_diary_note(NOW.date(), "跟他提了下雪", NOW)
    await memory.add_diary_note(NOW.date(), "发了张窗外", NOW + timedelta(hours=1))
    assert len(await memory.diary_notes(NOW.date())) == 2


# -- 照片与用量 --------------------------------------------------------------


async def test_a_photo_has_a_cooldown(memory: Memory) -> None:
    """同一张照片隔太近再发就露馅了。"""
    await memory.mark_photo_used("sunset-001", CONV, NOW)
    assert "sunset-001" in await memory.recently_used_photo_ids(NOW + timedelta(days=5))
    assert "sunset-001" not in await memory.recently_used_photo_ids(NOW + timedelta(days=40))


async def test_usage_accumulates_per_day(memory: Memory) -> None:
    await memory.record_usage(NOW.date(), input_tokens=100, output_tokens=20, estimated_usd=0.01)
    await memory.record_usage(NOW.date(), input_tokens=50, output_tokens=10, estimated_usd=0.005)
    got = await memory.usage_for(NOW.date())
    assert got["calls"] == 2
    assert got["input_tokens"] == 150
    assert got["estimated_usd"] == pytest.approx(0.015)


async def test_kv_round_trip(memory: Memory) -> None:
    assert await memory.kv_get("paused") is None
    await memory.kv_set("paused", "1")
    assert await memory.kv_get("paused") == "1"
    await memory.kv_delete("paused")
    assert await memory.kv_get("paused") is None


async def test_everything_survives_reopening(tmp_path: Path) -> None:
    path = tmp_path / "persist.db"
    mem = Memory(path)
    await mem.open()
    await mem.add_user_message(incoming(1))
    await mem.add_facts("owner", ["他在悉尼"], NOW)
    await mem.close()

    again = Memory(path)
    await again.open()
    assert len(await again.unread_messages(CONV)) == 1
    assert await again.recall_facts("owner", NOW, 45, 0.25) == ["他在悉尼"]
    await again.close()
