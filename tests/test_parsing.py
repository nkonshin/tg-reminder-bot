from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from src.config import Config
from src.parsing import (
    ParsedWhen,
    _extract_date,
    extract_part_of_day,
    extract_relative,
    extract_time,
    parse_when,
    resolve,
)

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


def test_small_hour_with_explicit_minutes_is_morning():
    # Реальный случай: «напомни взять экг завтра в 6.40» — имелось в виду утро.
    # Названные минуты означают, что время продиктовано точно, поэтому
    # догадка «маленький час = день» здесь не применяется.
    t, _ = extract_time("завтра в 6.40 взять экг")
    assert t == time(6, 40)


def test_small_hour_with_explicit_minutes_colon_is_morning():
    t, _ = extract_time("в 6:40 взять экг")
    assert t == time(6, 40)


def test_small_hour_with_zero_minutes_is_morning():
    t, _ = extract_time("в 5:00 выехать")
    assert t == time(5, 0)


def test_small_hour_with_minutes_and_evening_qualifier_is_pm():
    t, _ = extract_time("в 6.40 вечера забрать заказ")
    assert t == time(18, 40)


def test_no_time():
    t, spans = extract_time("посмотреть анализы")
    assert t is None and spans == []


# «на 8 утра» и «на завтра» — то, что она реально писала в ответ на переспрос;
# предлог «на» не понимался, и бот отвечал «не поняла время».
def test_na_preposition_with_qualifier():
    t, _ = extract_time("на 8 утра отправить доки")
    assert t == time(8, 0)


def test_na_preposition_with_explicit_minutes():
    t, _ = extract_time("на 18:30 позвонить")
    assert t == time(18, 30)


def test_qualifier_without_any_preposition():
    t, _ = extract_time("8 утра отправить доки")
    assert t == time(8, 0)


def test_bare_number_without_preposition_is_not_a_time():
    t, spans = extract_time("купить 5 яблок")
    assert t is None and spans == []


def test_na_with_days_is_a_duration_not_a_time():
    # «уехать на 3 дня» — это срок, а не 15:00
    t, spans = extract_time("уехать на 3 дня")
    assert t is None and spans == []


def test_midnight_with_night_qualifier():
    t, _ = extract_time("в 12 ночи принять таблетку")
    assert t == time(0, 0)


def test_small_hour_with_night_qualifier_kept():
    t, _ = extract_time("в 1 ночи выключить свет")
    assert t == time(1, 0)


def test_late_hour_with_night_qualifier_is_pm():
    t, _ = extract_time("в 10 ночи выключить свет")
    assert t == time(22, 0)


def test_counting_phrase_is_not_a_time():
    t, spans = extract_time("в 5 подъездов")
    assert t is None and spans == []


def test_counting_phrase_plural_prepositional():
    t, _ = extract_time("в 3 магазинах")
    assert t is None


def test_counting_phrase_minutes_walk():
    t, _ = extract_time("в 20 минутах ходьбы")
    assert t is None


def test_bare_hour_with_verb_still_parses():
    t, _ = extract_time("в 5 позвонить маме")
    assert t == time(17, 0)


def test_chasov_suffix_is_still_a_time():
    # "часов" ends in "-ов" too, but TIME_RE's own "час..." group consumes the
    # whole word as part of the match, so the counting-noun check (which only
    # looks at the word *after* the match) never sees it.
    t, _ = extract_time("в 5 часов позвонить")
    assert t == time(17, 0)


def test_part_of_day_evening():
    t, spans = extract_part_of_day("вечером посмотреть анализы", CFG)
    assert t == time(20, 0) and len(spans) == 1


def test_part_of_day_multiword():
    t, _ = extract_part_of_day("после обеда позвонить врачу", CFG)
    assert t == time(14, 0)


def test_part_of_day_not_matched_inside_word():
    t, spans = extract_part_of_day("сесть на заднем сиденье", CFG)
    assert t is None and spans == []


def test_relative_hours():
    dt, spans = extract_relative("через 2 часа выключить духовку", NOW)
    assert dt == datetime(2026, 7, 24, 17, 0, tzinfo=TZ) and len(spans) == 1


def test_relative_half_hour():
    dt, _ = extract_relative("через полчаса снять белье", NOW)
    assert dt == datetime(2026, 7, 24, 15, 30, tzinfo=TZ)


def test_tomorrow_evening():
    p = parse_when("завтра вечером посмотреть анализы", NOW, CFG)
    assert (p.day, p.at, p.explicit_date) == (NOW.date() + timedelta(days=1), time(20, 0), True)
    assert p.title == "посмотреть анализы"
    assert resolve(p, NOW) == datetime(2026, 7, 25, 20, 0, tzinfo=TZ)


def test_evening_today_rolls_when_passed():
    p = parse_when("вечером выпить таблетки", NOW.replace(hour=21), CFG)
    assert resolve(p, NOW.replace(hour=21)) == datetime(2026, 7, 25, 20, 0, tzinfo=TZ)


def test_weekday_is_next_strictly():
    p = parse_when("в пятницу утром сдать отчет", NOW, CFG)  # NOW — пятница
    assert p.day == NOW.date() + timedelta(days=7)
    assert p.title == "сдать отчет"


def test_dd_mm():
    p = parse_when("31.07 оплатить курс", NOW, CFG)
    assert p.day == date(2026, 7, 31) and p.at is None
    assert resolve(p, NOW) is None  # даты мало — нужен переспрос времени


