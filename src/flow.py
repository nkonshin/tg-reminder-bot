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
    # Whether the CalDAV calendar is currently believed to be down, and when
    # the admin was last alerted about it. Together these keep an ongoing
    # outage from alerting once per pending row per tick while still letting
    # a *new* outage alert again after calendar_outage_alert_cooldown_minutes,
    # even with no successful calendar op in between to reset cal_outage
    # directly. See _alert_calendar_outage / _mark_calendar_ok.
    cal_outage: bool = False
    cal_last_alert_at: datetime | None = None
    # Per-reminder rate limit for "this row keeps raising an unexpected
    # exception in the scheduler" admin alerts — see _alert_row_error.
    row_error_alerts: dict = field(default_factory=dict)


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


# Telegram error text that indicates a chat is permanently unreachable, not
# just having a transient blip (network hiccup, rate limit, 5xx). Matched
# against str(exception) instead of aiogram's exception classes so this stays
# framework-agnostic (flow.py must not import aiogram) and is trivially
# driven from a plain-exception fake in tests. Anything that doesn't match is
# treated as transient by default: the safer failure mode is retrying a
# genuinely dead chat forever over the tick cadence, not expiring a real
# reminder because of a short-lived network blip.
_PERMANENT_FAILURE_MARKERS = (
    "bot was blocked",
    "user is deactivated",
    "chat not found",
    "kicked",
    "have no rights to send",
)


def _is_permanent_delivery_failure(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _PERMANENT_FAILURE_MARKERS)


async def _mark_calendar_ok(deps: Deps) -> None:
    """Clear the outage-alert state on any successful calendar operation
    (create or delete). Also tells the admin the outage is over, since the
    original "calendar down" alert otherwise has no matching "it's back"."""
    if deps.cal_outage:
        deps.cal_outage = False
        deps.cal_last_alert_at = None
        await notify_admin(deps, texts.calendar_recovered_admin_text())


async def _alert_calendar_outage(deps: Deps, now: datetime, text: str) -> None:
    """Alert the admin about a calendar failure, but not more than once per
    calendar_outage_alert_cooldown_minutes. A plain "alert once until a
    success" latch is not enough on its own: if nothing pending ever needs a
    calendar op again (e.g. she cancels the only affected row), cal_outage
    would stay stuck at True forever with no success left to clear it, and a
    genuinely new outage days later would then alert zero times. The cooldown
    means that case still recovers on its own once enough time has passed,
    while same-tick/same-outage failures across many rows still collapse to
    one alert."""
    if (deps.cal_outage and deps.cal_last_alert_at is not None
            and now - deps.cal_last_alert_at
            < timedelta(minutes=deps.cfg.calendar_outage_alert_cooldown_minutes)):
        return
    deps.cal_outage = True
    deps.cal_last_alert_at = now
    await notify_admin(deps, text)


async def _alert_row_error(deps: Deps, rid: int, now: datetime, text: str) -> None:
    """Rate-limited admin alert for a reminder that keeps raising an
    unexpected exception in the scheduler (see scheduler_tick). Without this,
    a persistent per-row bug is swallowed forever behind nothing but a
    container log line that eventually rotates out."""
    last = deps.row_error_alerts.get(rid)
    if (last is not None
            and now - last < timedelta(minutes=deps.cfg.row_error_alert_cooldown_minutes)):
        return
    deps.row_error_alerts[rid] = now
    await notify_admin(deps, text)


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


async def _create_calendar_event(title: str, due: datetime, deps: Deps,
                                  now: datetime) -> tuple[str | None, int]:
    """Create a calendar event for (title, due). Returns (uid, calendar_pending);
    calendar_pending=1 means creation failed and the scheduler is expected to
    retry later — callers must persist both via set_calendar and must not let
    this failure block whatever DB state change they're already making. The
    admin is alerted at most once per calendar_outage_alert_cooldown_minutes,
    not on every failed row on every tick — see _alert_calendar_outage."""
    try:
        end = due + timedelta(minutes=deps.cfg.event_duration_minutes)
        uid = await asyncio.to_thread(deps.cal.create_event, title, due, end)
        await _mark_calendar_ok(deps)
        return uid, 0
    except Exception as e:
        log.exception("calendar create failed")
        await _alert_calendar_outage(deps, now, texts.calendar_failure_admin_text(title, e))
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
            uid, pending = await _create_calendar_event(title, due, deps, now)
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
    if not r.calendar_uid:
        return
    try:
        await asyncio.to_thread(deps.cal.delete_event, r.calendar_uid)
        await _mark_calendar_ok(deps)
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
        # set_status runs inside the same lock as the delete so a concurrent
        # scheduler repair can never slip in between "event deleted" and
        # "row marked cancelled" and recreate an event nobody will ever clean
        # up again (_retry_calendar_event's fresh re-read now also checks
        # status, but closing the window here too removes the race outright).
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
                uid, pending = await _create_calendar_event(current.title, due, deps, now)
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
    retried every tick. The re-read under the lock checks status as well as
    calendar_pending: the row could have been cancelled (or edited/confirmed)
    by the time this repair actually gets the lock, in which case creating an
    event for it now would just orphan it -- nothing "pending" is left to
    ever track or delete that uid again."""
    if not _calendar_retry_due(r, deps.cfg, now):
        return
    async with _lock_for(deps, r.id):
        current = await deps.db.get(r.id)
        if current is None or not current.calendar_pending or current.status != "pending":
            return
        await deps.db.record_calendar_attempt(r.id, now)
        await _delete_event_if_any(current, deps)
        uid, pending = await _create_calendar_event(
            current.title, from_iso(current.due_at), deps, now)
        await deps.db.set_calendar(r.id, uid, pending)


async def _ping(r, deps: Deps, now: datetime) -> None:
    """Send a ping/reping. If delivery itself fails (not just "no tap yet"),
    that's tracked separately from pings_sent so a permanently unreachable
    chat eventually gives up instead of being retried by scheduler_tick
    forever — see record_delivery_failure and max_delivery_failures. Only
    failures classified as permanent (_is_permanent_delivery_failure) count
    toward that budget: a transient blip (network hiccup, rate limit, a
    momentary 5xx) must not burn through max_delivery_failures in a couple of
    tick_seconds-spaced ticks and expire a reminder that was never actually
    undeliverable. A transient failure is simply retried next tick, since
    pings_sent stays 0 either way."""
    try:
        await deps.bot.send_message(deps.cfg.her_user_id, texts.ping_text(r.title),
                                     reply_markup=texts.kb_ping(r.id))
    except Exception as e:
        log.exception("ping delivery failed for reminder %s", r.id)
        if not _is_permanent_delivery_failure(e):
            return
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
    already due. A row that keeps raising also gets a rate-limited admin
    alert (_alert_row_error) -- otherwise a persistent per-row bug is
    swallowed forever behind nothing but a container log line."""
    for r in await deps.db.list_pending():
        try:
            await _process_due_reminder(r, deps, now)
        except Exception as e:
            log.exception("scheduler tick: ping pass failed for reminder %s", r.id)
            await _alert_row_error(deps, r.id, now,
                                    texts.row_processing_failed_admin_text(r.id, r.title, e))
    for r in await deps.db.list_pending():
        if not r.calendar_pending:
            continue
        try:
            await _retry_calendar_event(r, deps, now)
        except Exception as e:
            log.exception("scheduler tick: calendar repair failed for reminder %s", r.id)
            await _alert_row_error(deps, r.id, now,
                                    texts.row_processing_failed_admin_text(r.id, r.title, e))


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
