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


def edited_text(name: str, before: str | None, after: str | None,
                when_local: datetime) -> str:
    return (f"✏️ {name} изменил(а) сообщение в {_hm(when_local)}\n\n"
            f"Было: «{before or ''}»\n"
            f"Стало: «{after or ''}»")


def deleted_text(name: str, message: StoredMessage, when_local: datetime) -> str:
    return f"🗑 {name} удалил(а) в {_hm(when_local)}: {_body(message)}"


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
