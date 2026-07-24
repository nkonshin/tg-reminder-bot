from datetime import datetime, timezone

import pytest

from src.db import Database, from_iso


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "t.sqlite3"))
    await d.init()
    return d

DUE = datetime(2026, 7, 25, 15, 0, tzinfo=timezone.utc)


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
    assert await db.get_awaiting_manual() is None
    await db.set_awaiting_manual(r.id)
    assert (await db.get_awaiting_manual()).id == r.id


async def test_set_awaiting_manual_clears_other_rows(db):
    r1 = await db.add_clarify("анализы", "2026-07-25")
    r2 = await db.add_clarify("другое", "2026-07-25")
    await db.set_awaiting_manual(r1.id)
    await db.set_awaiting_manual(r2.id)
    assert (await db.get(r1.id)).awaiting_manual_time == 0
    assert (await db.get(r2.id)).awaiting_manual_time == 1
    got = await db.get_awaiting_manual()
    assert got.id == r2.id


async def test_list_pending_and_ping(db):
    r = await db.add_pending("анализы", DUE)
    await db.add_clarify("другое", "2026-07-25")
    pend = await db.list_pending()
    assert [p.id for p in pend] == [r.id]
    await db.record_ping(r.id, DUE)
    assert (await db.get(r.id)).pings_sent == 1
