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
    """Discord 断线重连会重发事件，插重了就当没发生。

    **重复的那次必须返回 0，不能返回已有那条的 id。**
    调用方靠的就是这个：`discord_bot.on_user_message` 里写着
    `if not stored_id: return  # 网关重发，已经处理过了`。
    返回已有 id 的话那道防线永远不生效——同一条消息会被再排一次回复，
    而补抓跟实时消息撞上时还会被算成"补回来了 N 条"。
    """
    first = await memory.add_user_message(incoming(1))
    second = await memory.add_user_message(incoming(1))
    assert first, "第一次要返回真实的 id"
    assert second == 0, "重复的那次要返回 0，否则上层的去重判断形同虚设"
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
    assert got[0][2].claim == "这次一定设止损"


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
    assert await memory.claim_day_plan(NOW.date(), NOW) is True
    assert await memory.claim_day_plan(NOW.date(), NOW) is False


async def test_a_late_night_note_does_not_block_the_day_plan(memory: Memory) -> None:
    """她过了午夜还在回消息，inner_note 会先把那天的日记行建出来。

    早先拿"行存不存在"当锁，于是早上再想生成日程就永远抢不到，
    那一整天没有日程、没有任何主动消息，而且一个字的日志都没有。
    人设的入睡中位数在午夜之后，这条路径每周都会踩到。
    """
    await memory.add_diary_note(NOW.date(), "他今天听着挺累", NOW)
    assert await memory.claim_day_plan(NOW.date(), NOW) is True


async def test_a_stuck_claim_expires(memory: Memory) -> None:
    """生成到一半进程被杀，不能让这一天再也生成不出来。"""
    assert await memory.claim_day_plan(NOW.date(), NOW) is True
    assert await memory.claim_day_plan(NOW.date(), NOW + timedelta(minutes=5)) is False
    assert await memory.claim_day_plan(NOW.date(), NOW + timedelta(hours=1)) is True


async def test_a_finished_day_plan_is_never_reclaimed(memory: Memory) -> None:
    await memory.claim_day_plan(NOW.date(), NOW)
    await memory.save_day_plan(NOW.date(), DayPlan(date="2026-10-12", mood="还行"))
    assert await memory.claim_day_plan(NOW.date(), NOW + timedelta(days=1)) is False


async def test_releasing_a_claim_keeps_the_notes(memory: Memory) -> None:
    """生成失败要放掉抢占，但那一行里可能已经有日记了，不能整行删掉。"""
    await memory.add_diary_note(NOW.date(), "写了点什么", NOW)
    await memory.claim_day_plan(NOW.date(), NOW)
    await memory.release_day_plan(NOW.date())
    assert len(await memory.diary_notes(NOW.date())) == 1
    assert await memory.claim_day_plan(NOW.date(), NOW) is True


async def test_messages_can_be_put_back_in_the_unread_pile(memory: Memory) -> None:
    """存下来的回复读不出来时，那批消息要能放回去重新处理。"""
    ids = [await memory.add_user_message(incoming(i)) for i in range(3)]
    await memory.mark_read(ids, NOW)
    assert await memory.unread_messages(CONV) == []
    assert await memory.restore_unread(CONV, ids[-1]) == 3
    assert len(await memory.unread_messages(CONV)) == 3


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


async def test_recording_her_own_message_never_blows_up(memory: Memory) -> None:
    """她说的话已经发到对方手机上了，入库再抛异常会让整个任务失败重试，
    后面的日记、台账、跟进全部跳过。"""
    await memory.add_bot_message(CONV, "嗯", NOW, discord_message_id=999)
    await memory.add_bot_message(CONV, "又一条", NOW + timedelta(minutes=1), discord_message_id=999)
    conv = await memory.get_conversation(CONV)
    assert conv.last_bot_message_at == NOW + timedelta(minutes=1)


async def test_restoring_unread_only_puts_back_that_one_batch(memory: Memory) -> None:
    """放回未读只该放回**那一批**，不是整部历史。

    原来的条件是 `id <= upto_id AND read_at IS NOT NULL`，没有下界——
    那是这个会话所有已读的消息。一次读不出存下来的 plan，
    她就会把十几天前的几十条当成一批未读，一次性重新回一遍。
    """
    at = datetime(2026, 9, 20, 20, 0, tzinfo=TZ)
    batches: list[list[int]] = []
    for b in range(5):
        ids = []
        for j in range(2):
            msg = await memory.add_user_message(
                IncomingMessage(
                    conversation_id="owner",
                    discord_message_id=1000 + b * 10 + j,
                    author_id=42,
                    author_name="Leo",
                    content=f"第 {b}-{j} 句",
                    created_at=at,
                )
            )
            ids.append(msg)
            at += timedelta(minutes=1)
        await memory.mark_read(ids, at)  # 一批一个 read_at，线上就是这样
        at += timedelta(hours=6)
        batches.append(ids)

    assert await memory.unread_messages("owner") == []
    restored = await memory.restore_unread("owner", batches[3][-1])
    assert restored == 2, f"放回了 {restored} 条，该只有那一批的两条"
    assert sorted(m.id for m in await memory.unread_messages("owner")) == batches[3]

    # 再放一次什么都不会发生：那批已经是未读了
    assert await memory.restore_unread("owner", batches[3][-1]) == 0
