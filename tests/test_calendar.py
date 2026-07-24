from datetime import datetime, timezone

from src.calendar_client import build_event_ics


def test_ics_contains_event_and_alarm():
    start = datetime(2026, 7, 25, 15, 0, tzinfo=timezone.utc)
    end = datetime(2026, 7, 25, 16, 0, tzinfo=timezone.utc)
    ics = build_event_ics("uid-1", "Посмотреть анализы", start, end).decode()
    assert "BEGIN:VEVENT" in ics and "BEGIN:VALARM" in ics
    assert "UID:uid-1" in ics
    assert "SUMMARY:Посмотреть анализы" in ics
    assert "DTSTART:20260725T150000Z" in ics
