"""Deleting an event must not go through get_event_by_uid.

iCloud answers caldav's search-by-UID REPORT with a hard 412 Precondition
Failed — reproduced 3/3 against the live account — so every «Отменить»,
«Изменить время» and snooze left its event behind in her calendar and woke
the admin with an alert. The event is addressed by its own URL instead.
"""
from types import SimpleNamespace

import pytest

from src import calendar_client
from src.calendar_client import CalendarClient
from src.config import Config

# The numeric segment is an Apple DSID — an account identifier. This repository
# is public, so it is zeroed rather than copied from a real calendar URL.
CAL_URL = "https://caldav.icloud.com/000000000/calendars/abc/"
UID = "5cdc7913-b39c-4b4a-9d9b-133d24cb18dc"


class FakeEvent:
    def __init__(self, data="", on_delete=None):
        self.data = data
        self.deleted = False
        self._on_delete = on_delete

    def delete(self):
        if self._on_delete:
            self._on_delete()
        self.deleted = True


class FakeCalendar:
    def __init__(self, listed=None, on_list=None):
        self.url = CAL_URL
        self.client = object()
        self._listed = listed or []
        self._on_list = on_list

    def events(self):
        if self._on_list:
            self._on_list()
        return self._listed


@pytest.fixture
def client(monkeypatch):
    cfg = Config(bot_token="t", her_user_id=1, admin_user_id=2, _env_file=None)
    return CalendarClient(cfg)


def _patch_direct(monkeypatch, event):
    """Make caldav.Event(...) return `event` and record the url it was built with."""
    seen = {}

    def fake_event(client, url):
        seen["url"] = url
        return event

    monkeypatch.setattr(calendar_client.caldav, "Event", fake_event)
    return seen


def test_delete_addresses_the_event_by_its_own_url(client, monkeypatch):
    cal = FakeCalendar()
    monkeypatch.setattr(client, "_calendar", lambda: cal)
    event = FakeEvent()
    seen = _patch_direct(monkeypatch, event)

    client.delete_event(UID)

    assert seen["url"] == f"{CAL_URL}{UID}.ics"
    assert event.deleted


def test_delete_never_searches_by_uid(client, monkeypatch):
    """The search REPORT is what iCloud rejects, so it must not be reachable."""
    def explode():
        raise AssertionError("get_event_by_uid must not be called")

    cal = FakeCalendar()
    cal.get_event_by_uid = lambda uid: explode()
    monkeypatch.setattr(client, "_calendar", lambda: cal)
    _patch_direct(monkeypatch, FakeEvent())

    client.delete_event(UID)


def test_delete_falls_back_to_listing_when_the_url_is_wrong(client, monkeypatch):
    """A server free to store the event elsewhere must not strand it."""
    ours = FakeEvent(data=f"BEGIN:VEVENT\nUID:{UID}\nEND:VEVENT")
    other = FakeEvent(data="BEGIN:VEVENT\nUID:someone-elses\nEND:VEVENT")
    cal = FakeCalendar(listed=[other, ours])
    monkeypatch.setattr(client, "_calendar", lambda: cal)

    def missing(client, url):
        return FakeEvent(on_delete=lambda: (_ for _ in ()).throw(RuntimeError("404")))

    monkeypatch.setattr(calendar_client.caldav, "Event", missing)

    client.delete_event(UID)

    assert ours.deleted
    assert not other.deleted


def test_delete_is_quiet_when_the_event_is_already_gone(client, monkeypatch):
    """She deletes it by hand in Calendar — that is not an admin-worthy error."""
    cal = FakeCalendar(listed=[FakeEvent(data="UID:unrelated")])
    monkeypatch.setattr(client, "_calendar", lambda: cal)

    def missing(client, url):
        return FakeEvent(on_delete=lambda: (_ for _ in ()).throw(RuntimeError("404")))

    monkeypatch.setattr(calendar_client.caldav, "Event", missing)

    client.delete_event(UID)  # must not raise


def test_delete_surfaces_a_real_failure(client, monkeypatch):
    """Credentials or network gone: the admin does need to hear about it."""
    def boom():
        raise RuntimeError("401 Unauthorized")

    cal = FakeCalendar(on_list=boom)
    monkeypatch.setattr(client, "_calendar", lambda: cal)

    def missing(client, url):
        return FakeEvent(on_delete=lambda: (_ for _ in ()).throw(RuntimeError("401")))

    monkeypatch.setattr(calendar_client.caldav, "Event", missing)

    with pytest.raises(RuntimeError):
        client.delete_event(UID)
