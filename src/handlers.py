from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, Message

from src import texts
from src.flow import Deps, now_local, on_callback, on_her_private_text, process_dialog_message

router = Router(name="reminder-bot")


@router.business_message(F.text)
async def business(msg: Message, deps: Deps) -> None:
    """Messages in the watched Telegram Business dialog. Only hers are acted
    on — the owner writing "напомни" to her is talking to a person, not to the
    bot. Nothing is ever sent back into this dialog."""
    if msg.from_user is None or msg.from_user.id != deps.cfg.her_user_id:
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
    await cb.answer(answer or None)


@router.message(F.chat.type == "private", F.text)
async def private_text(msg: Message, deps: Deps) -> None:
    """Her typed reply after "Напишу время". Anything else is ignored."""
    if msg.from_user.id != deps.cfg.her_user_id:
        return
    await on_her_private_text(msg.text, deps, now_local(deps.cfg))
