from datetime import datetime, timedelta, timezone

import pytest

from src.db import Database, from_iso


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "t.sqlite3"))
    await d.init()
    return d

DUE = datetime(2026, 7, 25, 15, 0, tzinfo=timezone.utc)
NOW = datetime(2026, 7, 24, 15, 0, tzinfo=timezone.utc)


async def test_add_pending_roundtrip(db):
    r = await db.add_pending("анализы", DUE)
    got = await db.get(r.id)
    assert got.status == "pending" and from_iso(got.due_at) == DUE


async def test_clarify_then_promote(db):
    r = await db.add_clarify("анализы", "2026-07-25")
    assert (await db.get(r.id)).status == "pending_clarify"
    await db.promote(r.id, DUE)
    got = await db.get(r.id)
    assert got.status == "pending" and got.pings_sent == 0 and got.awaiting_manual_time == 0


async def test_awaiting_manual_flow(db):
    r = await db.add_clarify("анализы", "2026-07-25")
    assert await db.get_awaiting_manual(NOW, 30) is None
    await db.set_awaiting_manual(r.id, NOW)
    assert (await db.get_awaiting_manual(NOW, 30)).id == r.id


async def test_set_awaiting_manual_clears_other_rows(db):
    r1 = await db.add_clarify("анализы", "2026-07-25")
    r2 = await db.add_clarify("другое", "2026-07-25")
    await db.set_awaiting_manual(r1.id, NOW)
    await db.set_awaiting_manual(r2.id, NOW)
    assert (await db.get(r1.id)).awaiting_manual_time == 0
    assert (await db.get(r2.id)).awaiting_manual_time == 1
    got = await db.get_awaiting_manual(NOW, 30)
    assert got.id == r2.id


async def test_awaiting_manual_expires_after_timeout(db):
    r = await db.add_clarify("анализы", "2026-07-25")
    await db.set_awaiting_manual(r.id, NOW)
    just_inside = NOW + timedelta(minutes=29)
    just_outside = NOW + timedelta(minutes=31)
    assert (await db.get_awaiting_manual(just_inside, 30)).id == r.id
    assert await db.get_awaiting_manual(just_outside, 30) is None


async def test_add_clarify_clears_stale_awaiting_manual(db):
    # A brand new clarify card supersedes an older "Напишу время" flag: her
    # next free-text reply should not be hijacked by an abandoned reminder.
    r1 = await db.add_clarify("анализы", "2026-07-25")
    await db.set_awaiting_manual(r1.id, NOW)
    await db.add_clarify("другое", "2026-07-25")
    assert (await db.get(r1.id)).awaiting_manual_time == 0
    assert await db.get_awaiting_manual(NOW, 30) is None


async def test_calendar_attempt_backoff_bookkeeping(db):
    r = await db.add_pending("анализы", DUE)
    await db.record_calendar_attempt(r.id, NOW)
    got = await db.get(r.id)
    assert got.calendar_attempts == 1
    assert from_iso(got.calendar_last_attempt_at) == NOW
    await db.record_calendar_attempt(r.id, NOW + timedelta(minutes=5))
    assert (await db.get(r.id)).calendar_attempts == 2
    # A successful sync clears the backoff state entirely.
    await db.set_calendar(r.id, "uid-1", 0)
    got = await db.get(r.id)
    assert got.calendar_attempts == 0
    assert got.calendar_last_attempt_at is None


async def test_delivery_failure_bookkeeping(db):
    r = await db.add_pending("анализы", DUE)
    assert await db.record_delivery_failure(r.id) == 1
    assert await db.record_delivery_failure(r.id) == 2
    assert (await db.get(r.id)).delivery_failures == 2
    # A successful ping resets the streak.
    await db.record_ping(r.id, NOW)
    assert (await db.get(r.id)).delivery_failures == 0


async def test_list_pending_and_ping(db):
    r = await db.add_pending("анализы", DUE)
    await db.add_clarify("другое", "2026-07-25")
    pend = await db.list_pending()
    assert [p.id for p in pend] == [r.id]
    await db.record_ping(r.id, DUE)
    assert (await db.get(r.id)).pings_sent == 1