def test_day_month_name():
    p = parse_when("25 июля в 12 забрать платье", NOW, CFG)
    assert p.day == date(2026, 7, 25) and p.at == time(12, 0)


def test_no_time_at_all_needs_clarify():
    p = parse_when("посмотреть анализы", NOW, CFG)
    assert p.at is None and p.day == NOW.date() and p.explicit_date is False
    assert resolve(p, NOW) is None


def test_explicit_past_needs_clarify():
    p = parse_when("сегодня в 9 утра принять таблетку", NOW, CFG)  # уже 15:00
    assert resolve(p, NOW) is None


def test_relative_sets_everything():
    p = parse_when("через 2 часа выключить духовку", NOW, CFG)
    assert resolve(p, NOW) == NOW + timedelta(hours=2)
    assert p.title == "выключить духовку"


# --- bugs found in the reference implementation while implementing this task ---


def test_breakfast_word_not_mistaken_for_tomorrow():
    # "завтра" (tomorrow) is a substring of "позавтракать" (to have breakfast).
    # A plain substring search for "завтра" would wrongly treat this as an
    # explicit date and mutilate the title down to "покать".
    p = parse_when("позавтракать в 9 утра", NOW, CFG)
    assert p.day == NOW.date() and p.explicit_date is False
    assert p.title == "позавтракать"


def test_dotted_time_not_mistaken_for_date():
    # "в 6.05" is a time written with a dot (common in ru: "17.30" for 5:30pm),
    # but "6.05" also parses as a valid dd.mm date (6 May). The date regex must
    # not re-claim digits already consumed by the time match. Named minutes
    # also mean the hour is taken literally, so this is 06:05, not 18:05.
    p = parse_when("в 6.05 разбудить", NOW, CFG)
    assert p.at == time(6, 5)
    assert p.day == NOW.date() and p.explicit_date is False
    assert p.title == "разбудить"


def test_invalid_calendar_date_does_not_crash():
    # "31.04" (April has 30 days) must not raise ValueError from date();
    # it should simply fail to match as a date.
    assert _extract_date("31.04 сходить к врачу", date(2026, 7, 24)) == (None, [])


# --- fix round: Feb-29 rollover crash + dangling qualifier word in title ---


def test_day_month_feb29_rollover_does_not_crash():
    # 2024 is a leap year, so "29 февраля" parses fine as this year's date, but
    # by 2024-03-01 it's already in the past and rolls to 2025 — not a leap
    # year, so date.replace(year=2025) on Feb 29 raises ValueError unless guarded.
    d, spans = _extract_date("29 февраля отметить праздник", date(2024, 3, 1))
    assert d == date(2025, 2, 28)
    assert spans


def test_ddmm_feb29_rollover_does_not_crash():
    d, spans = _extract_date("29.02 отметить праздник", date(2024, 3, 1))
    assert d == date(2025, 2, 28)
    assert spans


def test_next_weekday_qualifier_masculine_cleans_title():
    p = parse_when("в следующий вторник сдать отчет", NOW, CFG)  # NOW — пятница
    assert p.day == NOW.date() + timedelta(days=4)  # ближайший вторник впереди
    assert p.title == "сдать отчет"


def test_next_weekday_qualifier_feminine_cleans_title():
    p = parse_when("в следующую пятницу заплатить за интернет", NOW, CFG)  # NOW — пятница
    assert p.day == NOW.date() + timedelta(days=7)  # сегодня тоже пятница — строго вперед
    assert p.title == "заплатить за интернет"


# --- Дашин фидбэк: «понедельник 12:20» без «в» перед временем не срабатывал ---


def test_colon_time_without_preposition_is_a_time():
    # «12:20» с двоеточием — однозначно время, предлог не нужен. Раньше правило
    # «голое число — не время» отбрасывало его наравне с «купить 5 яблок».
    t, spans = extract_time("ирина швея понедельник 12:20")
    assert t == time(12, 20) and len(spans) == 1


def test_dotted_time_without_preposition_stays_a_date():
    # Точка — запись даты («12.08» = 12 августа), а не время; без предлога
    # временем она по-прежнему не считается, иначе сломались бы даты.
    t, spans = extract_time("12.08 позвонить")
    assert t is None and spans == []


def test_weekday_and_colon_time_without_any_preposition():
    # Ровно фраза Даши: без «в» ни перед днём недели, ни перед временем.
    # Должна разбираться так же, как сработавшее «10 августа в 12:20».
    p = parse_when("напомни ирина швея понедельник 12:20", NOW, CFG)  # NOW — пятница
    assert p.at == time(12, 20)
    assert p.day == date(2026, 7, 27)  # ближайший понедельник впереди
    assert resolve(p, NOW) == datetime(2026, 7, 27, 12, 20, tzinfo=TZ)
    assert "ирина швея" in p.title and "12:20" not in p.title


def test_explicit_date_and_colon_time_without_preposition():
    # «10 августа 12:20» — явная дата плюс двоеточное время, оба без предлога.
    p = parse_when("напомни ирина швея 10 августа 12:20", NOW, CFG)
    assert p.at == time(12, 20) and p.day == date(2026, 8, 10)
    assert resolve(p, NOW) == datetime(2026, 8, 10, 12, 20, tzinfo=TZ)
