import asyncio
import contextlib
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src import flow
from src.config import Config
from src.db import Database
from src.flow import Deps, process_dialog_message, scheduler_tick
from tests.fakes import FakeBot, FakeCalendar

TZ = ZoneInfo("Asia/Yekaterinburg")
NOW = datetime(2026, 7, 24, 15, 0, tzinfo=TZ)
DUE = datetime(2026, 7, 25, 17, 0, tzinfo=TZ)


@pytest.fixture
async def deps(tmp_path):
    db = Database(str(tmp_path / "t.sqlite3"))
    await db.init()
    cfg = Config(bot_token="t", her_user_id=100, admin_user_id=200, _env_file=None)
    return Deps(bot=FakeBot(), db=db, cal=FakeCalendar(), cfg=cfg)


async def make(deps):
    await process_dialog_message("напомни завтра в 17 сдать кровь", deps, NOW)
    deps.bot.sent.clear()
    deps.cal.created.clear()


async def test_not_due_yet_no_ping(deps):
    await make(deps)
    await scheduler_tick(deps, DUE - timedelta(minutes=1))
    assert deps.bot.sent == []


async def test_first_ping(deps):
    await make(deps)
    await scheduler_tick(deps, DUE)
    assert len(deps.bot.sent) == 1
    assert "Напоминаю" in deps.bot.sent[0].text
    assert deps.bot.sent[0].chat_id == 100
    assert (await deps.db.get(1)).pings_sent == 1


async def test_reping_only_after_interval(deps):
    await make(deps)
    await scheduler_tick(deps, DUE)
    await scheduler_tick(deps, DUE + timedelta(minutes=10))
    assert len(deps.bot.sent) == 1
    await scheduler_tick(deps, DUE + timedelta(minutes=30))
    assert len(deps.bot.sent) == 2


async def test_confirmed_reminder_stops_pinging(deps):
    await make(deps)
    await scheduler_tick(deps, DUE)
    await deps.db.set_status(1, "confirmed")
    await scheduler_tick(deps, DUE + timedelta(minutes=30))
    assert len(deps.bot.sent) == 1


async def test_expires_after_max_repings(deps):
    await make(deps)
    t = DUE
    for _ in range(4):  # first ping + 3 repings
        await scheduler_tick(deps, t)
        t += timedelta(minutes=30)
    assert (await deps.db.get(1)).pings_sent == 4
    await scheduler_tick(deps, t)
    r = await deps.db.get(1)
    assert r.status == "expired"
    assert any(m.chat_id == 200 for m in deps.bot.sent)


async def test_calendar_retry_creates_missing_event(deps):
    deps.cal.fail = True
    await process_dialog_message("напомни завтра в 17 сдать кровь", deps, NOW)
    deps.bot.sent.clear()
    assert (await deps.db.get(1)).calendar_pending == 1
    deps.cal.fail = False
    await scheduler_tick(deps, NOW + timedelta(minutes=1))
    r = await deps.db.get(1)
    assert r.calendar_pending == 0
    assert r.calendar_uid is not None
    assert len(deps.cal.created) == 1


async def test_calendar_retry_failure_keeps_pending(deps):
    deps.cal.fail = True
    await process_dialog_message("напомни завтра в 17 сдать кровь", deps, NOW)
    deps.bot.sent.clear()
    await scheduler_tick(deps, NOW + timedelta(minutes=1))
    r = await deps.db.get(1)
    assert r.calendar_pending == 1
    assert r.calendar_uid is None


async def test_crashed_row_without_event_is_repaired(deps):
    # A process death between the INSERT and set_calendar leaves a row with no
    # calendar event; it must still be repaired rather than silently missing
    # from her calendar forever.
    await deps.db.add_pending("Сдать кровь", DUE)
    r = await deps.db.get(1)
    assert r.calendar_uid is None
    await scheduler_tick(deps, NOW)
    r = await deps.db.get(1)
    assert r.calendar_uid is not None
    assert r.calendar_pending == 0


async def test_retry_replaces_stale_event_instead_of_orphaning_it(deps):
    await make(deps)
    original_uid = (await deps.db.get(1)).calendar_uid
    await deps.db.set_calendar(1, original_uid, 1)
    await scheduler_tick(deps, NOW)
    r = await deps.db.get(1)
    assert deps.cal.deleted == [original_uid]
    assert r.calendar_uid != original_uid
    assert r.calendar_pending == 0


# --- one bad row must not sink the others (finding 1) ---


async def test_ping_failure_for_one_row_does_not_block_others(deps):
    await process_dialog_message("напомни сегодня в 15:30 дело а", deps, NOW)
    await process_dialog_message("напомни сегодня в 15:30 дело б", deps, NOW)
    deps.bot.sent.clear()
    due = NOW.replace(hour=15, minute=30)
    deps.bot.fail_predicate = lambda chat_id, text: "дело а" in text.lower()
    await scheduler_tick(deps, due)
    sent_lower = [m.text.lower() for m in deps.bot.sent]
    assert any("дело б" in t for t in sent_lower)
    r_a, r_b = await deps.db.get(1), await deps.db.get(2)
    assert r_a.pings_sent == 0 and r_a.delivery_failures == 1
    assert r_b.pings_sent == 1 and r_b.delivery_failures == 0


