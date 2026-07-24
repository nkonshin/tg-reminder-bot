from datetime import datetime, time
from zoneinfo import ZoneInfo

from src.config import Config
from src.parsing import extract_part_of_day, extract_relative, extract_time

TZ = ZoneInfo("Asia/Yekaterinburg")
NOW = datetime(2026, 7, 24, 15, 0, tzinfo=TZ)  # пятница
CFG = Config(bot_token="t", her_user_id=1, admin_user_id=2, _env_file=None)


def test_explicit_time_colon():
    t, spans = extract_time("завтра в 17:30 сдать кровь")
    assert t == time(17, 30) and len(spans) == 1


def test_time_evening_qualifier():
    t, _ = extract_time("в 9 вечера позвонить маме")
    assert t == time(21, 0)


def test_small_hour_defaults_to_pm():
    t, _ = extract_time("в 2 забрать посылку")
    assert t == time(14, 0)


def test_morning_hour_kept_as_is():
    t, _ = extract_time("в 8 разбудить")
    assert t == time(8, 0)


def test_no_time():
    t, spans = extract_time("посмотреть анализы")
    assert t is None and spans == []


def test_part_of_day_evening():
    t, spans = extract_part_of_day("вечером посмотреть анализы", CFG)
    assert t == time(20, 0) and len(spans) == 1


def test_part_of_day_multiword():
    t, _ = extract_part_of_day("после обеда позвонить врачу", CFG)
    assert t == time(14, 0)


def test_relative_hours():
    dt, spans = extract_relative("через 2 часа выключить духовку", NOW)
    assert dt == datetime(2026, 7, 24, 17, 0, tzinfo=TZ) and len(spans) == 1


def test_relative_half_hour():
    dt, _ = extract_relative("через полчаса снять белье", NOW)
    assert dt == datetime(2026, 7, 24, 15, 30, tzinfo=TZ)
