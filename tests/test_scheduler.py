import asyncio
import contextlib
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import aiosqlite
import pytest

from src import flow
from src.config import Config
from src.db import Database, to_iso
from src.flow import Deps, on_callback, process_dialog_message, scheduler_tick
from tests.fakes import FakeBot, FakeCalendar
from tests.test_db import OLD_SCHEMA

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


# --- an existing pre-backoff database must not brick the scheduler (regression) ---


async def test_scheduler_runs_cleanly_against_a_migrated_pre_backoff_database(tmp_path):
    # Reproduces the reviewer's finding: a database file created by an older
    # commit (before calendar_attempts/delivery_failures/etc existed) must be
    # migrated by init(), not just silently accepted and then blow up every
    # query with "no such column" the moment the scheduler touches it.
    # run_scheduler uses the real wall clock, not a fixed `now` -- so "due"
    # for the ping row and "not due yet" for the calendar-only row both need
    # to be relative to real time, not the fictional NOW used elsewhere here.
    real_now = datetime.now(timezone.utc)
    old_path = str(tmp_path / "old.sqlite3")
    async with aiosqlite.connect(old_path) as c:
        await c.execute(OLD_SCHEMA)
        # one row due for a ping right now, one not due yet but still needing
        # a calendar sync (calendar repair ignores due time entirely)
        await c.execute(
            "INSERT INTO reminders (title, due_at, status, calendar_pending, created_at) "
            "VALUES (?,?,?,0,?)",
            ("дело один", to_iso(real_now - timedelta(minutes=1)), "pending", to_iso(real_now)))
        await c.execute(
            "INSERT INTO reminders (title, due_at, status, calendar_pending, created_at) "
            "VALUES (?,?,?,1,?)",
            ("дело два", to_iso(real_now + timedelta(days=1)), "pending", to_iso(real_now)))
        await c.commit()

    db = Database(old_path)
    await db.init()
    cfg = Config(bot_token="t", her_user_id=100, admin_user_id=200, tick_seconds=0,
                 _env_file=None)
    deps = Deps(bot=FakeBot(), db=db, cal=FakeCalendar(), cfg=cfg)

    # Drive the real scheduler loop (not just one scheduler_tick call) against
    # it, same as the reviewer's repro.
    task = asyncio.create_task(flow.run_scheduler(deps))
    for _ in range(200):
        if deps.bot.sent:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)  # let a few more ticks pass before stopping
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    admin_alerts = [m for m in deps.bot.sent if m.chat_id == 200]
    assert admin_alerts == []  # no "no such column" alert storm
    her_pings = [m for m in deps.bot.sent if m.chat_id == 100]
    assert len(her_pings) == 1  # pinged once, not once per (sub-second) tick
    assert "дело один" in her_pings[0].text.lower()
    r2 = await db.get(2)
    assert r2.calendar_uid is not None and r2.calendar_pending == 0


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
    # FakeBot's default fail_message ("Forbidden: bot was blocked by the
    # user") is the classic permanent-unreachability wording, so this is
    # exercising the "she blocked the bot" path, not a network blip.
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


async def test_transient_ping_failure_does_not_expire_and_recovers(deps):
    # A network blip / rate limit / momentary 5xx must not count toward
    # max_delivery_failures the way a permanently blocked chat does: three
    # failed ticks (a few tick_seconds) must not be enough to give up on a
    # reminder that a short outage will let through moments later.
    await make(deps)
    deps.bot.fail_message = "Telegram server says - Bad Gateway"
    deps.bot.fail_chat_ids.add(deps.cfg.her_user_id)
    t = DUE
    for _ in range(deps.cfg.max_delivery_failures + 2):
        await scheduler_tick(deps, t)
        t += timedelta(seconds=deps.cfg.tick_seconds)
    r = await deps.db.get(1)
    assert r.status == "pending"  # not expired by a transient blip
    assert r.delivery_failures == 0  # transient failures don't spend the budget
    assert r.pings_sent == 0
    assert not any(m.chat_id == deps.cfg.admin_user_id for m in deps.bot.sent)
    deps.bot.fail_chat_ids.discard(deps.cfg.her_user_id)  # the blip passes
    await scheduler_tick(deps, t)
    assert (await deps.db.get(1)).pings_sent == 1  # now gets pinged normally


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


async def test_repeated_row_error_is_rate_limited_to_one_admin_alert_per_hour(deps):
    # A row that keeps raising an unexpected exception (not a handled
    # delivery/calendar failure) must still tell the admin, but not once per
    # tick forever -- it should be rate-limited like the outage alert is.
    await make(deps)
    await deps.db._exec("UPDATE reminders SET due_at=? WHERE id=?", ("not-a-date", 1))
    for _ in range(5):
        await scheduler_tick(deps, NOW)
    admin_alerts = [m for m in deps.bot.sent if m.chat_id == deps.cfg.admin_user_id]
    assert len(admin_alerts) == 1
    await scheduler_tick(deps, NOW + timedelta(hours=2))
    admin_alerts = [m for m in deps.bot.sent if m.chat_id == deps.cfg.admin_user_id]
    assert len(admin_alerts) == 2


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


