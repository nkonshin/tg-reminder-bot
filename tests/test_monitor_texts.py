import re
from datetime import datetime
from zoneinfo import ZoneInfo

from src.monitor import notify
from src.monitor.store import Owner, StoredMessage

TZ = ZoneInfo("Asia/Yekaterinburg")
WHEN = datetime(2026, 7, 30, 18, 5, tzinfo=TZ)


def a_message(**over):
    base = dict(id=1, owner_id=1, chat_id=-1, message_id=5, from_user_id=100,
                from_name="Кто-то", text="привет", media_kind=None, media_path=None,
                sent_at="2026-07-30T13:00:00+00:00", edited_at=None, deleted_at=None)
    base.update(over)
    return StoredMessage(**base)


def an_owner(**over):
    base = dict(id=1, business_connection_id="conn-1", owner_user_id=100,
                owner_name="Кто-то", is_enabled=1, monitor_enabled=1, mirror_to_admin=0,
                notify_enabled=1,
                retention_days=None, log_photo=0, log_video=0, log_video_note=0,
                log_voice=0, log_document=0, log_animation=0, log_audio=0,
                log_sticker=0,
                connected_at="2026-07-30T13:00:00+00:00")
    base.update(over)
    return Owner(**base)


def test_edited_text_shows_both_versions():
    t = notify.edited_text("Кто-то", None, "было", "стало", WHEN)
    assert "было" in t and "стало" in t and "18:05" in t


def test_edited_text_wraps_both_versions_in_a_blockquote():
    t = notify.edited_text("Кто-то", None, "было", "стало", WHEN)
    assert t.count("<blockquote>") == 2 and t.count("</blockquote>") == 2
    assert "«" not in t and "»" not in t  # guillemets are gone now that quoting is structural


def test_edited_text_omits_the_blockquote_for_an_empty_version():
    # A media-only message with no caption edited to add one (or the other
    # way around) has an empty before/after. An empty <blockquote></blockquote>
    # renders as a stray empty quote box in Telegram, so this is a plain
    # placeholder instead of empty markup.
    t = notify.edited_text("Кто-то", None, "", "стало", WHEN)
    assert "<blockquote></blockquote>" not in t
    assert "без текста" in t
    assert t.count("<blockquote>") == 1  # only "стало" is actually quoted


def test_deleted_text_quotes_the_message():
    t = notify.deleted_text("Кто-то", None, a_message(), WHEN)
    assert "привет" in t and "18:05" in t


def test_deleted_text_wraps_the_body_in_a_blockquote():
    t = notify.deleted_text("Кто-то", None, a_message(), WHEN)
    assert "<blockquote>привет</blockquote>" in t


def test_deleted_text_names_the_media_kind():
    t = notify.deleted_text("Кто-то", None, a_message(text=None, media_kind="voice"), WHEN)
    assert "голосовое" in t


def test_kind_label_covers_the_new_kinds():
    assert notify.kind_label("sticker") == "стикер"
    assert notify.kind_label("animation") == "гифка"
    assert notify.kind_label("location") == "геолокация"
    assert notify.kind_label("contact") == "контакт"
    assert notify.kind_label("poll") == "опрос"


def test_deleted_text_names_a_sticker():
    t = notify.deleted_text("Кто-то", None, a_message(text=None, media_kind="sticker"), WHEN)
    assert "стикер" in t


def test_kind_label_covers_the_second_batch_of_new_kinds():
    assert notify.kind_label("audio") == "музыка"
    assert notify.kind_label("dice") == "эмодзи-кубик"
    assert notify.kind_label("story") == "история"
    assert notify.kind_label("venue") == "место"
    assert notify.kind_label("game") == "игра"


def test_mirrored_prefix_escapes_the_owner_name():
    t = notify.mirrored_prefix("A & B")
    assert "A &amp; B" in t
    assert "A & B" not in t


def test_edited_text_falls_back_to_a_generic_name_when_none_is_known():
    t = notify.edited_text(None, None, "было", "стало", WHEN)
    assert "Собеседник" in t


def test_deleted_text_falls_back_to_a_generic_name_when_none_is_known():
    t = notify.deleted_text(None, None, a_message(), WHEN)
    assert "Собеседник" in t


