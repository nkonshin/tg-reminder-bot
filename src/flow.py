import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from src import texts
from src.config import Config
from src.db import Database, from_iso
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
    title = texts.cap(parsed.title) or texts.default_title()
    due = resolve(parsed, now)
    if due is None:
        r = await deps.db.add_clarify(title, parsed.day.isoformat())
        await deps.bot.send_message(deps.cfg.her_user_id, texts.clarify_text(title),
                                     reply_markup=texts.kb_clarify(r.id))
        return
    await create_reminder(title, due, deps, now)


async def _create_calendar_event(title: str, due: datetime, deps: Deps) -> tuple[str | None, int]:
    """Create a calendar event for (title, due). Returns (uid, calendar_pending);
    calendar_pending=1 means creation failed, the admin was alerted, and the
    scheduler is expected to retry later — callers must persist both via
    set_calendar and must not let this failure block whatever DB state change
    they're already making."""
    try:
        end = due + timedelta(minutes=deps.cfg.event_duration_minutes)
        uid = await asyncio.to_thread(deps.cal.create_event, title, due, end)
        return uid, 0
    except Exception as e:
        log.exception("calendar create failed")
        await notify_admin(deps, texts.calendar_failure_admin_text(title, e))
        return None, 1


async def create_reminder(title: str, due: datetime, deps: Deps, now: datetime,
                           rid: int | None = None) -> None:
    if rid is None:
        r = await deps.db.add_pending(title, due)
        rid = r.id
    else:
        await deps.db.promote(rid, due)
    uid, pending = await _create_calendar_event(title, due, deps)
    await deps.db.set_calendar(rid, uid, pending)
    await deps.bot.send_message(
        deps.cfg.her_user_id,
        texts.created_text(title, due.astimezone(local_tz(deps.cfg)), now, calendar_ok=pending == 0),
        reply_markup=texts.kb_created(rid))


# Which statuses a callback action is valid from. Anything else (including a
# missing row) is treated as a stale/no-longer-relevant button tap: her old
# Telegram messages never disappear, so a button from a previous state of the
# same reminder can still be tapped at any time. Acting on it anyway would
# corrupt the row (e.g. "snooze" promoting a clarify row with no calendar
# event, or "pod"/"manual" firing twice and orphaning the first calendar
# event), so we no-op instead.
ACTION_STATES = {
    "done": {"pending"},
    "cancel": {"pending", "pending_clarify"},
    "snooze": {"pending"},
    "edit": {"pending"},
    "pod": {"pending_clarify"},
    "manual": {"pending_clarify"},
}


async def _delete_event_if_any(r, deps: Deps) -> None:
    if r.calendar_uid:
        try:
            await asyncio.to_thread(deps.cal.delete_event, r.calendar_uid)
        except Exception as e:
            await notify_admin(deps, texts.calendar_delete_failure_admin_text(e))


def _clarify_day(r, deps: Deps, now: datetime) -> date:
    """Day to use when promoting a clarify row via a part-of-day button or an
    untyped manual time. Prefers the row's stored clarify day; falls back to
    the date of a previously scheduled due_at (an "edit" only clears the
    calendar/status, it keeps due_at until the row is repromoted) so that
    "Изменить время" changes the time without silently moving the reminder to
    today; falls back to today only for a brand-new clarify row."""
    if r.day:
        return date.fromisoformat(r.day)
    if r.due_at:
        return from_iso(r.due_at).astimezone(local_tz(deps.cfg)).date()
    return now.date()


