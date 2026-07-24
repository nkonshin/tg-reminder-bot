import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from src import texts
from src.config import Config
from src.db import Database
from src.parsing import parse_when, resolve
from src.trigger import match_trigger

log = logging.getLogger(__name__)


@dataclass
class Deps:
    bot: object
    db: Database
    cal: object
    cfg: Config


def local_tz(cfg: Config) -> ZoneInfo:
    return ZoneInfo(cfg.timezone)


def now_local(cfg: Config) -> datetime:
    return datetime.now(local_tz(cfg))


async def notify_admin(deps: Deps, text: str) -> None:
    try:
        await deps.bot.send_message(deps.cfg.admin_user_id, f"[reminder-bot] {text}")
    except Exception:
        log.exception("cannot notify admin")


async def process_dialog_message(text: str, deps: Deps, now: datetime) -> None:
    cleaned = match_trigger(text, deps.cfg.stems)
    if cleaned is None:
        return
    parsed = parse_when(cleaned, now, deps.cfg)
    title = texts.cap(parsed.title) or "Напоминание"
    due = resolve(parsed, now)
    if due is None:
        r = await deps.db.add_clarify(title, parsed.day.isoformat())
        await deps.bot.send_message(deps.cfg.her_user_id, texts.clarify_text(title),
                                     reply_markup=texts.kb_clarify(r.id))
        return
    await create_reminder(title, due, deps, now)


async def create_reminder(title: str, due: datetime, deps: Deps, now: datetime,
                           rid: int | None = None) -> None:
    if rid is None:
        r = await deps.db.add_pending(title, due)
        rid = r.id
    else:
        await deps.db.promote(rid, due)
    uid, pending = None, 1
    try:
        end = due + timedelta(minutes=deps.cfg.event_duration_minutes)
        uid = await asyncio.to_thread(deps.cal.create_event, title, due, end)
        pending = 0
    except Exception as e:
        log.exception("calendar create failed")
        await notify_admin(deps, f"Календарь недоступен, событие «{title}» не создано: {e}")
    await deps.db.set_calendar(rid, uid, pending)
    await deps.bot.send_message(
        deps.cfg.her_user_id,
        texts.created_text(title, due.astimezone(local_tz(deps.cfg)), now, calendar_ok=pending == 0),
        reply_markup=texts.kb_created(rid))
