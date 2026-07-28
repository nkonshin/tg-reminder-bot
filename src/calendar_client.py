import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone

import caldav
from caldav.lib.error import NotFoundError
from icalendar import Alarm, Calendar, Event

log = logging.getLogger(__name__)


def build_event_ics(uid: str, title: str, start: datetime, end: datetime) -> bytes:
    cal = Calendar()
    cal.add("prodid", "-//tg-reminder-bot//RU")
    cal.add("version", "2.0")
    ev = Event()
    ev.add("uid", uid)
    ev.add("summary", title)
    ev.add("dtstart", start)
    ev.add("dtend", end)
    ev.add("dtstamp", datetime.now(timezone.utc))
    alarm = Alarm()
    alarm.add("action", "DISPLAY")
    alarm.add("description", title)
    alarm.add("trigger", timedelta(0))
    ev.add_component(alarm)
    cal.add_component(ev)
    return cal.to_ical()


class CalendarClient:
    """Synchronous CalDAV client. Flow code must call these via asyncio.to_thread."""

    def __init__(self, cfg):
        self.cfg = cfg
        self._cal = None
        self._lock = threading.Lock()

    def _calendar(self):
        # create_event/delete_event run under asyncio.to_thread, so two real
        # threads can race here; the lock makes calendar discovery run once.
        with self._lock:
            if self._cal is not None:
                return self._cal
            client = caldav.DAVClient(url=self.cfg.caldav_url,
                                      username=self.cfg.apple_id,
                                      password=self.cfg.apple_app_password,
                                      timeout=self.cfg.caldav_timeout_seconds)
            if self.cfg.calendar_url:
                self._cal = caldav.Calendar(client=client, url=self.cfg.calendar_url)
                return self._cal
            for cal in client.principal().calendars():
                if (cal.name or "").strip().lower() == self.cfg.calendar_name.lower():
                    self._cal = cal
                    return self._cal
            raise LookupError(f"Calendar '{self.cfg.calendar_name}' not found")

    def _reconnect(self) -> None:
        """Drop the cached calendar so the next _calendar() dials afresh."""
        with self._lock:
            self._cal = None

    def _retrying(self, op):
        """Run op(), and on failure reconnect and run it once more.

        caldav pools HTTP keep-alive sockets. iCloud drops idle ones without
        telling us, so the first call after a quiet spell writes into a dead
        socket and hangs until the read timeout instead of failing fast — the
        one failure mode observed in practice. The retry goes out over a new
        connection, which costs ~0.1s. NotFoundError is a real answer, not a
        transport failure, so it is never retried."""
        try:
            return op()
        except NotFoundError:
            raise
        except Exception:
            log.warning("caldav call failed, reconnecting and retrying once",
                        exc_info=True)
            self._reconnect()
            return op()

    def create_event(self, title: str, start: datetime, end: datetime) -> str:
        uid = str(uuid.uuid4())
        ics = build_event_ics(uid, title, start, end).decode()
        self._retrying(lambda: self._calendar().add_event(ics))
        return uid

    def _event_url(self, uid: str) -> str:
        """Where caldav puts an event it creates: <calendar>/<uid>.ics."""
        return f"{str(self._calendar().url).rstrip('/')}/{uid}.ics"

    def _find_by_listing(self, uid: str):
        """Last resort if the event does not sit at its expected URL. events()
        returns the calendar data inline, so this is one request, not one per
        event."""
        for event in self._calendar().events():
            if uid in (event.data or ""):
                return event
        return None

    def delete_event(self, uid: str) -> None:
        """Delete by the event's own URL.

        Never search by UID: caldav implements that as a calendar-query REPORT,
        and iCloud rejects it with 412 Precondition Failed every single time
        (reproduced 3/3 against the live account). That silently left the old
        event in her calendar on every «Отменить»/«Изменить время»/snooze and
        alerted the admin instead."""
        try:
            self._retrying(lambda: caldav.Event(client=self._calendar().client,
                                                url=self._event_url(uid)).delete())
            return
        except NotFoundError:
            return  # уже удалено — удалять нечего
        except Exception as e:
            log.info("delete by url failed (%s), looking the event up in the calendar",
                     type(e).__name__)
        event = self._retrying(lambda: self._find_by_listing(uid))
        if event is None:
            return  # события в календаре нет: скорее всего, удалила вручную
        self._retrying(event.delete)