async def on_callback(data: str, deps: Deps, now: datetime) -> str:
    parts = data.split(":")
    action, rid = parts[0], int(parts[1])
    r = await deps.db.get(rid)
    if r is None or r.status not in ACTION_STATES.get(action, set()):
        return texts.stale_callback_text()
    if action == "done":
        await deps.db.set_status(rid, "confirmed")
        return texts.toast_done()
    if action == "cancel":
        await _delete_event_if_any(r, deps)
        await deps.db.set_status(rid, "cancelled")
        return texts.toast_cancel()
    if action == "snooze":
        due = now + timedelta(minutes=deps.cfg.snooze_minutes)
        await deps.db.promote(rid, due)
        # The old event still shows the pre-snooze time, so it must be
        # re-pointed rather than left stale in her calendar. A CalDAV hiccup
        # here must not undo the reschedule — set_calendar always records
        # whatever _create_calendar_event managed (uid or a pending flag for
        # the scheduler to retry).
        await _delete_event_if_any(r, deps)
        uid, pending = await _create_calendar_event(r.title, due, deps)
        await deps.db.set_calendar(rid, uid, pending)
        return texts.toast_snooze()
    if action == "edit":
        await _delete_event_if_any(r, deps)
        await deps.db.set_calendar(rid, None, 0)
        await deps.db.set_status(rid, "pending_clarify")
        await deps.bot.send_message(deps.cfg.her_user_id, texts.clarify_text(r.title),
                                     reply_markup=texts.kb_clarify(rid))
        return texts.toast_edit()
    if action == "pod":
        hours = {"morning": deps.cfg.morning_hour, "day": deps.cfg.day_hour,
                 "evening": deps.cfg.evening_hour}
        day = _clarify_day(r, deps, now)
        due = datetime.combine(day, dtime(hours[parts[2]], 0), tzinfo=local_tz(deps.cfg))
        if due <= now:
            due += timedelta(days=1)
        await create_reminder(r.title, due, deps, now, rid=rid)
        return texts.toast_pod()
    # action == "manual"
    await deps.db.set_awaiting_manual(rid)
    await deps.bot.send_message(deps.cfg.her_user_id, texts.manual_time_prompt())
    return ""


async def _retry_calendar_event(r, deps: Deps) -> None:
    """Repair a reminder whose calendar event is missing: either creation
    failed earlier, or the process died between the INSERT and set_calendar.
    A stale uid is deleted first so a retry never orphans the old event."""
    await _delete_event_if_any(r, deps)
    uid, pending = await _create_calendar_event(r.title, from_iso(r.due_at), deps)
    await deps.db.set_calendar(r.id, uid, pending)


async def _ping(r, deps: Deps, now: datetime) -> None:
    await deps.bot.send_message(deps.cfg.her_user_id, texts.ping_text(r.title),
                                 reply_markup=texts.kb_ping(r.id))
    await deps.db.record_ping(r.id, now)


async def scheduler_tick(deps: Deps, now: datetime) -> None:
    for r in await deps.db.list_pending():
        if r.calendar_pending:
            await _retry_calendar_event(r, deps)
        if from_iso(r.due_at) > now:
            continue
        if r.pings_sent == 0:
            await _ping(r, deps, now)
            continue
        if now - from_iso(r.last_ping_at) < timedelta(minutes=deps.cfg.reping_minutes):
            continue
        if r.pings_sent >= 1 + deps.cfg.max_repings:
            await deps.db.set_status(r.id, "expired")
            await notify_admin(deps, texts.expired_admin_text(r.title, r.pings_sent))
        else:
            await _ping(r, deps, now)


async def run_scheduler(deps: Deps) -> None:
    """Poll loop: all state lives in SQLite, so a restart loses nothing and a
    failing tick must never kill the loop."""
    while True:
        try:
            await scheduler_tick(deps, now_local(deps.cfg))
        except Exception as e:
            log.exception("scheduler tick failed")
            await notify_admin(deps, texts.scheduler_failure_admin_text(e))
        await asyncio.sleep(deps.cfg.tick_seconds)


async def on_her_private_text(text: str, deps: Deps, now: datetime) -> None:
    r = await deps.db.get_awaiting_manual()
    if r is None:
        return
    parsed = parse_when(text.lower().replace("ё", "е"), now, deps.cfg)
    if parsed.at is None:
        await deps.bot.send_message(deps.cfg.her_user_id, texts.manual_time_error())
        return
    day = parsed.day if parsed.explicit_date else _clarify_day(r, deps, now)
    due = datetime.combine(day, parsed.at, tzinfo=local_tz(deps.cfg))
    if due <= now:
        due += timedelta(days=1)
    await create_reminder(r.title, due, deps, now, rid=r.id)
