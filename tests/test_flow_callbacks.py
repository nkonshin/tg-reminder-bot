from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from src import texts
from src.config import Config
from src.db import Database, from_iso
from src.flow import Deps, on_callback, on_her_private_text, process_dialog_message
from tests.fakes import FakeBot, FakeCalendar

TZ = ZoneInfo("Asia/Yekaterinburg")
NOW = datetime(2026, 7, 24, 15, 0, tzinfo=TZ)


@pytest.fixture
async def deps(tmp_path):
    db = Database(str(tmp_path / "t.sqlite3"))
    await db.init()
    cfg = Config(bot_token="t", her_user_id=100, admin_user_id=200, _env_file=None)
    return Deps(bot=FakeBot(), db=db, cal=FakeCalendar(), cfg=cfg)


async def make_pending(deps):
    await process_dialog_message("напомни завтра в 17 сдать кровь", deps, NOW)
    return await deps.db.get(1)


async def test_done(deps):
    await make_pending(deps)
    await on_callback("done:1", deps, NOW)
    assert (await deps.db.get(1)).status == "confirmed"


async def test_cancel_removes_calendar_event(deps):
    r = await make_pending(deps)
    await on_callback("cancel:1", deps, NOW)
    assert (await deps.db.get(1)).status == "cancelled"
    assert deps.cal.deleted == [r.calendar_uid]


async def test_snooze_shifts_due(deps):
    await make_pending(deps)
    await on_callback("snooze:1", deps, NOW)
    r = await deps.db.get(1)
    assert from_iso(r.due_at) == NOW.astimezone(timezone.utc) + timedelta(hours=1)
    assert r.pings_sent == 0


async def test_pod_choice_promotes_clarify(deps):
    await process_dialog_message("напомни завтра про анализы", deps, NOW)
    await on_callback("pod:1:evening", deps, NOW)
    r = await deps.db.get(1)
    assert r.status == "pending"
    assert from_iso(r.due_at) == datetime(2026, 7, 25, 20, 0, tzinfo=TZ)
    assert len(deps.cal.created) == 1


async def test_manual_time_flow(deps):
    await process_dialog_message("напомни про анализы", deps, NOW)
    await on_callback("manual:1", deps, NOW)
    assert (await deps.db.get_awaiting_manual()).id == 1
    await on_her_private_text("завтра в 18", deps, NOW)
    r = await deps.db.get(1)
    assert r.status == "pending"
    assert from_iso(r.due_at) == datetime(2026, 7, 25, 18, 0, tzinfo=TZ)


async def test_manual_time_unparsed_reasks(deps):
    await process_dialog_message("напомни про анализы", deps, NOW)
    await on_callback("manual:1", deps, NOW)
    await on_her_private_text("ну когда-нибудь", deps, NOW)
    assert (await deps.db.get(1)).status == "pending_clarify"
    assert "Не поняла" in deps.bot.sent[-1].text


async def test_edit_reopens_clarify_and_removes_event(deps):
    r = await make_pending(deps)
    await on_callback("edit:1", deps, NOW)
    got = await deps.db.get(1)
    assert got.status == "pending_clarify"
    assert deps.cal.deleted == [r.calendar_uid]


# --- additional coverage: bugs found in the reference implementation ---


async def test_callback_on_terminal_state_is_safe_noop(deps):
    await make_pending(deps)
    await on_callback("done:1", deps, NOW)
    toast = await on_callback("done:1", deps, NOW)
    assert (await deps.db.get(1)).status == "confirmed"
    assert toast == texts.stale_callback_text()


async def test_stale_snooze_after_edit_does_not_corrupt_state(deps):
    # A ping message with "Отложить на час" can still be sitting in her chat
    # after she separately taps "Изменить время" on the creation card. That
    # stale tap must not silently promote a clarify row to "pending" with no
    # calendar event.
    await make_pending(deps)
    await on_callback("edit:1", deps, NOW)
    toast = await on_callback("snooze:1", deps, NOW)
    r = await deps.db.get(1)
    assert r.status == "pending_clarify"
    assert toast == texts.stale_callback_text()


async def test_stale_pod_on_already_pending_does_not_orphan_calendar_event(deps):
    # Tapping an old "Утром/Днём/Вечером" button a second time (e.g. double
    # tap, or a stale message from before the row was already promoted) must
    # not create a second calendar event and lose track of the first one.
    await process_dialog_message("напомни завтра про анализы", deps, NOW)
    await on_callback("pod:1:evening", deps, NOW)
    toast = await on_callback("pod:1:morning", deps, NOW)
    assert len(deps.cal.created) == 1
    assert toast == texts.stale_callback_text()


async def test_pod_after_edit_keeps_original_day(deps):
    # "Изменить время" is meant to change the TIME, not silently move the
    # reminder to today. The row's `day` column stays empty after edit (it
    # was created via add_pending, not add_clarify), so the fallback must use
    # the previously scheduled due_at's date, not "now".
    edit_now = datetime(2026, 7, 24, 7, 0, tzinfo=TZ)
    await process_dialog_message("напомни завтра в 17 сдать кровь", deps, edit_now)
    await on_callback("edit:1", deps, edit_now)
    await on_callback("pod:1:morning", deps, edit_now)
    r = await deps.db.get(1)
    assert r.status == "pending"
    assert from_iso(r.due_at) == datetime(2026, 7, 25, 9, 0, tzinfo=TZ)


async def test_manual_after_edit_keeps_original_day(deps):
    edit_now = datetime(2026, 7, 24, 7, 0, tzinfo=TZ)
    await process_dialog_message("напомни завтра в 17 сдать кровь", deps, edit_now)
    await on_callback("edit:1", deps, edit_now)
    await on_callback("manual:1", deps, edit_now)
    await on_her_private_text("в 9", deps, edit_now)
    r = await deps.db.get(1)
    assert r.status == "pending"
    assert from_iso(r.due_at) == datetime(2026, 7, 25, 9, 0, tzinfo=TZ)
