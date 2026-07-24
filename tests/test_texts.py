from datetime import datetime
from zoneinfo import ZoneInfo

from src.texts import cap, created_text, format_dt, kb_created

TZ = ZoneInfo("Asia/Yekaterinburg")
NOW = datetime(2026, 7, 24, 15, 0, tzinfo=TZ)


def test_format_today_tomorrow_far():
    assert format_dt(NOW.replace(hour=20), NOW) == "сегодня, 20:00"
    assert format_dt(datetime(2026, 7, 25, 9, 0, tzinfo=TZ), NOW) == "завтра, 09:00"
    assert format_dt(datetime(2026, 7, 31, 14, 0, tzinfo=TZ), NOW) == "31 июля, 14:00"


def test_created_text_mentions_calendar_state():
    ok = created_text("посмотреть анализы", NOW.replace(hour=20), NOW, calendar_ok=True)
    fail = created_text("посмотреть анализы", NOW.replace(hour=20), NOW, calendar_ok=False)
    assert "календар" in ok.lower() and "не получилось" in fail.lower()


def test_kb_created_callbacks():
    kb = kb_created(7)
    datas = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert datas == ["cancel:7", "edit:7"]


def test_cap():
    assert cap("посмотреть анализы") == "Посмотреть анализы"
