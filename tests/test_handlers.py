from types import SimpleNamespace

import pytest

from src.config import Config
from src.db import Database
from src.flow import Deps, now_local, on_callback, process_dialog_message
from src.handlers import business, private_text, router
from tests.fakes import FakeBot, FakeCalendar


def test_router_named_and_wired():
    assert router.name == "reminder-bot"
    assert router.business_message.handlers
    assert router.callback_query.handlers
    assert router.message.handlers


def test_entrypoint_imports():
    import src.main

    assert callable(src.main.main)


@pytest.fixture
async def deps(tmp_path):
    db = Database(str(tmp_path / "t.sqlite3"))
    await db.init()
    cfg = Config(bot_token="t", her_user_id=100, admin_user_id=200, _env_file=None)
    return Deps(bot=FakeBot(), db=db, cal=FakeCalendar(), cfg=cfg)


def _msg(user_id, text, chat_id=100):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        chat=SimpleNamespace(id=chat_id, type="private"),
        text=text)


# --- single-dialog scope enforced in code, not just via the Telegram toggle (finding 8) ---


async def test_business_ignored_when_dialog_chat_id_mismatches(deps):
    deps.cfg.dialog_chat_id = 999
    await business(_msg(100, "напомни завтра в 17 сдать кровь", chat_id=100), deps)
    assert await deps.db.get(1) is None


async def test_business_processed_when_dialog_chat_id_matches(deps):
    deps.cfg.dialog_chat_id = 100
    await business(_msg(100, "напомни завтра в 17 сдать кровь", chat_id=100), deps)
    assert await deps.db.get(1) is not None


async def test_business_processed_when_dialog_chat_id_unset(deps):
    assert deps.cfg.dialog_chat_id == 0
    await business(_msg(100, "напомни завтра в 17 сдать кровь", chat_id=999), deps)
    assert await deps.db.get(1) is not None


# --- private_text must not feed bot commands to the time parser (minor) ---


async def test_private_text_ignores_slash_commands(deps):
    await private_text(_msg(100, "/help"), deps)
    assert deps.bot.sent == []


async def test_private_text_ignores_slash_commands_even_when_awaiting_manual(deps):
    now = now_local(deps.cfg)
    await process_dialog_message("напомни про анализы", deps, now)
    await on_callback("manual:1", deps, now)
    deps.bot.sent.clear()
    await private_text(_msg(100, "/help"), deps)
    r = await deps.db.get(1)
    assert r.status == "pending_clarify"
    assert deps.bot.sent == []
