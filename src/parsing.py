import re
from datetime import datetime, time, timedelta

Span = tuple[int, int]

TIME_RE = re.compile(
    r"\b(?:в|к)\s+(\d{1,2})(?:[:.](\d{2}))?(?:\s*час(?:а|ов|ик(?:а|ов)?)?)?"
    r"(?:\s+(утра|дня|вечера|ночи))?\b"
)

PART_OF_DAY = [  # (фраза, ключ) — длинные фразы раньше коротких
    ("после обеда", "day"), ("в обед", "day"), ("с утра", "morning"),
    ("к вечеру", "evening"), ("утречком", "morning"), ("вечерком", "evening"),
    ("утром", "morning"), ("днем", "day"), ("вечером", "evening"),
]

REL_RE = re.compile(r"\bчерез\s+(?:(полчаса)|(час(?:ик)?)|(\d+)\s*(минут\w*|час\w*))\b")


def extract_time(text: str) -> tuple[time | None, list[Span]]:
    m = TIME_RE.search(text)
    if not m:
        return None, []
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    qual = m.group(3)
    if hour > 23 or minute > 59:
        return None, []
    if qual in ("вечера", "дня") and hour < 12:
        hour += 12
    elif qual == "ночи" and hour == 12:
        hour = 0  # «в 12 ночи» — полночь, а не полдень
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
