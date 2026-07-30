from types import SimpleNamespace

import pytest
from aiogram import Dispatcher
from aiogram.dispatcher.event.bases import SkipHandler

from src.handlers_monitor import router as monitor_router
from src.handlers import router as reminder_router


@pytest.fixture(scope="module")
def wired_dispatcher():
    """Built once and shared: an aiogram Router can only ever be attached to
    one parent, so every test that needs both routers wired together (in the
    real order, monitor first) has to reuse the same Dispatcher instead of
    calling include_router again."""
    dp = Dispatcher()
    dp.include_router(monitor_router)
    dp.include_router(reminder_router)
    return dp


def test_router_is_named_and_wired():
    assert monitor_router.name == "monitor"
    assert monitor_router.business_connection.handlers
    assert monitor_router.edited_business_message.handlers
    assert monitor_router.deleted_business_messages.handlers
    assert monitor_router.callback_query.handlers
    assert monitor_router.message.handlers


async def test_a_failing_monitor_does_not_escape_into_aiogram(monkeypatch):
    """The reminder scheduler shares this process: a monitor crash must be
    swallowed and logged, never propagated out of the handler as anything
    other than aiogram's own SkipHandler control-flow signal -- see
    test_business_message_does_not_block_the_reminder_router for why that
    signal has to be raised at all."""
    from src import handlers_monitor
    from src.config import Config

    async def boom(*_a, **_kw):
        raise RuntimeError("monitor is broken")

    monkeypatch.setattr(handlers_monitor.capture, "on_business_message", boom)
    cfg = Config(bot_token="t", her_user_id=100, admin_user_id=200, _env_file=None)
    monitor = SimpleNamespace(cfg=cfg)
    with pytest.raises(SkipHandler):
        await handlers_monitor.business_message(SimpleNamespace(), monitor)


async def test_business_message_does_not_block_the_reminder_router(wired_dispatcher, monkeypatch):
    """Regression test for a real routing bug: the monitor router is included
    BEFORE the reminder router (required so it can filter adm: callbacks
    first -- see test_business_update_types_are_requested), and its
    business_message handler carries no filter of its own, so it matches
    every single business message.

    aiogram's Dispatcher stops propagating an update to further sibling
    routers as soon as one router's handler matches and returns normally --
    it does NOT hand the same update to every router willing to accept it.
    Without an explicit skip() in the monitor's handler, this would silently
    swallow every business message and the reminder router's own trigger
    matching (e.g. "напомни ...") would never run again once this feature is
    wired in."""
    from src import handlers, handlers_monitor
    from src.config import Config
    from src.flow import Deps

    monitor_called = reminder_called = False

    async def fake_capture(msg, monitor, now):
        nonlocal monitor_called
        monitor_called = True

    async def fake_process(text, deps, now):
        nonlocal reminder_called
        reminder_called = True

    monkeypatch.setattr(handlers_monitor.capture, "on_business_message", fake_capture)
    monkeypatch.setattr(handlers, "process_dialog_message", fake_process)

    cfg = Config(bot_token="t", her_user_id=100, admin_user_id=200, _env_file=None)
    deps = Deps(bot=SimpleNamespace(), db=SimpleNamespace(), cal=SimpleNamespace(), cfg=cfg)
    monitor = SimpleNamespace(cfg=cfg)

    msg = SimpleNamespace(
        text="напомни купить молоко",
        caption=None,
        from_user=SimpleNamespace(id=cfg.her_user_id, full_name="Her"),
        chat=SimpleNamespace(id=0, type="private"),
        business_connection_id="conn1",
    )

    await wired_dispatcher.propagate_event("business_message", msg, deps=deps, monitor=monitor)

    assert monitor_called, "the monitor should still have captured the message"
    assert reminder_called, "the reminder router must still see her business messages"


def test_business_update_types_are_requested(wired_dispatcher):
    used = wired_dispatcher.resolve_used_update_types()
    for needed in ("business_connection", "business_message",
                   "edited_business_message", "deleted_business_messages"):
        assert needed in used, needed