def test_edited_text_stays_inside_the_telegram_limit():
    # Both versions travel in one message; 2500 + 2500 chars is 5056, which
    # sendMessage rejects outright -- so the owner would hear nothing at all.
    t = notify.edited_text("Кто-то", None, "а" * 2500, "б" * 2500, WHEN)
    assert len(t) <= notify.MESSAGE_LIMIT
    assert notify.TRUNCATED_MARK in t
    assert "а" in t and "б" in t


def test_deleted_text_stays_inside_the_telegram_limit():
    t = notify.deleted_text("Кто-то", None, a_message(text="я" * 9000), WHEN)
    assert len(t) <= notify.MESSAGE_LIMIT
    assert notify.TRUNCATED_MARK in t


def test_deleted_text_stays_inside_the_limit_when_escaping_would_expand_it():
    # html.escape can grow text up to 5x ('&' -> '&amp;'). Clipping the RAW
    # text to BODY_LIMIT and escaping afterwards (the old order) would let a
    # message of 3000 '&' escape out to 15000 chars -- past MESSAGE_LIMIT,
    # so the send raises and notify_owner silently drops the notification.
    t = notify.deleted_text("Кто-то", None, a_message(text="&" * 3000), WHEN)
    assert len(t) <= notify.MESSAGE_LIMIT
    assert notify.TRUNCATED_MARK in t


def test_edited_text_stays_inside_the_limit_when_escaping_would_expand_it():
    t = notify.edited_text("Кто-то", None, "&" * 1500, "&" * 1500, WHEN)
    assert len(t) <= notify.MESSAGE_LIMIT
    assert notify.TRUNCATED_MARK in t


def test_clipping_after_escaping_never_leaves_a_truncated_html_entity():
    # Land the cut exactly inside an '&amp;' entity: 2998 plain chars plus a
    # run of '&' means BODY_LIMIT=3000 lands two characters into the first
    # '&amp;' of the run ('&a'). A naive clip of the escaped string would
    # leave that dangling '&a' in the outgoing HTML, which Telegram also
    # rejects.
    before = "a" * 2998 + "&" * 10
    t = notify.deleted_text("Кто-то", None, a_message(text=before), WHEN)
    # Every '&' in the output must start one of the five entities
    # html.escape can produce -- no partial fragment survives.
    assert re.search(r"&(?!amp;|lt;|gt;|quot;|#x27;)", t) is None


def test_edited_text_author_line_shows_the_username_when_known():
    t = notify.edited_text("Даша", "someone", "было", "стало", WHEN)
    assert "<b>Даша</b> (@someone)" in t


def test_edited_text_author_line_has_no_stray_parentheses_without_a_username():
    t = notify.edited_text("Даша", None, "было", "стало", WHEN)
    assert "<b>Даша</b>" in t
    # "</b> (" would be the tail of the "(@username)" suffix; the Russian verb
    # ending "изменил(а)" legitimately has its own parens, so check the exact
    # boundary instead of the message as a whole.
    assert "</b> (" not in t


def test_deleted_text_author_line_shows_the_username_when_known():
    t = notify.deleted_text("Даша", "someone", a_message(), WHEN)
    assert "<b>Даша</b> (@someone)" in t


def test_deleted_text_author_line_has_no_stray_parentheses_without_a_username():
    t = notify.deleted_text("Даша", None, a_message(), WHEN)
    assert "<b>Даша</b>" in t
    assert "</b> (" not in t


def test_edited_text_escapes_html_special_characters_in_the_body():
    t = notify.edited_text("Кто-то", None, "5 < 10", "Тинькофф & Ко <3", WHEN)
    assert "5 &lt; 10" in t
    assert "Тинькофф &amp; Ко &lt;3" in t
    # No unescaped '<' or '>' survive outside of the tags this module itself
    # inserted -- otherwise Telegram rejects the whole HTML-mode send.
    assert "< 10" not in t and "Ко <3" not in t


def test_edited_text_escapes_the_author_name_and_username():
    t = notify.edited_text("<script>", "a&b", "было", "стало", WHEN)
    assert "<script>" not in t
    assert "&lt;script&gt;" in t
    assert "a&amp;b" in t


def test_deleted_text_escapes_html_special_characters_in_the_body():
    t = notify.deleted_text("Кто-то", None, a_message(text="Тинькофф & Ко <3"), WHEN)
    assert "Тинькофф &amp; Ко &lt;3" in t
    assert "Ко <3" not in t


