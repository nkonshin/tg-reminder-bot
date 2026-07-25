from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest

from src.db import Database, from_iso, to_iso


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "t.sqlite3"))
    await d.init()
    return d

DUE = datetime(2026, 7, 25, 15, 0, tzinfo=timezone.utc)
NOW = datetime(2026, 7, 24, 15, 0, tzinfo=timezone.utc)

# The schema as it existed before calendar_attempts / calendar_last_attempt_at
# / awaiting_manual_since / delivery_failures were added, to prove init()
# migrates an existing on-disk database rather than just creating new ones
# correctly. Deliberately hand-copied (not imported) so a future schema
# change can't accidentally "fix" this fixture along with it.
OLD_SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  day TEXT,
  due_at TEXT,
  status TEXT NOT NULL,
  calendar_uid TEXT,
  calendar_pending INTEGER NOT NULL DEFAULT 0,
  awaiting_manual_time INTEGER NOT NULL DEFAULT 0,
  pings_sent INTEGER NOT NULL DEFAULT 0,
  last_ping_at TEXT,
  created_at TEXT NOT NULL
);
"""


async def test_init_migrates_a_pre_backoff_database_in_place(tmp_path):
    path = str(tmp_path / "old.sqlite3")
    async with aiosqlite.connect(path) as c:
        await c.execute(OLD_SCHEMA)
        await c.execute(
            "INSERT INTO reminders (title, due_at, status, calendar_pending, created_at) "
            "VALUES (?,?,?,1,?)",
            ("старое напоминание", to_iso(DUE), "pending", to_iso(NOW)))
        await c.commit()

    d = Database(path)
    await d.init()  # must not raise, and must add the missing columns in place

    pending = await d.list_pending()
    assert len(pending) == 1
    r = pending[0]
    assert r.calendar_attempts == 0
    assert r.calendar_last_attempt_at is None
    assert r.awaiting_manual_since is None
    assert r.delivery_failures == 0

    # Exercise every new column's write path against the migrated row.
    await d.record_calendar_attempt(r.id, NOW)
    assert (await d.get(r.id)).calendar_attempts == 1
    assert await d.record_delivery_failure(r.id) == 1
    await d.record_ping(r.id, NOW)
    got = await d.get(r.id)
    assert got.delivery_failures == 0 and got.pings_sent == 1


async def test_init_is_a_noop_migration_on_a_database_already_current(db):
    # Running init() twice against an already-up-to-date file (e.g. a second
    # process start) must not error or touch existing data.
    r = await db.add_pending("анализы", DUE)
    await db.init()
    assert (await db.get(r.id)).title == "анализы"


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
