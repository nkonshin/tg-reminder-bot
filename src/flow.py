import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from src import texts
from src.config import Config
from src.db import Database, from_iso
from src.parsing import parse_when, resolve
from src.trigger import match_trigger

log = logging.getLogger(__name__)

# A ping/creation card carries the full reminder title; Telegram messages cap
# out at 4096 chars, so an unbounded title (e.g. a very long dictated message)
# could make a formatted card exceed the limit and fail to send every time.
MAX_TITLE_LENGTH = 200


@dataclass
class Deps:
    bot: object
    db: Database
    cal: object
    cfg: Config
    # In-process only, intentionally not persisted: the whole deployment is
    # one asyncio process (one docker-compose service, no replicas), so a
    # per-reminder lock here is enough to fully serialize concurrent calendar
    # writers for the same row. See _lock_for / create_reminder.
    cal_locks: dict = field(default_factory=dict)
    # Whether the most recent calendar attempt (any row) failed. Used to
    # alert the admin only on the up -> down transition instead of once per
    # pending row on every tick during an outage. See _create_calendar_event.
    cal_outage: bool = False


def local_tz(cfg: Config) -> ZoneInfo:
    return ZoneInfo(cfg.timezone)


def now_local(cfg: Config) -> datetime:
    return datetime.now(local_tz(cfg))


async def notify_admin(deps: Deps, text: str) -> None:
    try:
        await deps.bot.send_message(deps.cfg.admin_user_id, f"[reminder-bot] {text}")
    except Exception:
        log.exception("cannot notify admin")


async def _send_her(deps: Deps, text: str, reply_markup, what: str) -> None:
    """Send a message to her private chat. If it's unreachable (she never
    pressed /start, blocked the bot, deactivated the account) the failure
    must not be silent: alert the admin instead. `what` is a short Russian
    description of the message for the admin alert."""
    try:
        await deps.bot.send_message(deps.cfg.her_user_id, text, reply_markup=reply_markup)
    except Exception as e:
        log.exception("cannot deliver message to her")
        await notify_admin(deps, texts.her_unreachable_admin_text(what, e))


def _lock_for(deps: Deps, rid: int) -> asyncio.Lock:
    lock = deps.cal_locks.get(rid)
    if lock is None:
        lock = deps.cal_locks[rid] = asyncio.Lock()
    return lock


async def process_dialog_message(text: str, deps: Deps, now: datetime) -> None:
    cleaned = match_trigger(text, deps.cfg.stems)
    if cleaned is None:
        return
    parsed = parse_when(cleaned, now, deps.cfg)
    title = texts.cap(parsed.title[:MAX_TITLE_LENGTH]) or texts.default_title()
    due = resolve(parsed, now)
    if due is None:
        r = await deps.db.add_clarify(title, parsed.day.isoformat())
        await _send_her(deps, texts.clarify_text(title), texts.kb_clarify(r.id),
                         f"переспрос про «{title}»")
        return
    await create_reminder(title, due, deps, now)


async def _create_calendar_event(title: str, due: datetime, deps: Deps) -> tuple[str | None, int]:
    """Create a calendar event for (title, due). Returns (uid, calendar_pending);
    calendar_pending=1 means creation failed and the scheduler is expected to
    retry later — callers must persist both via set_calendar and must not let
    this failure block whatever DB state change they're already making. The
    admin is alerted only when the outage starts, not on every failed row on
    every tick — see Deps.cal_outage."""
    try:
        end = due + timedelta(minutes=deps.cfg.event_duration_minutes)
        uid = await asyncio.to_thread(deps.cal.create_event, title, due, end)
        deps.cal_outage = False
        return uid, 0
    except Exception as e:
        log.exception("calendar create failed")
        if not deps.cal_outage:
            deps.cal_outage = True
            await notify_admin(deps, texts.calendar_failure_admin_text(title, e))
        return None, 1


async def create_reminder(title: str, due: datetime, deps: Deps, now: datetime,
                           rid: int | None = None) -> None:
    if rid is None:
        r = await deps.db.add_pending(title, due)
        rid = r.id
    else:
        await deps.db.promote(rid, due)
    # A scheduler tick can land while the CalDAV round-trip below is still in
    # flight: it sees calendar_pending=1 the instant add_pending/promote
    # commits (regardless of due time) and may start its own repair for the
    # same row concurrently. The lock serializes the two; re-reading the row
    # under it means whichever caller runs second sees calendar_pending
    # already cleared and skips, so at most one calendar event is ever
    # created for a given create_reminder call.
    async with _lock_for(deps, rid):
        row = await deps.db.get(rid)
        pending = row.calendar_pending if row else 0
        if row is not None and row.calendar_pending:
            uid, pending = await _create_calendar_event(title, due, deps)
            await deps.db.set_calendar(rid, uid, pending)
    await _send_her(
        deps,
        texts.created_text(title, due.astimezone(local_tz(deps.cfg)), now, calendar_ok=pending == 0),
        texts.kb_created(rid),
        f"карточку о напоминании «{title}»")


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
        async with _lock_for(deps, rid):
            current = await deps.db.get(rid) or r
            await _delete_event_if_any(current, deps)
        await deps.db.set_status(rid, "cancelled")
        return texts.toast_cancel()
    if action == "snooze":
        due = now + timedelta(minutes=deps.cfg.snooze_minutes)
        await deps.db.promote(rid, due)
        # The old event still shows the pre-snooze time, so it must be
        # re-pointed rather than left stale in her calendar. Same race as
        # create_reminder can happen here (a concurrent scheduler repair),
        # so the same lock + re-read-under-lock pattern applies: `current` is
        # fetched fresh rather than reusing the `r` snapshot from above, so
        # whichever event is actually tracked right now is the one deleted.
        async with _lock_for(deps, rid):
            current = await deps.db.get(rid)
            if current is not None and current.calendar_pending:
                await _delete_event_if_any(current, deps)
                uid, pending = await _create_calendar_event(current.title, due, deps)
                await deps.db.set_calendar(rid, uid, pending)
        return texts.toast_snooze()
    if action == "edit":
        async with _lock_for(deps, rid):
            current = await deps.db.get(rid) or r
            await _delete_event_if_any(current, deps)
            await deps.db.set_calendar(rid, None, 0)
        await deps.db.set_status(rid, "pending_clarify")
        await deps.db.clear_awaiting_manual()
        await _send_her(deps, texts.clarify_text(r.title), texts.kb_clarify(rid),
                         f"переспрос про «{r.title}»")
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
    await deps.db.set_awaiting_manual(rid, now)
    await _send_her(deps, texts.manual_time_prompt(r.title), None,
                     f"запрос времени для «{r.title}»")
    return ""


