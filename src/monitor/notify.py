from datetime import datetime

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from src.monitor.store import StoredMessage

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


def _body(message: StoredMessage) -> str:
    if message.text:
        return f"«{message.text}»"
    return kind_label(message.media_kind)


def edited_text(name: str | None, before: str | None, after: str | None,
                when_local: datetime) -> str:
    who = name or "Собеседник"
    return (f"✏️ {who} изменил(а) сообщение в {_hm(when_local)}\n\n"
            f"Было: «{before or ''}»\n"
            f"Стало: «{after or ''}»")


def deleted_text(name: str | None, message: StoredMessage, when_local: datetime) -> str:
    who = name or "Собеседник"
    return f"🗑 {who} удалил(а) в {_hm(when_local)}: {_body(message)}"


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