def test_bulk_delete_summary_counts_everything_and_fits():
    messages = [a_message(message_id=i, text=f"сообщение {i}") for i in range(300)]
    t = notify.deleted_bulk_text(messages, WHEN)
    assert len(t) <= notify.MESSAGE_LIMIT
    assert "300" in t
    assert "и ещё" in t  # the tail says how many were not listed


def test_bulk_delete_summary_reports_ids_it_never_stored():
    t = notify.deleted_bulk_text([None, None], WHEN)
    assert "не сохранено" in t


def test_bulk_delete_summary_escapes_each_line():
    messages = [a_message(message_id=1, text="5 < 10"),
               a_message(message_id=2, text="Тинькофф & Ко")]
    t = notify.deleted_bulk_text(messages, WHEN)
    assert "5 &lt; 10" in t
    assert "Тинькофф &amp; Ко" in t
    assert "5 < 10" not in t and "Тинькофф & Ко" not in t


def test_as_document_wraps_a_path_for_upload():
    # A bare str is sent as a file_id, not uploaded -- see tests/fakes.py.
    from aiogram.types import FSInputFile
    assert isinstance(notify.as_document("/tmp/x.tar.gz"), FSInputFile)


def test_owners_screen_states_the_global_switch_and_every_flag():
    owner = an_owner(owner_name="Первый", monitor_enabled=0, mirror_to_admin=1)
    t = notify.admin_owners_text([owner], monitor_enabled=True)
    assert "MONITOR_ENABLED" in t and "включён" in t
    assert "Первый" in t and "на паузе" in t and "вкл" in t


def test_owners_screen_explains_the_difference_between_journal_and_notify_pauses():
    # A future reader must not confuse "журнал на паузе" (nothing at all,
    # archive included) with "уведомления выкл" (archive keeps filling,
    # only the pings stop) -- the screen text has to say so plainly.
    t = notify.admin_owners_text([an_owner()], monitor_enabled=True)
    assert "уведомлен" in t.lower()


def test_owners_screen_reports_each_owners_notify_state():
    muted = an_owner(owner_name="Молчун", notify_enabled=0)
    loud = an_owner(owner_name="Обычный", notify_enabled=1)
    t = notify.admin_owners_text([muted, loud], monitor_enabled=True)
    assert "Молчун" in t and "Обычный" in t
    muted_line = next(line for line in t.splitlines() if "Молчун" in line)
    loud_line = next(line for line in t.splitlines() if "Обычный" in line)
    assert "выкл" in muted_line
    assert "уведомления" in muted_line and "уведомления" in loud_line


def test_retention_screen_states_that_it_applies_to_all_connections():
    t = notify.admin_retention_text(30, owners_count=2)
    assert "всех подключений" in t and "2" in t


def test_format_bytes_is_readable():
    assert notify.format_bytes(512) == "512 Б"
    assert notify.format_bytes(2048) == "2.0 КБ"
    assert notify.format_bytes(5 * 1024 * 1024) == "5.0 МБ"


def test_owners_keyboard_offers_a_notify_toggle_alongside_mirror():
    owner = an_owner()
    kb = notify.kb_admin_owners([owner])
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert f"adm:toggle:{owner.id}:notify_enabled" in data
    assert f"adm:toggle:{owner.id}:mirror_to_admin" in data


def test_media_keyboard_offers_an_animation_toggle():
    owner = an_owner()
    kb = notify.kb_admin_media(owner)
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert f"adm:toggle:{owner.id}:log_animation" in data


def test_media_keyboard_offers_an_audio_toggle():
    owner = an_owner()
    kb = notify.kb_admin_media(owner)
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert f"adm:toggle:{owner.id}:log_audio" in data


def test_every_admin_button_uses_the_adm_prefix():
    owner = an_owner()
    for kb in (notify.kb_admin_main(), notify.kb_admin_storage(),
               notify.kb_admin_retention(), notify.kb_admin_export(),
               notify.kb_admin_media(owner), notify.kb_admin_media_owners([owner]),
               notify.kb_admin_owners([owner])):
        for row in kb.inline_keyboard:
            for b in row:
                assert b.callback_data.startswith("adm:"), b.callback_data
