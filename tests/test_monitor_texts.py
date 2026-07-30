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
                retention_days=None, log_photo=0, log_video=0, log_video_note=0,
                log_voice=0, log_document=0, connected_at="2026-07-30T13:00:00+00:00")
    base.update(over)
    return Owner(**base)


def test_edited_text_shows_both_versions():
    t = notify.edited_text("Кто-то", "было", "стало", WHEN)
    assert "было" in t and "стало" in t and "18:05" in t


def test_deleted_text_quotes_the_message():
    t = notify.deleted_text("Кто-то", a_message(), WHEN)
    assert "привет" in t and "18:05" in t


def test_deleted_text_names_the_media_kind():
    t = notify.deleted_text("Кто-то", a_message(text=None, media_kind="voice"), WHEN)
    assert "голосовое" in t


def test_deleted_unknown_says_content_was_not_stored():
    t = notify.deleted_unknown_text("Кто-то", WHEN)
    assert "не сохранено" in t


def test_edited_text_falls_back_to_a_generic_name_when_none_is_known():
    t = notify.edited_text(None, "было", "стало", WHEN)
    assert "Собеседник" in t


def test_deleted_text_falls_back_to_a_generic_name_when_none_is_known():
    t = notify.deleted_text(None, a_message(), WHEN)
    assert "Собеседник" in t


def test_edited_text_stays_inside_the_telegram_limit():
    # Both versions travel in one message; 2500 + 2500 chars is 5056, which
    # sendMessage rejects outright -- so the owner would hear nothing at all.
    t = notify.edited_text("Кто-то", "а" * 2500, "б" * 2500, WHEN)
    assert len(t) <= notify.MESSAGE_LIMIT
    assert notify.TRUNCATED_MARK in t
    assert "а" in t and "б" in t


def test_deleted_text_stays_inside_the_telegram_limit():
    t = notify.deleted_text("Кто-то", a_message(text="я" * 9000), WHEN)
    assert len(t) <= notify.MESSAGE_LIMIT
    assert notify.TRUNCATED_MARK in t


def test_bulk_delete_summary_counts_everything_and_fits():
    messages = [a_message(message_id=i, text=f"сообщение {i}") for i in range(300)]
    t = notify.deleted_bulk_text(messages, WHEN)
    assert len(t) <= notify.MESSAGE_LIMIT
    assert "300" in t
    assert "и ещё" in t  # the tail says how many were not listed


def test_bulk_delete_summary_reports_ids_it_never_stored():
    t = notify.deleted_bulk_text([None, None], WHEN)
    assert "не сохранено" in t


def test_as_document_wraps_a_path_for_upload():
    # A bare str is sent as a file_id, not uploaded -- see tests/fakes.py.
    from aiogram.types import FSInputFile
    assert isinstance(notify.as_document("/tmp/x.tar.gz"), FSInputFile)


def test_format_bytes_is_readable():
    assert notify.format_bytes(512) == "512 Б"
    assert notify.format_bytes(2048) == "2.0 КБ"
    assert notify.format_bytes(5 * 1024 * 1024) == "5.0 МБ"


def test_every_admin_button_uses_the_adm_prefix():
    owner = an_owner()
    for kb in (notify.kb_admin_main(), notify.kb_admin_storage(),
               notify.kb_admin_retention(), notify.kb_admin_export(),
               notify.kb_admin_media(owner), notify.kb_admin_owners([owner])):
        for row in kb.inline_keyboard:
            for b in row:
                assert b.callback_data.startswith("adm:"), b.callback_data