async def test_permanently_undeliverable_reminder_expires_with_one_alert(deps):
    await make(deps)
    deps.bot.fail_chat_ids.add(deps.cfg.her_user_id)
    t = DUE
    for _ in range(deps.cfg.max_delivery_failures):
        await scheduler_tick(deps, t)
        t += timedelta(minutes=1)
    r = await deps.db.get(1)
    assert r.status == "expired"
    admin_alerts = [m for m in deps.bot.sent if m.chat_id == deps.cfg.admin_user_id]
    assert len(admin_alerts) == 1
    # One further tick must not process the (no longer pending) row again.
    await scheduler_tick(deps, t)
    assert len([m for m in deps.bot.sent if m.chat_id == deps.cfg.admin_user_id]) == 1


async def test_run_scheduler_survives_a_raising_tick(deps, monkeypatch):
    calls = []

    async def fake_tick(d, now):
        calls.append(now)
        if len(calls) == 1:
            raise RuntimeError("boom")

    monkeypatch.setattr(flow, "scheduler_tick", fake_tick)
    deps.cfg.tick_seconds = 0
    task = asyncio.create_task(flow.run_scheduler(deps))
    for _ in range(200):
        if len(calls) >= 3:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert len(calls) >= 3
    assert any(m.chat_id == deps.cfg.admin_user_id for m in deps.bot.sent)


# --- CalDAV outage handling must not spam the admin or delay pings (finding 4) ---


async def test_outage_produces_at_most_one_admin_alert_per_tick(deps):
    for i in range(4):
        await process_dialog_message(f"напомни завтра в 17 дело {i}", deps, NOW)
    for rid in range(1, 5):
        await deps.db.set_calendar(rid, None, 1)
    deps.bot.sent.clear()
    deps.cal.fail = True
    await scheduler_tick(deps, NOW)
    admin_alerts = [m for m in deps.bot.sent if m.chat_id == deps.cfg.admin_user_id]
    assert len(admin_alerts) == 1


async def test_calendar_retry_backs_off_instead_of_retrying_every_tick(deps):
    deps.cal.fail = True
    await process_dialog_message("напомни завтра в 17 сдать кровь", deps, NOW)
    deps.bot.sent.clear()
    t = NOW + timedelta(seconds=30)
    await scheduler_tick(deps, t)  # first retry: attempts 0 -> 1, still fails
    assert (await deps.db.get(1)).calendar_attempts == 1
    t += timedelta(seconds=30)
    await scheduler_tick(deps, t)  # too soon (base backoff is minutes, not seconds)
    assert (await deps.db.get(1)).calendar_attempts == 1
    t += timedelta(minutes=deps.cfg.calendar_retry_base_minutes + 1)
    await scheduler_tick(deps, t)  # backoff elapsed: retries again
    assert (await deps.db.get(1)).calendar_attempts == 2


async def test_ping_pass_runs_before_calendar_repair_pass(deps):
    # A calendar repair that hangs (see calendar_client.py's timeout for the
    # real-world case) must never delay a ping that is already due.
    await make(deps)
    deps.bot.sent.clear()
    await deps.db.set_calendar(1, None, 1)
    deps.cal.gate = threading.Event()
    task = asyncio.create_task(scheduler_tick(deps, DUE))
    for _ in range(200):
        if deps.bot.sent:
            break
        await asyncio.sleep(0.01)
    assert len(deps.bot.sent) == 1
    assert "Напоминаю" in deps.bot.sent[0].text
    deps.cal.gate.set()
    await asyncio.wait_for(task, timeout=2)


# --- a tick landing mid-create must not duplicate the calendar event (finding 3) ---


async def test_tick_during_create_does_not_duplicate_calendar_event(deps):
    deps.cal.gate = threading.Event()
    create_task = asyncio.create_task(
        process_dialog_message("напомни завтра в 17 сдать кровь", deps, NOW))
    for _ in range(200):
        if 1 in deps.cal_locks:
            break
        await asyncio.sleep(0.01)
    assert (await deps.db.get(1)).calendar_pending == 1  # create is mid-flight
    tick_task = asyncio.create_task(scheduler_tick(deps, NOW))
    await asyncio.sleep(0.05)  # let the tick reach (and block on) the same lock
    deps.cal.gate.set()
    await create_task
    await tick_task
    assert len(deps.cal.created) == 1
    r = await deps.db.get(1)
    assert r.calendar_uid == deps.cal.created[0][0]
    assert r.calendar_pending == 0