async def test_outage_alert_recovers_and_can_fire_again_later(deps):
    # A plain "alert once until a success" latch has a gap: if the only
    # affected row gets cancelled and nothing else ever needs a calendar op,
    # there's no success left to clear it, and a brand new outage days later
    # would then never alert. The cooldown must let it fire again regardless.
    deps.cal.fail = True
    await process_dialog_message("напомни завтра в 17 дело один", deps, NOW)
    first_alerts = [m for m in deps.bot.sent if m.chat_id == deps.cfg.admin_user_id]
    assert len(first_alerts) == 1
    # She cancels the only affected row; nothing pending needs the calendar
    # again, so there is no successful op to reset cal_outage via a create.
    await on_callback("cancel:1", deps, NOW)
    assert await deps.db.get(1)
    assert (await deps.db.get(1)).status == "cancelled"
    # A genuinely new, unrelated outage a long time later.
    much_later = NOW + timedelta(minutes=deps.cfg.calendar_outage_alert_cooldown_minutes + 1)
    await process_dialog_message("напомни завтра в 17 дело два", deps, much_later)
    admin_alerts = [m for m in deps.bot.sent if m.chat_id == deps.cfg.admin_user_id]
    assert len(admin_alerts) == 2  # the new outage got its own alert


async def test_outage_recovery_resets_immediately_on_success(deps):
    deps.cal.fail = True
    await process_dialog_message("напомни завтра в 17 дело", deps, NOW)
    assert any(m.chat_id == deps.cfg.admin_user_id for m in deps.bot.sent)
    deps.bot.sent.clear()
    deps.cal.fail = False
    await scheduler_tick(deps, NOW + timedelta(minutes=1))  # calendar repair succeeds
    recovered = [m for m in deps.bot.sent if m.chat_id == deps.cfg.admin_user_id]
    assert len(recovered) == 1
    assert "снова доступен" in recovered[0].text.lower()
    # Immediately after recovering, a fresh failure alerts right away rather
    # than waiting out the cooldown.
    deps.bot.sent.clear()
    deps.cal.fail = True
    await process_dialog_message("напомни завтра в 18 другое дело", deps, NOW)
    assert any(m.chat_id == deps.cfg.admin_user_id for m in deps.bot.sent)


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
    try:
        # If an assertion below ever fails, the gate must still be released in
        # `finally` -- otherwise create_event's real OS thread stays blocked
        # on gate.wait() forever and interpreter shutdown hangs joining it.
        for _ in range(200):
            if deps.bot.sent:
                break
            await asyncio.sleep(0.01)
        assert len(deps.bot.sent) == 1
        assert "Напоминаю" in deps.bot.sent[0].text
    finally:
        deps.cal.gate.set()
    await asyncio.wait_for(task, timeout=2)


# --- a tick landing mid-create must not duplicate the calendar event (finding 3) ---


async def test_tick_during_create_does_not_duplicate_calendar_event(deps):
    deps.cal.gate = threading.Event()
    create_task = asyncio.create_task(
        process_dialog_message("напомни завтра в 17 сдать кровь", deps, NOW))
    tick_task = None
    try:
        # Same rationale as above: always release the gate, even if an
        # assertion or the polling loop itself fails, so create_event's
        # blocked thread can never hang the test process.
        for _ in range(200):
            if 1 in deps.cal_locks:
                break
            await asyncio.sleep(0.01)
        assert (await deps.db.get(1)).calendar_pending == 1  # create is mid-flight
        tick_task = asyncio.create_task(scheduler_tick(deps, NOW))
        await asyncio.sleep(0.05)  # let the tick reach (and block on) the same lock
    finally:
        deps.cal.gate.set()
    await create_task
    if tick_task is not None:
        await tick_task
    assert len(deps.cal.created) == 1
    r = await deps.db.get(1)
    assert r.calendar_uid == deps.cal.created[0][0]
    assert r.calendar_pending == 0


async def test_cancel_during_calendar_repair_does_not_create_an_orphaned_event(deps):
    # Reproduces the reviewer's finding: a row cancelled while the repair
    # pass is mid-flight for it must not end up with a fresh calendar event
    # that nothing will ever track or delete again (cancel only clears
    # status, never calendar_pending, so a repair racing it used to have no
    # way of knowing the row it was about to act on had just been cancelled).
    await make(deps)
    original_uid = (await deps.db.get(1)).calendar_uid
    await deps.db.set_calendar(1, original_uid, 1)  # force it to need a repair
    deps.cal.delete_gate = threading.Event()
    cancel_task = asyncio.create_task(on_callback("cancel:1", deps, NOW))
    tick_task = None
    try:
        # Wait until cancel is actually holding the row's lock, blocked
        # mid-delete, before starting a concurrent repair pass.
        for _ in range(200):
            lock = deps.cal_locks.get(1)
            if lock is not None and lock.locked():
                break
            await asyncio.sleep(0.01)
        assert deps.cal_locks[1].locked()
        tick_task = asyncio.create_task(scheduler_tick(deps, NOW))
        await asyncio.sleep(0.05)  # let the repair pass queue up on the same lock
    finally:
        deps.cal.delete_gate.set()
    await cancel_task
    if tick_task is not None:
        await tick_task
    r = await deps.db.get(1)
    assert r.status == "cancelled"
    assert deps.cal.deleted == [original_uid]  # only cancel's delete, nothing else
    assert deps.cal.created == []  # the repair must not have created a new event
