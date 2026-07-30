import logging
from types import SimpleNamespace

import pytest
from aiogram import Dispatcher
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.types import Chat, InaccessibleMessage

from src.handlers_monitor import router as monitor_router
from src.handlers import router as reminder_router


def _fake_callback(data, user_id):
    async def _answer(*_a, **_kw):
        return None

    async def _edit_text(*_a, **_kw):
        return None

    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user_id),
        message=SimpleNamespace(edit_text=_edit_text),
        answer=_answer,
    )


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


async def test_a_failing_monitor_does_not_escape_into_aiogram(monkeypatch, caplog):
    """The reminder scheduler shares this process: a monitor crash must be
    swallowed and LOGGED, never propagated out of the handler as anything
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
    with caplog.at_level(logging.ERROR, logger="src.handlers_monitor"):
        with pytest.raises(SkipHandler):
            await handlers_monitor.business_message(SimpleNamespace(), monitor)
    assert any("recording a business message failed" in r.message for r in caplog.records), (
        "a swallowed monitor failure must still leave a trace -- this is a background "
        "journal nobody watches interactively, so a silently eaten exception is invisible"
    )


@pytest.mark.parametrize("monitor_enabled", [True, False])
async def test_business_message_does_not_block_the_reminder_router(
        wired_dispatcher, monkeypatch, monitor_enabled):
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
    wired in.

    Parametrized over monitor_enabled: skip() sits OUTSIDE the `if
    _enabled(monitor):` guard in handlers_monitor.business_message, an
    unusual shape versus its three sibling handlers (which all early-return
    when disabled). Config defaults monitor_enabled to True, so without this
    parametrization the disabled branch -- every MONITOR_ENABLED=false
    deployment -- is never exercised, and "normalising" the handler to match
    its siblings would silently kill the reminder trigger whenever the
    monitor is switched off."""
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

    cfg = Config(bot_token="t", her_user_id=100, admin_user_id=200,
                 monitor_enabled=monitor_enabled, _env_file=None)
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

    assert monitor_called is monitor_enabled
    assert reminder_called, "the reminder router must still see her business messages"


async def test_adm_callback_reaches_only_the_admin_panel(wired_dispatcher, monkeypatch):
    """Rule 1 from the brief: an adm: callback must be swallowed by the
    monitor router and never reach the reminder router's own callback
    handler (which would otherwise answer with the "stale button" toast for
    her, since it has no idea what an adm: callback is)."""
    from src import handlers, handlers_monitor
    from src.config import Config
    from src.flow import Deps

    admin_called = reminder_called = False

    async def fake_admin_cb(data, monitor, uid, now):
        nonlocal admin_called
        admin_called = True
        return None

    async def fake_on_callback(data, deps, now):
        nonlocal reminder_called
        reminder_called = True
        return ""

    monkeypatch.setattr(handlers_monitor.admin, "handle_callback", fake_admin_cb)
    monkeypatch.setattr(handlers, "on_callback", fake_on_callback)

    cfg = Config(bot_token="t", her_user_id=100, admin_user_id=200, _env_file=None)
    deps = Deps(bot=SimpleNamespace(), db=SimpleNamespace(), cal=SimpleNamespace(), cfg=cfg)
    monitor = SimpleNamespace(cfg=cfg)

    cb = _fake_callback("adm:main", cfg.admin_user_id)
    await wired_dispatcher.propagate_event("callback_query", cb, deps=deps, monitor=monitor)

    assert admin_called
    assert not reminder_called


async def test_non_adm_callback_reaches_only_the_reminder_router(wired_dispatcher, monkeypatch):
    """The other half of rule 1: a plain reminder callback (e.g. from a
    snooze button) must not be intercepted by the monitor router. If the
    F.data.startswith("adm:") filter were ever removed from admin_callback,
    this would fail because the monitor's now-filterless handler would
    swallow it first."""
    from src import handlers, handlers_monitor
    from src.config import Config
    from src.flow import Deps

    admin_called = reminder_called = False

    async def fake_admin_cb(data, monitor, uid, now):
        nonlocal admin_called
        admin_called = True
        return None

    async def fake_on_callback(data, deps, now):
        nonlocal reminder_called
        reminder_called = True
        return ""

    monkeypatch.setattr(handlers_monitor.admin, "handle_callback", fake_admin_cb)
    monkeypatch.setattr(handlers, "on_callback", fake_on_callback)

    cfg = Config(bot_token="t", her_user_id=100, admin_user_id=200, _env_file=None)
    deps = Deps(bot=SimpleNamespace(), db=SimpleNamespace(), cal=SimpleNamespace(), cfg=cfg)
    monitor = SimpleNamespace(cfg=cfg)

    cb = _fake_callback("snooze:1", cfg.her_user_id)
    await wired_dispatcher.propagate_event("callback_query", cb, deps=deps, monitor=monitor)

    assert not admin_called
    assert reminder_called


