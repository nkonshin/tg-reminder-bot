import re
import warnings
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from dateparser.search import search_dates

Span = tuple[int, int]

TIME_RE = re.compile(
    r"\b(?:в|к)\s+(\d{1,2})(?:[:.](\d{2}))?(?:\s*час(?:а|ов|ик(?:а|ов)?)?)?"
    r"(?:\s+(утра|дня|вечера|ночи))?\b"
)

# genitive/prepositional-plural noun endings — "в 5 подъездов"/"в 3 магазинах" are counts,
# not clock times; deliberately narrow (no full morphology), see extract_time
COUNTED_NOUN_SUFFIXES = ("ами", "ями", "ов", "ев", "ах", "ях")

PART_OF_DAY = [  # (фраза, ключ) — длинные фразы раньше коротких
    ("после обеда", "day"), ("в обед", "day"), ("с утра", "morning"),
    ("к вечеру", "evening"), ("утречком", "morning"), ("вечерком", "evening"),
    ("утром", "morning"), ("днем", "day"), ("вечером", "evening"),
]

REL_RE = re.compile(r"\bчерез\s+(?:(полчаса)|(час(?:ик)?)|(\d+)\s*(минут\w*|час\w*))\b")

TOMORROW_PLUS2_RE = re.compile(r"\bпослезавтра\b")
TOMORROW_RE = re.compile(r"\bзавтра\b")
TODAY_RE = re.compile(r"\bсегодня\b")
WEEKDAY_RE = re.compile(
    r"\bв[оа]?\s+(понедельник|вторник|среду|четверг|пятницу|субботу|воскресенье)\b"
)
WEEKDAYS = {"понедельник": 0, "вторник": 1, "среду": 2, "четверг": 3,
            "пятницу": 4, "субботу": 5, "воскресенье": 6}
DDMM_RE = re.compile(r"\b(\d{1,2})[./](\d{1,2})\b")
MONTHS = {"января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
          "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12}
DAY_MONTH_RE = re.compile(r"\b(\d{1,2})\s+(" + "|".join(MONTHS) + r")\b")


def _next_word(text: str, pos: int) -> str:
    m = re.match(r"\s*(\w+)", text[pos:])
    return m.group(1) if m else ""


def extract_time(text: str) -> tuple[time | None, list[Span]]:
    m = TIME_RE.search(text)
    if not m:
        return None, []
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    qual = m.group(3)
    if hour > 23 or minute > 59:
        return None, []
    if qual is None and _next_word(text, m.end()).endswith(COUNTED_NOUN_SUFFIXES):
        return None, []  # «в 5 подъездов» — это счёт, а не время
    if qual in ("вечера", "дня") and hour < 12:
        hour += 12
    elif qual == "ночи":
        if hour == 12:
            hour = 0  # «в 12 ночи» — полночь, а не полдень
        elif 7 <= hour <= 11:
            hour += 12  # «в 10 ночи» — это 22:00, а не 10 утра
    elif qual is None and 1 <= hour <= 6:
        hour += 12  # «в 2» почти всегда значит 14:00, а не ночь
    return time(hour, minute), [m.span()]


def extract_part_of_day(text: str, cfg) -> tuple[time | None, list[Span]]:
    hours = {"morning": cfg.morning_hour, "day": cfg.day_hour, "evening": cfg.evening_hour}
    for phrase, key in PART_OF_DAY:
        m = re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", text)
        if m:
            return time(hours[key], 0), [m.span()]
    return None, []


def extract_relative(text: str, now: datetime) -> tuple[datetime | None, list[Span]]:
    m = REL_RE.search(text)
    if not m:
        return None, []
    if m.group(1):
        delta = timedelta(minutes=30)
    elif m.group(2):
        delta = timedelta(hours=1)
    else:
        n = int(m.group(3))
        delta = timedelta(minutes=n) if m.group(4).startswith("минут") else timedelta(hours=n)
    return now + delta, [m.span()]


@dataclass
class ParsedWhen:
    day: date
    at: time | None
    explicit_date: bool
    title: str


def _overlaps(span: Span, others: list[Span]) -> bool:
    start, end = span
    return any(start < e and s < end for s, e in others)


def _extract_date(
    text: str, today: date, avoid: list[Span] | None = None
) -> tuple[date | None, list[Span]]:
    avoid = avoid or []
    if m := TOMORROW_PLUS2_RE.search(text):
        return today + timedelta(days=2), [m.span()]
    if m := TOMORROW_RE.search(text):
        return today + timedelta(days=1), [m.span()]
    if m := TODAY_RE.search(text):
        return today, [m.span()]
    if m := WEEKDAY_RE.search(text):
        ahead = (WEEKDAYS[m.group(1)] - today.weekday()) % 7 or 7
        return today + timedelta(days=ahead), [m.span()]
    if m := DAY_MONTH_RE.search(text):
        try:
            d = date(today.year, MONTHS[m.group(2)], int(m.group(1)))
        except ValueError:
            d = None  # несуществующая дата вроде «31 февраля» — считаем, что не распознали
        if d is not None:
            return (d if d >= today else d.replace(year=today.year + 1)), [m.span()]
    for m in DDMM_RE.finditer(text):
        if _overlaps(m.span(), avoid):
            continue  # эти цифры уже разобраны как время (напр. «в 6.05»), а не дата
        dd, mm = int(m.group(1)), int(m.group(2))
        if not (1 <= dd <= 31 and 1 <= mm <= 12):
            continue
        try:
            d = date(today.year, mm, dd)
        except ValueError:
            continue  # несуществующая дата вроде «31.04»
        return (d if d >= today else d.replace(year=today.year + 1)), [m.span()]
    return None, []


def _fallback_date(text: str, now: datetime) -> tuple[date | None, list[Span]]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        found = search_dates(text, languages=["ru"], settings={
            "PREFER_DATES_FROM": "future", "RELATIVE_BASE": now.replace(tzinfo=None)})
    for fragment, dt in found or []:
        if len(fragment) >= 4 and any(c.isalpha() for c in fragment):
            i = text.find(fragment)
            return dt.date(), ([(i, i + len(fragment))] if i != -1 else [])
    return None, []


def _cut(text: str, spans: list[Span]) -> str:
    out, prev = [], 0
    for start, end in sorted(spans):
        out.append(text[prev:start])
        prev = end
    out.append(text[prev:])
    return re.sub(r"\s{2,}", " ", "".join(out)).strip(" ,.-")


def parse_when(text: str, now: datetime, cfg) -> ParsedWhen:
    spans: list[Span] = []
    rel, s = extract_relative(text, now)
    if rel is not None:
        return ParsedWhen(rel.date(), rel.time(), True, _cut(text, s))
    at, s = extract_time(text)
    spans += s
    if at is None:
        at, s = extract_part_of_day(text, cfg)
        spans += s
    day, s = _extract_date(text, now.date(), avoid=spans)
    spans += s
    if day is None:
        day, s = _fallback_date(_cut(text, spans), now)
        if day is not None:
            base = _cut(text, spans)
            return ParsedWhen(day, at, True, _cut(base, s))
    return ParsedWhen(day or now.date(), at, day is not None, _cut(text, spans))


def resolve(parsed: ParsedWhen, now: datetime) -> datetime | None:
    if parsed.at is None:
        return None
    dt = datetime.combine(parsed.day, parsed.at, tzinfo=now.tzinfo)
    if dt <= now:
        if parsed.explicit_date:
            return None
        dt += timedelta(days=1)
    return dt
