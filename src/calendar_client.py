import uuid
from datetime import datetime, timedelta, timezone

import caldav
from icalendar import Alarm, Calendar, Event


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

    def _calendar(self):
        if self._cal is not None:
            return self._cal
        client = caldav.DAVClient(url=self.cfg.caldav_url,
                                  username=self.cfg.apple_id,
                                  password=self.cfg.apple_app_password)
        if self.cfg.calendar_url:
            self._cal = caldav.Calendar(client=client, url=self.cfg.calendar_url)
            return self._cal
        for cal in client.principal().calendars():
            if (cal.name or "").strip().lower() == self.cfg.calendar_name.lower():
                self._cal = cal
                return self._cal
        raise LookupError(f"Calendar '{self.cfg.calendar_name}' not found")

    def create_event(self, title: str, start: datetime, end: datetime) -> str:
        uid = str(uuid.uuid4())
        self._calendar().add_event(build_event_ics(uid, title, start, end).decode())
        return uid

    def delete_event(self, uid: str) -> None:
        try:
            self._calendar().get_event_by_uid(uid).delete()
        except caldav.lib.error.NotFoundError:
            pass