async def test_admin_callback_survives_an_inaccessible_message(monkeypatch):
    """Telegram represents a callback on a message older than ~48h as an
    InaccessibleMessage stub, which has no edit_text (it doesn't even
    subclass Message). admin_callback must not blow up trying to redraw a
    screen onto one of those."""
    from src import handlers_monitor
    from src.config import Config

    async def fake_handle_callback(data, monitor, uid, now):
        return "some screen text", None

    async def _answer(*_a, **_kw):
        return None

    monkeypatch.setattr(handlers_monitor.admin, "handle_callback", fake_handle_callback)
    cfg = Config(bot_token="t", her_user_id=100, admin_user_id=200, _env_file=None)
    monitor = SimpleNamespace(cfg=cfg)
    cb = SimpleNamespace(
        data="adm:main",
        from_user=SimpleNamespace(id=cfg.admin_user_id),
        message=InaccessibleMessage(chat=Chat(id=1, type="private"), message_id=1, date=0),
        answer=_answer,
    )

    await handlers_monitor.admin_callback(cb, monitor)  # must not raise


async def test_a_failing_admin_callback_answers_the_button_and_is_logged(monkeypatch, caplog):
    """admin_command and admin_callback were the only handlers here with no
    isolation. Worse, the screen was computed BEFORE cb.answer(), so a failure
    escaped into aiogram with the callback still unanswered and the admin's
    button spinning until Telegram timed it out."""
    from src import handlers_monitor
    from src.config import Config

    async def boom(*_a, **_kw):
        raise RuntimeError("the panel is broken")

    answered = []

    async def _answer(*_a, **_kw):
        answered.append(True)

    monkeypatch.setattr(handlers_monitor.admin, "handle_callback", boom)
    cfg = Config(bot_token="t", her_user_id=100, admin_user_id=200, _env_file=None)
    monitor = SimpleNamespace(cfg=cfg)
    cb = SimpleNamespace(data="adm:main", from_user=SimpleNamespace(id=200),
                         message=SimpleNamespace(), answer=_answer)

    with caplog.at_level(logging.ERROR, logger="src.handlers_monitor"):
        await handlers_monitor.admin_callback(cb, monitor)  # must not raise

    assert answered, "the button must stop spinning even when the screen fails"
    assert any("building an admin screen failed" in r.message for r in caplog.records)


async def test_a_failing_admin_command_does_not_escape_into_aiogram(monkeypatch, caplog):
    from src import handlers_monitor
    from src.config import Config

    async def boom(*_a, **_kw):
        raise RuntimeError("the panel is broken")

    monkeypatch.setattr(handlers_monitor.admin, "open_panel", boom)
    cfg = Config(bot_token="t", her_user_id=100, admin_user_id=200, _env_file=None)
    monitor = SimpleNamespace(cfg=cfg)
    msg = SimpleNamespace(from_user=SimpleNamespace(id=200))

    with caplog.at_level(logging.ERROR, logger="src.handlers_monitor"):
        await handlers_monitor.admin_command(msg, monitor)  # must not raise

    assert any("opening the admin panel failed" in r.message for r in caplog.records)


def test_business_update_types_are_requested(wired_dispatcher):
    used = wired_dispatcher.resolve_used_update_types()
    for needed in ("business_connection", "business_message",
                   "edited_business_message", "deleted_business_messages"):
        assert needed in used, needed


async def test_sweeper_runs_only_when_the_monitor_is_enabled(monkeypatch):
    """The retention sweeper deletes rows and unlinks media files past their
    retention window. If it kept running while cfg.monitor_enabled is False,
    "switch the monitor off" would not actually freeze the archive -- it
    would keep quietly pruning it in the background."""
    import asyncio as asyncio_module

    from src import main as main_module
    from src.config import Config

    async def fake_forever(*_a, **_kw):
        await asyncio_module.Event().wait()

    monkeypatch.setattr(main_module, "run_scheduler", fake_forever)
    monkeypatch.setattr(main_module, "run_sweeper", fake_forever)

    async def _for(monitor_enabled):
        cfg = Config(bot_token="t", her_user_id=100, admin_user_id=200,
                     monitor_enabled=monitor_enabled, _env_file=None)
        tasks = main_module._start_background_tasks(
            deps=SimpleNamespace(), monitor=SimpleNamespace(), cfg=cfg)
        try:
            return len(tasks)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio_module.gather(*tasks, return_exceptions=True)

    assert await _for(monitor_enabled=True) == 2   # scheduler + sweeper
    assert await _for(monitor_enabled=False) == 1  # scheduler only