def _calendar_retry_due(r, cfg: Config, now: datetime) -> bool:
    """Backoff gate for the scheduler's calendar-repair pass: a dead CalDAV
    endpoint must not be hammered every tick forever. calendar_attempts
    starts at 0 (no retry made yet for the current due time), so the first
    repair always runs right away; each further failed retry doubles the
    wait before the next one, capped at calendar_retry_max_minutes."""
    if r.calendar_attempts <= 0 or not r.calendar_last_attempt_at:
        return True
    delay = min(cfg.calendar_retry_max_minutes,
                cfg.calendar_retry_base_minutes * (2 ** (r.calendar_attempts - 1)))
    return now - from_iso(r.calendar_last_attempt_at) >= timedelta(minutes=delay)


async def _retry_calendar_event(r, deps: Deps, now: datetime) -> None:
    """Repair a reminder whose calendar event is missing: either creation
    failed earlier, or the process died between the INSERT and set_calendar.
    Guarded by the same per-row lock as create_reminder/snooze so a repair
    can never race a create that is still in flight for the same row, and
    gated by _calendar_retry_due so a dead server is backed off instead of
    retried every tick."""
    if not _calendar_retry_due(r, deps.cfg, now):
        return
    async with _lock_for(deps, r.id):
        current = await deps.db.get(r.id)
        if current is None or not current.calendar_pending:
            return
        await deps.db.record_calendar_attempt(r.id, now)
        await _delete_event_if_any(current, deps)
        uid, pending = await _create_calendar_event(current.title, from_iso(current.due_at), deps)
        await deps.db.set_calendar(r.id, uid, pending)


async def _ping(r, deps: Deps, now: datetime) -> None:
    """Send a ping/reping. If delivery itself fails (not just "no tap yet"),
    that's tracked separately from pings_sent so a permanently unreachable
    chat eventually gives up instead of being retried by scheduler_tick
    forever — see record_delivery_failure and max_delivery_failures."""
    try:
        await deps.bot.send_message(deps.cfg.her_user_id, texts.ping_text(r.title),
                                     reply_markup=texts.kb_ping(r.id))
    except Exception:
        log.exception("ping delivery failed for reminder %s", r.id)
        failures = await deps.db.record_delivery_failure(r.id)
        if failures >= deps.cfg.max_delivery_failures:
            await deps.db.set_status(r.id, "expired")
            await notify_admin(deps, texts.undeliverable_admin_text(r.title, failures))
        return
    await deps.db.record_ping(r.id, now)


async def _process_due_reminder(r, deps: Deps, now: datetime) -> None:
    if from_iso(r.due_at) > now:
        return
    if r.pings_sent == 0:
        await _ping(r, deps, now)
        return
    if now - from_iso(r.last_ping_at) < timedelta(minutes=deps.cfg.reping_minutes):
        return
    if r.pings_sent >= 1 + deps.cfg.max_repings:
        await deps.db.set_status(r.id, "expired")
        await notify_admin(deps, texts.expired_admin_text(r.title, r.pings_sent))
    else:
        await _ping(r, deps, now)


async def scheduler_tick(deps: Deps, now: datetime) -> None:
    """A single bad row (raising delivery, an unexpected DB hiccup, ...) must
    never stop the rest of the pending reminders from being processed, so
    each row's work is isolated in its own try/except. Pings run in their own
    full pass before calendar repair starts, so a hanging/dead CalDAV
    endpoint (see calendar_client.py's timeout) never delays a ping that is
    already due."""
    for r in await deps.db.list_pending():
        try:
            await _process_due_reminder(r, deps, now)
        except Exception:
            log.exception("scheduler tick: ping pass failed for reminder %s", r.id)
    for r in await deps.db.list_pending():
        if not r.calendar_pending:
            continue
        try:
            await _retry_calendar_event(r, deps, now)
        except Exception:
            log.exception("scheduler tick: calendar repair failed for reminder %s", r.id)


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
    r = await deps.db.get_awaiting_manual(now, deps.cfg.manual_time_timeout_minutes)
    if r is None:
        return
    parsed = parse_when(text.lower().replace("ё", "е"), now, deps.cfg)
    if parsed.at is None:
        await _send_her(deps, texts.manual_time_error(), None, "сообщение о нераспознанном времени")
        return
    day = parsed.day if parsed.explicit_date else _clarify_day(r, deps, now)
    due = datetime.combine(day, parsed.at, tzinfo=local_tz(deps.cfg))
    if due <= now:
        due += timedelta(days=1)
    await create_reminder(r.title, due, deps, now, rid=r.id)
