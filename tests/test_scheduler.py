from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

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
