from datetime import datetime

from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from src.monitor.store import StoredMessage

# Telegram rejects a sendMessage whose text is longer than this. Every builder
# below has to stay inside it: capture.notify_owner treats a failed send as
# "the owner was never told", which for an edit means the journal keeps the
# pre-edit text and the event is effectively lost until the next one.
MESSAGE_LIMIT = 4096
# Budget per version inside an edit diff -- both versions share one message.
EDIT_PART_LIMIT = 1500
# Budget for a single deleted message's quoted body.
BODY_LIMIT = 3000
# Budget for all the bodies listed in a bulk-delete summary, and per line
# inside it so one long message cannot crowd out the rest of the list.
BULK_LIMIT = 3000
BULK_LINE_LIMIT = 200
# More deletions than this in one event get a single summary instead of one
# outbound message each -- "clear history" hands over hundreds of ids at once.
BULK_THRESHOLD = 5

TRUNCATED_MARK = " […обрезано]"

KIND_LABELS = {
    "photo": "фото",
    "video": "видео",
    "video_note": "кружок",
    "voice": "голосовое",
    "document": "документ",
}


def kind_label(kind: str | None) -> str:
    return KIND_LABELS.get(kind or "", "сообщение")


def format_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} Б"
    if n < 1024 ** 2:
        return f"{n / 1024:.1f} КБ"
    if n < 1024 ** 3:
        return f"{n / 1024 ** 2:.1f} МБ"
    return f"{n / 1024 ** 3:.2f} ГБ"


def _hm(when_local: datetime) -> str:
    return when_local.strftime("%H:%M")


def clip(text: str, limit: int) -> str:
    """Cut `text` down to `limit` characters, marking that it was cut. A
    notification that overflows Telegram's cap is not a cosmetic problem: the
    send raises, so the owner learns nothing at all."""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + TRUNCATED_MARK


def _body(message: StoredMessage, limit: int = BODY_LIMIT) -> str:
    if message.text:
        return f"«{clip(message.text, limit)}»"
    return kind_label(message.media_kind)


def edited_text(name: str | None, before: str | None, after: str | None,
                when_local: datetime) -> str:
    who = name or "Собеседник"
    return (f"✏️ {who} изменил(а) сообщение в {_hm(when_local)}\n\n"
            f"Было: «{clip(before or '', EDIT_PART_LIMIT)}»\n"
            f"Стало: «{clip(after or '', EDIT_PART_LIMIT)}»")


def deleted_text(name: str | None, message: StoredMessage, when_local: datetime) -> str:
    who = name or "Собеседник"
    return f"🗑 {who} удалил(а) в {_hm(when_local)}: {_body(message)}"


def deleted_bulk_text(messages, when_local: datetime) -> str:
    """One summary for a burst of deletions (a "clear history" hands over every
    id at once). `messages` may contain None for ids that predate the journal."""
    lines, used, shown = [], 0, 0
    for message in messages:
        body = (_body(message, BULK_LINE_LIMIT) if message is not None
                else "сообщение не сохранено")
        line = f"— {body}"
        if used + len(line) + 1 > BULK_LIMIT:
            break
        lines.append(line)
        used += len(line) + 1
        shown += 1
    rest = len(messages) - shown
    if rest:
        lines.append(f"…и ещё {rest}")
    head = f"🗑 Удалено сообщений: {len(messages)} — в {_hm(when_local)}"
    return head + "\n\n" + "\n".join(lines)


def as_document(path: str) -> FSInputFile:
    """Wrap a filesystem path for send_document. aiogram uploads a local file
    only when it is given an InputFile; a bare path str is put in the request
    verbatim, where Telegram reads it as a file_id/HTTP URL and answers "400
    wrong file identifier/HTTP URL specified". This module is the monitor's
    only aiogram-facing one, so the wrapper lives here and capture.py/export.py
    stay free of aiogram imports."""
    return FSInputFile(path)


def deleted_unknown_text(name: str | None, when_local: datetime) -> str:
    who = name or "Собеседник"
    return (f"🗑 {who} удалил(а) сообщение в {_hm(when_local)} — содержимое "
            "не сохранено (отправлено до подключения бота)")


