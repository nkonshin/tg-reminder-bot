import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, Message

from src import texts
from src.flow import Deps, now_local, on_callback, on_her_private_text, process_dialog_message

log = logging.getLogger(__name__)

router = Router(name="reminder-bot")


@router.business_message(F.text)
async def business(msg: Message, deps: Deps) -> None:
    """Messages in the watched Telegram Business dialog. Only hers are acted
    on — the owner writing "напомни" to her is talking to a person, not to the
    bot. Nothing is ever sent back into this dialog.

    Scope is normally enforced entirely on the Telegram side (Business ->
    Chatbots -> only this one dialog); dialog_chat_id is an optional extra
    check in code in case that toggle is ever misconfigured or she shares
    another chat with the business account."""
    if msg.from_user is None or msg.from_user.id != deps.cfg.her_user_id:
        return
    if deps.cfg.dialog_chat_id and msg.chat.id != deps.cfg.dialog_chat_id:
        return
    await process_dialog_message(msg.text, deps, now_local(deps.cfg))


@router.message(CommandStart(), F.chat.type == "private")
async def start(msg: Message, deps: Deps) -> None:
    if msg.from_user.id == deps.cfg.her_user_id:
        await msg.answer(texts.start_text())
    elif msg.from_user.id == deps.cfg.admin_user_id:
        await msg.answer(texts.start_admin_text())


@router.callback_query(F.data)
async def callbacks(cb: CallbackQuery, deps: Deps) -> None:
    if cb.from_user.id != deps.cfg.her_user_id:
        await cb.answer()
        return
    answer = await on_callback(cb.data, deps, now_local(deps.cfg))
    try:
        await cb.answer(answer or None)
    except TelegramBadRequest:
        # Telegram expires a callback query after ~15s. If on_callback spent
        # that long (a stalled CalDAV write is the realistic cause) the work
        # above is still done and persisted — only the toast is lost, so this
        # must not surface as an unhandled error.
        log.warning("callback query expired before it could be answered")


@router.message(F.chat.type == "private", F.text)
async def private_text(msg: Message, deps: Deps) -> None:
    """Her typed reply after "Напишу время". Anything else, including bot
    commands (e.g. "/help"), is ignored — only /start is handled elsewhere."""
    if msg.from_user.id != deps.cfg.her_user_id:
        return
    if msg.text.startswith("/"):
        return
    await on_her_private_text(msg.text, deps, now_local(deps.cfg))
