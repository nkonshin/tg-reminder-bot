from datetime import datetime, timedelta

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня",
              "июля", "августа", "сентября", "октября", "ноября", "декабря"]


def cap(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s


def default_title() -> str:
    return "Напоминание"


def format_dt(dt: datetime, now: datetime) -> str:
    hm = dt.strftime("%H:%M")
    if dt.date() == now.date():
        return f"сегодня, {hm}"
    if dt.date() == now.date() + timedelta(days=1):
        return f"завтра, {hm}"
    return f"{dt.day} {MONTHS_GEN[dt.month - 1]}, {hm}"


def created_text(title: str, due_local: datetime, now: datetime, calendar_ok: bool) -> str:
    base = f"Поставила напоминание: «{cap(title)}» — {format_dt(due_local, now)}."
    if calendar_ok:
        return base + " Добавила событие в календарь."
    return base + " В календарь добавить не получилось — попробую ещё раз позже."


def clarify_text(title: str) -> str:
    return f"Напомнить про «{cap(title)}» — когда?"


def ping_text(title: str) -> str:
    return f"Напоминаю: {cap(title)}"


def manual_time_prompt() -> str:
    return "Напиши, когда напомнить — например «завтра в 18» или «в 19:30»."


def manual_time_error() -> str:
    return "Не поняла время. Напиши, например, «завтра в 18» или «в 19:30»."


def calendar_failure_admin_text(title: str, error: Exception) -> str:
    return f"Календарь недоступен, событие «{title}» не создано: {error}"


def calendar_delete_failure_admin_text(error: Exception) -> str:
    return f"Не смогла удалить событие из календаря: {error}"


def stale_callback_text() -> str:
    return "Это действие сейчас недоступно."


def toast_done() -> str:
    return "Готово, закрыла напоминание"


def toast_cancel() -> str:
    return "Отменила"


def toast_snooze() -> str:
    return "Отложила на час"


def toast_edit() -> str:
    return "Ок, выбери новое время"


def toast_pod() -> str:
    return "Поставила"


def start_text() -> str:
    return ("Привет! Я буду присылать тебе напоминания, когда ты попросишь "
            "в переписке («напомни...»), и добавлять их в календарь.")


def _kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows])


def kb_created(rid: int) -> InlineKeyboardMarkup:
    return _kb([[("Отменить", f"cancel:{rid}"), ("Изменить время", f"edit:{rid}")]])


def kb_clarify(rid: int) -> InlineKeyboardMarkup:
    return _kb([
        [("Утром", f"pod:{rid}:morning"), ("Днём", f"pod:{rid}:day"), ("Вечером", f"pod:{rid}:evening")],
        [("Напишу время", f"manual:{rid}"), ("Отменить", f"cancel:{rid}")],
    ])


def kb_ping(rid: int) -> InlineKeyboardMarkup:
    return _kb([[("Готово", f"done:{rid}"), ("Отложить на час", f"snooze:{rid}")]])