def mirrored_prefix(owner_name: str | None) -> str:
    return f"[аккаунт: {owner_name or 'без имени'}]\n"


def _kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows])


def kb_admin_main() -> InlineKeyboardMarkup:
    return _kb([
        [("Медиа", "adm:media"), ("Хранилище", "adm:storage")],
        [("Ретеншен", "adm:retention"), ("Мониторинг", "adm:owners")],
        [("Экспорт", "adm:export")],
    ])


def admin_menu_text(owners_count: int, monitor_on: bool) -> str:
    state = "включён" if monitor_on else "выключен"
    return (f"Панель управления\n\nМониторинг: {state}\n"
            f"Подключений: {owners_count}")


def admin_media_text() -> str:
    return ("Что сохранять помимо текста. Метаданные пишутся всегда — "
            "выключенный тип означает, что не скачивается сам файл.")


def admin_storage_text(db_bytes: int, media_bytes: int, total: dict,
                       day: dict, week: dict, month: dict) -> str:
    lines = [
        "Хранилище",
        f"База: {format_bytes(db_bytes)} · медиа: {format_bytes(media_bytes)}",
        f"Всего сообщений: {total['messages']}",
    ]
    if total["by_kind"]:
        kinds = ", ".join(f"{kind_label(k)}: {n}" for k, n in sorted(total["by_kind"].items()))
        lines.append(f"Из них медиа — {kinds}")
    lines += [
        "",
        f"За сутки: {day['messages']} сообщ. / {format_bytes(day['text_bytes'])} текста",
        f"За неделю: {week['messages']} сообщ. / {format_bytes(week['text_bytes'])} текста",
        f"За 30 дней: {month['messages']} сообщ. / {format_bytes(month['text_bytes'])} текста",
    ]
    return "\n".join(lines)


def admin_retention_text(days: int) -> str:
    return (f"Сейчас на сервере хранится {days} дн.\n"
            "Локальный архив на Mac это не затрагивает — там история копится целиком.")


def admin_owners_text(owners) -> str:
    rows = [f"{o.owner_name or o.owner_user_id} — "
            f"{'активно' if o.is_enabled else 'отключено'}" for o in owners]
    return "Подключения\n\n" + ("\n".join(rows) if rows else "пока никого")


def admin_export_text() -> str:
    return ("Что выгрузить? Архив придёт сюда файлом; если он больше лимита "
            "Telegram, разобью на части.")


def kb_admin_media(owner) -> InlineKeyboardMarkup:
    def mark(field: str, label: str) -> tuple[str, str]:
        on = "✅" if getattr(owner, field) else "☐"
        return f"{on} {label}", f"adm:toggle:{owner.id}:{field}"

    return _kb([
        [mark("log_photo", "Фото"), mark("log_video", "Видео")],
        [mark("log_video_note", "Кружки"), mark("log_voice", "Голосовые")],
        [mark("log_document", "Документы")],
        [("Назад", "adm:main")],
    ])


def kb_admin_storage() -> InlineKeyboardMarkup:
    return _kb([[("Обновить", "adm:storage"), ("Назад", "adm:main")]])


def kb_admin_retention() -> InlineKeyboardMarkup:
    return _kb([
        [("7 дней", "adm:retention:7"), ("30 дней", "adm:retention:30"),
         ("90 дней", "adm:retention:90")],
        [("Назад", "adm:main")],
    ])


def kb_admin_owners(owners) -> InlineKeyboardMarkup:
    rows = []
    for o in owners:
        mark = "✅" if o.mirror_to_admin else "☐"
        rows.append([(f"{mark} дублировать: {o.owner_name or o.owner_user_id}",
                      f"adm:mirror:{o.id}")])
    rows.append([("Назад", "adm:main")])
    return _kb(rows)


def kb_admin_export() -> InlineKeyboardMarkup:
    return _kb([
        [("Только текст", "adm:export:text"), ("Текст + медиа", "adm:export:full")],
        [("Назад", "adm:main")],
    ])


def export_part_caption(index: int, total: int) -> str:
    caption = f"Часть {index}/{total}"
    if index == 1:
        caption += "\nСобрать обратно: cat export-*.tar.gz.part-* > export.tar.gz"
    return caption
