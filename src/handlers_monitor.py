import logging

from aiogram import F, Router
from aiogram.dispatcher.event.bases import skip
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import BusinessConnection, BusinessMessagesDeleted, CallbackQuery, Message

from src.flow import now_local
from src.monitor import admin, capture

log = logging.getLogger(__name__)

router = Router(name="monitor")


def _enabled(deps) -> bool:
    return bool(deps.cfg.monitor_enabled)


@router.business_connection()
async def connection(event: BusinessConnection, monitor: capture.MonitorDeps) -> None:
    if not _enabled(monitor):
        return
    try:
        await capture.on_business_connection(event, monitor, now_local(monitor.cfg))
    except Exception:
        log.exception("business_connection handling failed")


@router.business_message()
async def business_message(msg: Message, monitor: capture.MonitorDeps) -> None:
    """Runs alongside the reminder router's own business_message handler
    (src.handlers.business). aiogram's Dispatcher stops propagating an update
    to further sibling routers as soon as one router's handler matches and
    returns normally -- it does NOT give the update to every router that
    accepts it, whatever a matching filter-less handler here would otherwise
    suggest. Since this handler has no filter, it would match (and so
    swallow) every single business message, permanently silencing the
    reminder router's own business_message handler -- the trigger phrase
    matching ("напомни", ...) would never run again. Calling skip() always,
    regardless of outcome, tells aiogram "treat this as unhandled" so the
    event keeps propagating to the next router. See
    tests/test_monitor_handlers.py::test_business_message_does_not_block_the_reminder_router."""
    if _enabled(monitor):
        try:
            await capture.on_business_message(msg, monitor, now_local(monitor.cfg))
        except Exception:
            log.exception("recording a business message failed")
    skip()


@router.edited_business_message()
async def edited(msg: Message, monitor: capture.MonitorDeps) -> None:
    if not _enabled(monitor):
        return
    try:
        await capture.on_edited_business_message(msg, monitor, now_local(monitor.cfg))
    except Exception:
        log.exception("handling an edited business message failed")


@router.deleted_business_messages()
async def deleted(event: BusinessMessagesDeleted, monitor: capture.MonitorDeps) -> None:
    if not _enabled(monitor):
        return
    try:
        await capture.on_deleted_business_messages(event, monitor, now_local(monitor.cfg))
    except Exception:
        log.exception("handling deleted business messages failed")


@router.message(Command("admin"), F.chat.type == "private")
async def admin_command(msg: Message, monitor: capture.MonitorDeps) -> None:
    screen = await admin.open_panel(monitor, msg.from_user.id)
    if screen is None:
        return  # not the admin: stay silent
    text, kb = screen
    await msg.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("adm:"))
async def admin_callback(cb: CallbackQuery, monitor: capture.MonitorDeps) -> None:
    screen = await admin.handle_callback(cb.data, monitor, cb.from_user.id,
                                         now_local(monitor.cfg))
    try:
        await cb.answer()
    except TelegramBadRequest:
        log.warning("callback query expired before it could be answered")
    if screen is None:
        return
    text, kb = screen
    if not isinstance(cb.message, Message):
        # Telegram represents a callback on a message older than ~48h as an
        # InaccessibleMessage stub -- it has no edit_text at all (it doesn't
        # even subclass Message). The callback was already answered above;
        # there's simply nothing left here to redraw.
        return
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        # "message is not modified" when the screen is unchanged — harmless.
        pass
