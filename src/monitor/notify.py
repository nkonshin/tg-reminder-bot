import html
import re
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
# This many deletions or more in one event get a single summary instead of
# one outbound message each -- "clear history" hands over hundreds of ids at
# once. Kept high enough that an ordinary multi-message delete (e.g. a
# several-photo album) still gets each message its own notification and
# media attachment, rather than the summary's text-only, clipped treatment.
BULK_THRESHOLD = 20

TRUNCATED_MARK = " […обрезано]"

# A trailing fragment of one of the five entities html.escape() can produce
# (&amp; &lt; &gt; &quot; &#x27;), with no closing ';' -- i.e. the cut landed
# inside the entity rather than before or after it. Anchored at the end of
# the string; a *complete* entity always ends in ';', which none of these
# alternatives consume, so a well-formed trailing entity is never matched.
_PARTIAL_ENTITY_RE = re.compile(r"&(#x?[0-9a-fA-F]*|[a-zA-Z]*)$")

KIND_LABELS = {
    "photo": "фото",
    "video": "видео",
    "video_note": "кружок",
    "voice": "голосовое",
    "document": "документ",
    "sticker": "стикер",
    "animation": "гифка",
    "location": "геолокация",
    "contact": "контакт",
    "poll": "опрос",
    "audio": "музыка",
    "dice": "эмодзи-кубик",
    "story": "история",
    "venue": "место",
    "game": "игра",
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


def _who(name: str | None, username: str | None) -> str:
    """Author line: bold display name, plus the @username alongside it when
    Telegram exposes one (many users have none -- no stray parentheses then).
    Both are attacker-controlled Telegram profile text, escaped before they
    reach the HTML-parsed message -- an unescaped '<', '>' or '&' makes
    Telegram reject the whole send."""
    who = html.escape(name or "Собеседник")
    if username:
        return f"<b>{who}</b> (@{html.escape(username)})"
    return f"<b>{who}</b>"


def _clip_escaped(text: str, limit: int) -> str:
    """Escape first, THEN clip to `limit` characters of the escaped text.

    html.escape() can expand text up to 5x ('&' -> '&amp;'), so clipping the
    raw text to `limit` and escaping afterwards -- the natural-looking order
    -- lets the escaped result blow straight past `limit`, and with it
    Telegram's whole-message cap: the send then raises and notify_owner
    swallows the failure, so the owner silently never learns about the
    edit/delete at all.

    Clipping the already-escaped text can itself land inside an entity (e.g.
    cut '&amp;' into '&am'), which Telegram's HTML parser also rejects, so
    any dangling partial entity at the cut point is stripped before the
    truncation mark is appended.
    """
    escaped = html.escape(text)
    if len(escaped) <= limit:
        return escaped
    cut = _PARTIAL_ENTITY_RE.sub("", escaped[:limit])
    return cut.rstrip() + TRUNCATED_MARK


def _quote(text: str, limit: int) -> str:
    if not text:
        # An empty <blockquote></blockquote> (a media message with no
        # caption, edited to add/remove text) renders as a stray empty quote
        # box in Telegram -- a plain placeholder instead.
        return "(без текста)"
    return f"<blockquote>{_clip_escaped(text, limit)}</blockquote>"


def _body(message: StoredMessage, limit: int = BODY_LIMIT) -> str:
    if message.text:
        return _quote(message.text, limit)
    return kind_label(message.media_kind)


def edited_text(name: str | None, username: str | None, before: str | None,
                after: str | None, when_local: datetime) -> str:
    who = _who(name, username)
    return (f"✏️ {who} изменил(а) сообщение в {_hm(when_local)}\n\n"
            f"Было:\n{_quote(before or '', EDIT_PART_LIMIT)}\n"
            f"Стало:\n{_quote(after or '', EDIT_PART_LIMIT)}")


def deleted_text(name: str | None, username: str | None, message: StoredMessage,
                 when_local: datetime) -> str:
    who = _who(name, username)
    return f"🗑 {who} удалил(а) в {_hm(when_local)}: {_body(message)}"


def deleted_bulk_text(messages, when_local: datetime) -> str:
    """One summary for a burst of deletions (a "clear history" hands over every
    id at once). `messages` may contain None for ids that predate the journal.
    Each line is escaped (not blockquoted -- a summary line, not a full quote)
    since the message body is user-controlled text going into an HTML send."""
    lines, used, shown = [], 0, 0
    for message in messages:
        if message is None:
            body = "сообщение не сохранено"
        elif message.text:
            body = html.escape(clip(message.text, BULK_LINE_LIMIT))
        else:
            body = kind_label(message.media_kind)
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


def mirrored_prefix(owner_name: str | None) -> str:
    # owner_name is the owner's own Telegram display name -- still user-
    # controlled text, and this prefix rides along on the same HTML-mode send.
    who = html.escape(owner_name) if owner_name else "без имени"
    return f"[аккаунт: {who}]\n"


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


def admin_media_text(owner_name: str | None = None) -> str:
    whose = f" — {owner_name}" if owner_name else ""
    return (f"Что сохранять помимо текста{whose}. Метаданные пишутся всегда — "
            "выключенный тип означает, что не скачивается сам файл.")


def admin_media_picker_text() -> str:
    return ("Подключений несколько, а тумблеры медиа у каждого свои. "
            "Чьи настраиваем?")


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


def admin_retention_text(days: int, owners_count: int = 0) -> str:
    # Spelled out because the three screens do three different things: медиа
    # and мониторинг act on one connection, ретеншен on all of them at once.
    return (f"Сейчас на сервере хранится {days} дн.\n"
            f"Кнопка ниже меняет срок сразу у всех подключений ({owners_count}).\n"
            "Локальный архив на Mac это не затрагивает — там история копится целиком.")


def admin_owners_text(owners, monitor_enabled: bool) -> str:
    state = "включён" if monitor_enabled else "выключен"
    head = ("Мониторинг\n\n"
            f"Глобально (MONITOR_ENABLED): {state} — при выключенном не работает "
            "ничего, независимо от тумблеров ниже.\n\n"
            "«Журнал» у отдельного подключения — то же самое, но только для "
            "него: на паузе не работает вообще ничего, архив тоже. "
            "«Уведомления» — мягче: журнал ведётся как обычно (архив, "
            "экспорт, бэкап), просто владельцу не приходят пинги о правках "
            "и удалениях.")
    if not owners:
        return head + "\n\nПодключений пока нет."
    rows = [f"{o.owner_name or o.owner_user_id}: "
            f"{'подключено' if o.is_enabled else 'отключено Telegram'}, "
            f"журнал {'ведётся' if o.monitor_enabled else 'на паузе'}, "
            f"уведомления {'вкл' if o.notify_enabled else 'выкл'}, "
            f"дубли админу {'вкл' if o.mirror_to_admin else 'выкл'}" for o in owners]
    return head + "\n\n" + "\n".join(rows)


def admin_export_text() -> str:
    return ("Что выгрузить? Архив придёт сюда файлом; если он больше лимита "
            "Telegram, разобью на части.")


def _toggle(owner, field: str, label: str) -> tuple[str, str]:
    on = "✅" if getattr(owner, field) else "☐"
    return f"{on} {label}", f"adm:toggle:{owner.id}:{field}"


def kb_admin_media(owner, back: str = "adm:main") -> InlineKeyboardMarkup:
    return _kb([
        [_toggle(owner, "log_photo", "Фото"), _toggle(owner, "log_video", "Видео")],
        [_toggle(owner, "log_video_note", "Кружки"),
         _toggle(owner, "log_voice", "Голосовые")],
        [_toggle(owner, "log_document", "Документы"), _toggle(owner, "log_animation", "Гифки")],
        [_toggle(owner, "log_audio", "Музыка"), _toggle(owner, "log_sticker", "Стикеры")],
        [("Назад", back)],
    ])


def kb_admin_media_owners(owners) -> InlineKeyboardMarkup:
    rows = [[(str(o.owner_name or o.owner_user_id), f"adm:media:{o.id}")] for o in owners]
    rows.append([("Назад", "adm:main")])
    return _kb(rows)


def kb_admin_storage() -> InlineKeyboardMarkup:
    return _kb([[("Обновить", "adm:storage"), ("Назад", "adm:main")]])


def kb_admin_retention() -> InlineKeyboardMarkup:
    return _kb([
        [("7 дней", "adm:retention:7"), ("30 дней", "adm:retention:30"),
         ("90 дней", "adm:retention:90")],
        [("Назад", "adm:main")],
    ])


def kb_admin_owners(owners) -> InlineKeyboardMarkup:
    """One row per connection with all three of its switches: the per-owner
    monitoring pause (stops everything, journal included), the notify-only
    mute (journal keeps running, only the pings stop), and the mirror-to-
    admin toggle."""
    rows = []
    for o in owners:
        whose = o.owner_name or o.owner_user_id
        rows.append([_toggle(o, "monitor_enabled", f"журнал: {whose}")])
        rows.append([_toggle(o, "notify_enabled", f"уведомления: {whose}")])
        rows.append([_toggle(o, "mirror_to_admin", f"дубли админу: {whose}")])
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
