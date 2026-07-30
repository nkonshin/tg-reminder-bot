from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src import texts
from src.config import Config
from src.db import Database, from_iso
from src.flow import Deps, process_dialog_message
from tests.fakes import FakeBot, FakeCalendar

TZ = ZoneInfo("Asia/Yekaterinburg")
NOW = datetime(2026, 7, 24, 15, 0, tzinfo=TZ)


@pytest.fixture
async def deps(tmp_path):
    db = Database(str(tmp_path / "t.sqlite3"))
    await db.init()
    cfg = Config(bot_token="t", her_user_id=100, admin_user_id=200, _env_file=None)
    return Deps(bot=FakeBot(), db=db, cal=FakeCalendar(), cfg=cfg)


async def test_full_creation(deps):
    await process_dialog_message("напомни мне завтра вечером посмотреть анализы", deps, NOW)
    r = await deps.db.get(1)
    assert r.status == "pending"
    assert from_iso(r.due_at) == datetime(2026, 7, 25, 20, 0, tzinfo=TZ)
    assert r.calendar_uid == "uid-1" and r.calendar_pending == 0
    assert deps.cal.created[0][1] == "Посмотреть анализы"
    card = deps.bot.sent[0]
    assert card.chat_id == 100 and "Поставила напоминание" in card.text
    # The reminder side must never get a parse_mode: it interpolates
    # user-written titles into its cards, and titles containing '<' would
    # fail to send if this were sent as HTML. Only the monitor half opts in
    # to parse_mode="HTML", and only per call -- see src/monitor/capture.py.
    assert card.parse_mode is None


async def test_no_trigger_is_ignored(deps):
    await process_dialog_message("просто поболтать", deps, NOW)
    assert deps.bot.sent == [] and await deps.db.get(1) is None


async def test_no_time_creates_clarify(deps):
    await process_dialog_message("напомни про анализы", deps, NOW)
    r = await deps.db.get(1)
    assert r.status == "pending_clarify" and r.day == "2026-07-24"
    assert "когда?" in deps.bot.sent[0].text


async def test_empty_title_falls_back_to_default(deps):
    # The trigger stem plus the date/time phrase consume the whole message,
    # leaving no task description behind.
    await process_dialog_message("напомни завтра в 17", deps, NOW)
    r = await deps.db.get(1)
    assert r.status == "pending"
    assert r.title == texts.default_title()
    assert r.title != ""


async def test_calendar_failure_still_creates(deps):
    deps.cal.fail = True
    await process_dialog_message("напомни завтра в 17 сдать кровь", deps, NOW)
    r = await deps.db.get(1)
    assert r.status == "pending" and r.calendar_pending == 1 and r.calendar_uid is None
    chats = [m.chat_id for m in deps.bot.sent]
    assert 200 in chats  # алерт админу
    assert any("не получилось" in m.text for m in deps.bot.sent if m.chat_id == 100)


async def test_admin_alerted_when_confirmation_card_undeliverable(deps):
    # She never pressed /start (or blocked the bot): the reminder and its
    # calendar event must still be created, but nobody would otherwise know
    # she was never actually told about it.
    deps.bot.fail_chat_ids.add(deps.cfg.her_user_id)
    await process_dialog_message("напомни завтра в 17 сдать кровь", deps, NOW)
    r = await deps.db.get(1)
    assert r.status == "pending" and r.calendar_pending == 0
    admin_alerts = [m for m in deps.bot.sent if m.chat_id == deps.cfg.admin_user_id]
    assert len(admin_alerts) == 1
    assert "недоступна" in admin_alerts[0].text


async def test_title_is_capped_so_ping_cannot_exceed_telegram_limit(deps):
    # An uncapped title from a very long dictated message could make a
    # formatted ping/card exceed Telegram's 4096-char message limit, which
    # would then fail to send on every single attempt.
    long_desc = "слово " * 100  # ~600 chars, well past the cap
    await process_dialog_message(f"напомни завтра в 17 {long_desc}", deps, NOW)
    r = await deps.db.get(1)
    assert len(r.title) <= 200
    assert len(texts.ping_text(r.title)) < 4096


async def test_admin_alerted_when_clarify_card_undeliverable(deps):
    deps.bot.fail_chat_ids.add(deps.cfg.her_user_id)
    await process_dialog_message("напомни про анализы", deps, NOW)
    r = await deps.db.get(1)
    assert r.status == "pending_clarify"
    admin_alerts = [m for m in deps.bot.sent if m.chat_id == deps.cfg.admin_user_id]
    assert len(admin_alerts) == 1
    assert "недоступна" in admin_alerts[0].text
