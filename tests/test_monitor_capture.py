import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.config import Config
from src.monitor import capture, notify
from src.monitor.store import MonitorStore
from tests.fakes import FakeBot

NOW = datetime(2026, 7, 30, 13, 0, tzinfo=timezone.utc)


@pytest.fixture
async def deps(tmp_path):
    store = MonitorStore(str(tmp_path / "t.sqlite3"))
    await store.init()
    cfg = Config(bot_token="t", her_user_id=100, admin_user_id=200,
                 monitor_media_dir=str(tmp_path / "media"), _env_file=None)
    return capture.MonitorDeps(bot=FakeBot(), store=store, cfg=cfg)


def a_connection(conn_id="conn-1", user_id=100, enabled=True):
    return SimpleNamespace(id=conn_id, is_enabled=enabled,
                           user=SimpleNamespace(id=user_id, full_name="Владелец"))


def a_message(conn_id="conn-1", chat_id=-1, message_id=5, text="привет",
              user_id=300, name="Собеседник", username=None, is_bot=False,
              forward_origin=None, **media_fields):
    fields = dict(photo=None, video=None, video_note=None, voice=None, document=None)
    fields.update(media_fields)
    return SimpleNamespace(business_connection_id=conn_id,
                           chat=SimpleNamespace(id=chat_id),
                           message_id=message_id, text=text, caption=None,
                           from_user=SimpleNamespace(id=user_id, full_name=name,
                                                     username=username, is_bot=is_bot),
                           forward_origin=forward_origin,
                           **fields)


async def connect(deps, **kw):
    conn = a_connection(**kw)
    await capture.on_business_connection(conn, deps, NOW)
    return await deps.store.get_owner(conn.id)


async def test_connection_creates_the_owner(deps):
    owner = await connect(deps)
    assert owner.owner_user_id == 100 and owner.is_enabled == 1


async def test_disconnection_disables_the_owner(deps):
    await connect(deps)
    await capture.on_business_connection(a_connection(enabled=False), deps, NOW)
    assert (await deps.store.get_owner("conn-1")).is_enabled == 0


async def test_a_disconnected_owner_gets_no_notifications(deps):
    owner = await connect(deps)
    await capture.on_business_message(a_message(text="было"), deps, NOW)
    await capture.on_business_connection(a_connection(enabled=False), deps, NOW)
    deps.bot.sent.clear()
    event = SimpleNamespace(business_connection_id="conn-1",
                            chat=SimpleNamespace(id=-1), message_ids=[5])
    await capture.on_deleted_business_messages(event, deps, NOW)
    await capture.on_edited_business_message(a_message(text="стало"), deps, NOW)
    assert deps.bot.sent == []


async def test_message_is_recorded(deps):
    owner = await connect(deps)
    await capture.on_business_message(a_message(), deps, NOW)
    assert (await deps.store.get_message(owner.id, -1, 5)).text == "привет"


async def test_message_from_an_unknown_connection_is_ignored(deps):
    await capture.on_business_message(a_message(conn_id="nope"), deps, NOW)
    assert await deps.store.get_owner("nope") is None


async def test_disabled_owner_records_nothing(deps):
    owner = await connect(deps)
    await deps.store.set_owner_flag(owner.id, "monitor_enabled", 0)
    await capture.on_business_message(a_message(), deps, NOW)
    assert await deps.store.get_message(owner.id, -1, 5) is None


async def test_media_metadata_is_recorded_even_when_download_is_off(deps):
    owner = await connect(deps)
    msg = a_message(text=None, voice=SimpleNamespace(file_id="v1"))
    await capture.on_business_message(msg, deps, NOW)
    stored = await deps.store.get_message(owner.id, -1, 5)
    assert stored.media_kind == "voice" and stored.media_path is None
    assert deps.bot.downloads == []


async def test_media_file_is_downloaded_when_the_kind_is_enabled(deps):
    owner = await connect(deps)
    await deps.store.set_owner_flag(owner.id, "log_voice", 1)
    msg = a_message(text=None, voice=SimpleNamespace(file_id="v1"))
    await capture.on_business_message(msg, deps, NOW)
    stored = await deps.store.get_message(owner.id, -1, 5)
    assert stored.media_path == f"{owner.id}/-1/5.ogg"
    assert deps.bot.downloads == ["v1"]


async def test_a_sticker_message_is_recorded_without_a_download(deps):
    # No log_sticker column exists at all -- is_enabled_for must fall back to
    # "off" for it, so a sticker's metadata is journaled but the file itself
    # is never fetched.
    owner = await connect(deps)
    msg = a_message(text=None, sticker=SimpleNamespace(file_id="s1"))
    await capture.on_business_message(msg, deps, NOW)
    stored = await deps.store.get_message(owner.id, -1, 5)
    assert stored.media_kind == "sticker" and stored.media_path is None
    assert deps.bot.downloads == []


async def test_a_gif_is_downloaded_when_log_animation_is_enabled(deps):
    owner = await connect(deps)
    await deps.store.set_owner_flag(owner.id, "log_animation", 1)
    msg = a_message(text=None, animation=SimpleNamespace(file_id="g1"))
    await capture.on_business_message(msg, deps, NOW)
    stored = await deps.store.get_message(owner.id, -1, 5)
    assert stored.media_path == f"{owner.id}/-1/5.mp4"
    assert deps.bot.downloads == ["g1"]


async def test_a_gif_is_metadata_only_when_log_animation_is_off(deps):
    owner = await connect(deps)
    msg = a_message(text=None, animation=SimpleNamespace(file_id="g1"))
    await capture.on_business_message(msg, deps, NOW)
    stored = await deps.store.get_message(owner.id, -1, 5)
    assert stored.media_kind == "animation" and stored.media_path is None
    assert deps.bot.downloads == []


async def test_deletion_of_a_sticker_notifies_with_its_label(deps):
    await connect(deps)
    msg = a_message(text=None, sticker=SimpleNamespace(file_id="s1"))
    await capture.on_business_message(msg, deps, NOW)
    deps.bot.sent.clear()
    event = SimpleNamespace(business_connection_id="conn-1",
                            chat=SimpleNamespace(id=-1), message_ids=[5])
    await capture.on_deleted_business_messages(event, deps, NOW)
    assert "стикер" in deps.bot.sent[0].text


async def test_a_dice_message_is_recorded_without_a_download(deps):
    owner = await connect(deps)
    msg = a_message(text=None, dice=SimpleNamespace(emoji="🎲", value=4))
    await capture.on_business_message(msg, deps, NOW)
    stored = await deps.store.get_message(owner.id, -1, 5)
    assert stored.media_kind == "dice" and stored.media_path is None
    assert deps.bot.downloads == []


async def test_an_audio_file_is_downloaded_when_log_audio_is_enabled(deps):
    owner = await connect(deps)
    await deps.store.set_owner_flag(owner.id, "log_audio", 1)
    msg = a_message(text=None, audio=SimpleNamespace(file_id="a1"))
    await capture.on_business_message(msg, deps, NOW)
    stored = await deps.store.get_message(owner.id, -1, 5)
    assert stored.media_path == f"{owner.id}/-1/5.mp3"
    assert deps.bot.downloads == ["a1"]


async def test_an_audio_file_is_metadata_only_when_log_audio_is_off(deps):
    owner = await connect(deps)
    msg = a_message(text=None, audio=SimpleNamespace(file_id="a1"))
    await capture.on_business_message(msg, deps, NOW)
    stored = await deps.store.get_message(owner.id, -1, 5)
    assert stored.media_kind == "audio" and stored.media_path is None
    assert deps.bot.downloads == []


async def test_edit_notifies_with_both_versions(deps):
    owner = await connect(deps)
    await capture.on_business_message(a_message(text="было"), deps, NOW)
    deps.bot.sent.clear()
    await capture.on_edited_business_message(a_message(text="стало"), deps, NOW)
    assert len(deps.bot.sent) == 1
    assert "было" in deps.bot.sent[0].text and "стало" in deps.bot.sent[0].text
    assert (await deps.store.get_message(owner.id, -1, 5)).text == "стало"


async def test_notifications_are_sent_as_html(deps):
    # notify_owner must opt in to parse_mode per call -- never globally on the
    # Bot instance, which the reminder half also shares (see src/main.py).
    await connect(deps)
    await capture.on_business_message(a_message(text="было"), deps, NOW)
    deps.bot.sent.clear()
    await capture.on_edited_business_message(a_message(text="стало"), deps, NOW)
    assert deps.bot.sent[0].parse_mode == "HTML"


async def test_from_username_is_journaled_and_appears_in_the_edit_notification(deps):
    owner = await connect(deps)
    await capture.on_business_message(a_message(text="было", username="dasha_biz"), deps, NOW)
    stored = await deps.store.get_message(owner.id, -1, 5)
    assert stored.from_username == "dasha_biz"
    deps.bot.sent.clear()
    await capture.on_edited_business_message(
        a_message(text="стало", username="dasha_biz"), deps, NOW)
    assert "@dasha_biz" in deps.bot.sent[0].text


def test_forward_source_is_none_for_a_plain_message():
    assert capture._forward_source(a_message()) == (None, None)


def test_forward_source_from_a_user():
    sender = SimpleNamespace(full_name="Иван Иванов", username="ivan_i")
    origin = SimpleNamespace(sender_user=sender)
    assert capture._forward_source(a_message(forward_origin=origin)) == ("Иван Иванов", "ivan_i")


def test_forward_source_from_a_user_without_a_username():
    sender = SimpleNamespace(full_name="Иван Иванов", username=None)
    origin = SimpleNamespace(sender_user=sender)
    assert capture._forward_source(a_message(forward_origin=origin)) == ("Иван Иванов", None)


def test_forward_source_from_a_hidden_user():
    origin = SimpleNamespace(sender_user_name="Скрытый Пользователь")
    assert capture._forward_source(a_message(forward_origin=origin)) == \
        ("Скрытый Пользователь", None)


def test_forward_source_from_a_chat():
    sender_chat = SimpleNamespace(title="Рабочий чат", username="work_chat")
    origin = SimpleNamespace(sender_chat=sender_chat)
    assert capture._forward_source(a_message(forward_origin=origin)) == \
        ("Рабочий чат", "work_chat")


def test_forward_source_from_a_channel():
    chat = SimpleNamespace(title="Новостной канал", username="news_channel")
    origin = SimpleNamespace(chat=chat)
    assert capture._forward_source(a_message(forward_origin=origin)) == \
        ("Новостной канал", "news_channel")


async def test_a_forwarded_message_is_journaled_with_its_source(deps):
    owner = await connect(deps)
    sender = SimpleNamespace(full_name="Иван Иванов", username="ivan_i")
    origin = SimpleNamespace(sender_user=sender)
    await capture.on_business_message(
        a_message(text="переслано", forward_origin=origin), deps, NOW)
    stored = await deps.store.get_message(owner.id, -1, 5)
    assert stored.forward_from_name == "Иван Иванов"
    assert stored.forward_from_username == "ivan_i"


async def test_a_normal_message_has_no_forward_source(deps):
    owner = await connect(deps)
    await capture.on_business_message(a_message(text="привет"), deps, NOW)
    stored = await deps.store.get_message(owner.id, -1, 5)
    assert stored.forward_from_name is None
    assert stored.forward_from_username is None


async def test_an_edited_pre_connection_forwarded_message_captures_its_source(deps):
    # Same bootstrap path as test_edit_of_a_pre_connection_message_also_captures_its_media:
    # a message predating the connection is seen for the first time via the edit
    # handler, and that first-sight capture must thread the forward source too.
    owner = await connect(deps)
    sender = SimpleNamespace(full_name="Пётр Петров", username=None)
    origin = SimpleNamespace(sender_user=sender)
    msg = a_message(text="давнее", forward_origin=origin)
    await capture.on_edited_business_message(msg, deps, NOW)
    stored = await deps.store.get_message(owner.id, -1, 5)
    assert stored.forward_from_name == "Пётр Петров"
    assert stored.forward_from_username is None


async def test_an_author_without_a_username_gets_no_stray_parentheses(deps):
    await connect(deps)
    await capture.on_business_message(a_message(text="было", username=None), deps, NOW)
    deps.bot.sent.clear()
    await capture.on_edited_business_message(a_message(text="стало", username=None), deps, NOW)
    # "</b> (" would be the tail of a "(@username)" suffix; the Russian verb
    # ending "изменил(а)" legitimately has its own parens elsewhere in the text.
    assert "</b> (" not in deps.bot.sent[0].text


async def test_an_edit_with_html_special_characters_is_escaped_and_delivered(deps):
    # With parse_mode="HTML" on, an unescaped '<', '>' or '&' makes Telegram
    # reject the whole send -- and notify_owner swallows that failure, so an
    # unescaped body would mean the owner silently never learns about the
    # edit at all.
    owner = await connect(deps)
    await capture.on_business_message(a_message(text="5 < 10"), deps, NOW)
    deps.bot.sent.clear()
    await capture.on_edited_business_message(
        a_message(text="Тинькофф & Ко <3"), deps, NOW)
    assert len(deps.bot.sent) == 1, "the fake bot must accept the escaped send"
    assert "5 &lt; 10" in deps.bot.sent[0].text
    assert "Тинькофф &amp; Ко &lt;3" in deps.bot.sent[0].text
    assert (await deps.store.get_message(owner.id, -1, 5)).text == "Тинькофф & Ко <3"


async def test_a_delete_with_html_special_characters_is_escaped_and_delivered(deps):
    await connect(deps)
    await capture.on_business_message(a_message(text="Тинькофф & Ко <3"), deps, NOW)
    deps.bot.sent.clear()
    event = SimpleNamespace(business_connection_id="conn-1",
                            chat=SimpleNamespace(id=-1), message_ids=[5])
    await capture.on_deleted_business_messages(event, deps, NOW)
    assert len(deps.bot.sent) == 1
    assert "Тинькофф &amp; Ко &lt;3" in deps.bot.sent[0].text


async def test_a_delete_full_of_ampersands_is_still_delivered_within_the_limit(deps):
    # html.escape expands '&' 5x ('&' -> '&amp;'); clipping the raw text and
    # escaping afterwards let a 3000-char body balloon past FakeBot's/
    # Telegram's 4096 cap. The send then raised, notify_owner swallowed it,
    # and the owner was never told about the deletion at all.
    await connect(deps)
    await capture.on_business_message(a_message(text="&" * 3000), deps, NOW)
    deps.bot.sent.clear()
    event = SimpleNamespace(business_connection_id="conn-1",
                            chat=SimpleNamespace(id=-1), message_ids=[5])
    await capture.on_deleted_business_messages(event, deps, NOW)
    assert len(deps.bot.sent) == 1, "the owner must still be told about the deletion"
    assert len(deps.bot.sent[0].text) <= 4096


async def test_an_edit_full_of_ampersands_is_still_delivered_within_the_limit(deps):
    owner = await connect(deps)
    await capture.on_business_message(a_message(text="&" * 1500), deps, NOW)
    deps.bot.sent.clear()
    await capture.on_edited_business_message(a_message(text="&" * 1500 + "x"), deps, NOW)
    assert len(deps.bot.sent) == 1, "the owner must still be told about the edit"
    assert len(deps.bot.sent[0].text) <= 4096
    assert (await deps.store.get_message(owner.id, -1, 5)).text == "&" * 1500 + "x"


async def test_edit_with_unchanged_text_is_silent(deps):
    await connect(deps)
    await capture.on_business_message(a_message(text="привет"), deps, NOW)
    deps.bot.sent.clear()
    await capture.on_edited_business_message(a_message(text="привет"), deps, NOW)
    assert deps.bot.sent == []


async def test_delete_notifies_with_the_stored_username(deps):
    owner = await connect(deps)
    await capture.on_business_message(
        a_message(text="секрет", username="dasha_biz"), deps, NOW)
    deps.bot.sent.clear()
    event = SimpleNamespace(business_connection_id="conn-1",
                            chat=SimpleNamespace(id=-1), message_ids=[5])
    await capture.on_deleted_business_messages(event, deps, NOW)
    assert "@dasha_biz" in deps.bot.sent[0].text
    assert (await deps.store.get_message(owner.id, -1, 5)).from_username == "dasha_biz"


async def test_delete_notifies_with_the_stored_text(deps):
    owner = await connect(deps)
    await capture.on_business_message(a_message(text="секрет"), deps, NOW)
    deps.bot.sent.clear()
    event = SimpleNamespace(business_connection_id="conn-1",
                            chat=SimpleNamespace(id=-1), message_ids=[5])
    await capture.on_deleted_business_messages(event, deps, NOW)
    assert "секрет" in deps.bot.sent[0].text
    assert (await deps.store.get_message(owner.id, -1, 5)).deleted_at is not None


async def test_delete_of_an_unknown_message_says_so(deps):
    await connect(deps)
    event = SimpleNamespace(business_connection_id="conn-1",
                            chat=SimpleNamespace(id=-1), message_ids=[999])
    await capture.on_deleted_business_messages(event, deps, NOW)
    assert "не сохранено" in deps.bot.sent[0].text


async def test_mirror_to_admin_duplicates_the_event(deps):
    owner = await connect(deps)
    await deps.store.set_owner_flag(owner.id, "mirror_to_admin", 1)
    await capture.on_business_message(a_message(text="секрет"), deps, NOW)
    deps.bot.sent.clear()
    event = SimpleNamespace(business_connection_id="conn-1",
                            chat=SimpleNamespace(id=-1), message_ids=[5])
    await capture.on_deleted_business_messages(event, deps, NOW)
    chats = [m.chat_id for m in deps.bot.sent]
    assert 100 in chats and 200 in chats


async def test_owner_who_is_the_admin_is_not_notified_twice(deps):
    owner = await connect(deps, user_id=200)  # owner IS the admin
    await deps.store.set_owner_flag(owner.id, "mirror_to_admin", 1)
    await capture.on_business_message(a_message(text="секрет"), deps, NOW)
    deps.bot.sent.clear()
    event = SimpleNamespace(business_connection_id="conn-1",
                            chat=SimpleNamespace(id=-1), message_ids=[5])
    await capture.on_deleted_business_messages(event, deps, NOW)
    assert len(deps.bot.sent) == 1


async def test_edit_of_a_pre_connection_message_also_captures_its_media(deps):
    # The message predates the connection, so the edit handler is seeing it
    # for the first time -- it must apply the same "metadata always, file
    # only if enabled" rule that on_business_message uses, not silently skip
    # the download.
    owner = await connect(deps)
    await deps.store.set_owner_flag(owner.id, "log_voice", 1)
    msg = a_message(text=None, voice=SimpleNamespace(file_id="v1"))
    await capture.on_edited_business_message(msg, deps, NOW)
    stored = await deps.store.get_message(owner.id, -1, 5)
    assert stored.media_kind == "voice"
    assert stored.media_path == f"{owner.id}/-1/5.ogg"
    assert deps.bot.downloads == ["v1"]


async def test_a_deleted_media_file_is_re_shared_with_the_notification(deps):
    # The spec promises the downloaded file back when the message is deleted --
    # the only moment it still exists anywhere. Passing the path as a bare str
    # makes aiogram send it as a file_id, so Telegram rejects it and nobody
    # ever gets the file.
    owner = await connect(deps)
    await deps.store.set_owner_flag(owner.id, "log_voice", 1)
    msg = a_message(text=None, voice=SimpleNamespace(file_id="v1"))
    await capture.on_business_message(msg, deps, NOW)
    event = SimpleNamespace(business_connection_id="conn-1",
                            chat=SimpleNamespace(id=-1), message_ids=[5])
    await capture.on_deleted_business_messages(event, deps, NOW)
    assert len(deps.bot.documents) == 1
    assert deps.bot.documents[0].path.endswith("5.ogg")
    assert deps.bot.documents[0].chat_id == 100


async def test_a_long_edit_is_delivered_within_the_telegram_limit(deps):
    # 2500 chars before + 2500 after is 5056 chars of notification, past
    # Telegram's 4096 cap: the send raises, notify_owner swallows it, and the
    # owner is never told the edit happened at all.
    owner = await connect(deps)
    before = "а" * 2500
    after = "б" * 2500
    await capture.on_business_message(a_message(text=before), deps, NOW)
    deps.bot.sent.clear()
    await capture.on_edited_business_message(a_message(text=after), deps, NOW)
    assert len(deps.bot.sent) == 1, "the owner must be told about a long edit"
    assert len(deps.bot.sent[0].text) <= 4096
    assert "а" in deps.bot.sent[0].text and "б" in deps.bot.sent[0].text
    assert (await deps.store.get_message(owner.id, -1, 5)).text == after


async def test_an_undelivered_edit_keeps_the_stored_original(deps):
    # An edit destroys the original on Telegram's side; this journal is the
    # only surviving copy. Overwriting it before the owner has actually been
    # told loses the pre-edit text from the server AND from every future
    # backup, with nobody any the wiser.
    owner = await connect(deps)
    await capture.on_business_message(a_message(text="оригинал"), deps, NOW)
    deps.bot.fail_chat_ids.add(owner.owner_user_id)
    deps.bot.sent.clear()
    await capture.on_edited_business_message(a_message(text="подмена"), deps, NOW)
    stored = await deps.store.get_message(owner.id, -1, 5)
    assert stored.text == "оригинал"
    assert stored.edited_at is None


async def test_the_owners_own_edit_is_not_reported_back_to_them(deps):
    # Business updates carry the owner's own messages too (src/handlers.py
    # relies on that). Journal them, but do not tell someone their own typo
    # fix was "изменил(а) сообщение".
    owner = await connect(deps)
    mine = dict(user_id=100, name="Владелец")
    await capture.on_business_message(a_message(text="было", **mine), deps, NOW)
    deps.bot.sent.clear()
    await capture.on_edited_business_message(a_message(text="стало", **mine), deps, NOW)
    assert deps.bot.sent == []
    assert (await deps.store.get_message(owner.id, -1, 5)).text == "стало"


async def test_the_owners_own_deletion_is_not_reported_back_to_them(deps):
    owner = await connect(deps)
    await capture.on_business_message(
        a_message(text="моё", user_id=100, name="Владелец"), deps, NOW)
    deps.bot.sent.clear()
    event = SimpleNamespace(business_connection_id="conn-1",
                            chat=SimpleNamespace(id=-1), message_ids=[5])
    await capture.on_deleted_business_messages(event, deps, NOW)
    assert deps.bot.sent == []
    assert (await deps.store.get_message(owner.id, -1, 5)).deleted_at is not None


async def test_a_bulk_delete_sends_a_bounded_number_of_messages(deps):
    # "Clear history" hands over every id at once. One send per id 429s most
    # of the burst; every swallowed failure is a deletion the owner never
    # hears about, because nothing retries.
    owner = await connect(deps)
    ids = list(range(1, 201))
    for message_id in ids:
        await deps.store.record_message(owner.id, -1, message_id, 300, "Собеседник",
                                        f"сообщение {message_id}", None, None, NOW)
    deps.bot.sent.clear()
    event = SimpleNamespace(business_connection_id="conn-1",
                            chat=SimpleNamespace(id=-1), message_ids=ids)
    await capture.on_deleted_business_messages(event, deps, NOW)
    assert len(deps.bot.sent) <= 3, f"{len(deps.bot.sent)} sends for one delete event"
    assert all(len(m.text) <= 4096 for m in deps.bot.sent)
    assert "200" in deps.bot.sent[0].text  # says how many went
    assert (await deps.store.get_message(owner.id, -1, 1)).deleted_at is not None
    assert (await deps.store.get_message(owner.id, -1, 200)).deleted_at is not None


async def test_a_small_delete_still_reports_each_message_separately(deps):
    owner = await connect(deps)
    for message_id in (1, 2):
        await deps.store.record_message(owner.id, -1, message_id, 300, "Собеседник",
                                        f"секрет {message_id}", None, None, NOW)
    deps.bot.sent.clear()
    event = SimpleNamespace(business_connection_id="conn-1",
                            chat=SimpleNamespace(id=-1), message_ids=[1, 2])
    await capture.on_deleted_business_messages(event, deps, NOW)
    assert len(deps.bot.sent) == 2
    assert "секрет 1" in deps.bot.sent[0].text and "секрет 2" in deps.bot.sent[1].text


async def test_a_delete_just_under_the_bulk_threshold_reports_each_message_individually(deps):
    # BULK_THRESHOLD exists to defuse a "clear history" flood, not to catch
    # an ordinary multi-message delete (e.g. a 7-photo album). Below it every
    # message must keep its own notification, media attachment included --
    # not the summary's `— фото`-only, no-attachment, 200-char-clipped line.
    owner = await connect(deps)
    ids = list(range(1, notify.BULK_THRESHOLD))  # one short of the threshold
    for message_id in ids:
        await deps.store.record_message(owner.id, -1, message_id, 300, "Собеседник",
                                        f"сообщение {message_id}", None, None, NOW)
    file_rel = f"{owner.id}/-1/1.jpg"
    abs_path = os.path.join(deps.cfg.monitor_media_dir, file_rel)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, "wb") as fh:
        fh.write(b"x")
    await deps.store.set_media_path(owner.id, -1, 1, file_rel)
    deps.bot.sent.clear()
    event = SimpleNamespace(business_connection_id="conn-1",
                            chat=SimpleNamespace(id=-1), message_ids=ids)
    await capture.on_deleted_business_messages(event, deps, NOW)
    assert len(deps.bot.sent) == len(ids), "every message below the threshold gets its own send"
    assert len(deps.bot.documents) == 1, "the media attachment must still ride along"
    assert deps.bot.documents[0].path.endswith("1.jpg")


async def test_a_delete_at_the_bulk_threshold_sends_a_single_summary(deps):
    owner = await connect(deps)
    ids = list(range(1, notify.BULK_THRESHOLD + 1))  # exactly at the threshold
    for message_id in ids:
        await deps.store.record_message(owner.id, -1, message_id, 300, "Собеседник",
                                        f"сообщение {message_id}", None, None, NOW)
    deps.bot.sent.clear()
    event = SimpleNamespace(business_connection_id="conn-1",
                            chat=SimpleNamespace(id=-1), message_ids=ids)
    await capture.on_deleted_business_messages(event, deps, NOW)
    assert len(deps.bot.sent) == 1, "at the threshold, one summary replaces the individual sends"
    assert str(notify.BULK_THRESHOLD) in deps.bot.sent[0].text


async def test_a_long_deleted_message_is_delivered_within_the_telegram_limit(deps):
    owner = await connect(deps)
    await deps.store.record_message(owner.id, -1, 1, 300, "Собеседник", "я" * 5000,
                                    None, None, NOW)
    deps.bot.sent.clear()
    event = SimpleNamespace(business_connection_id="conn-1",
                            chat=SimpleNamespace(id=-1), message_ids=[1])
    await capture.on_deleted_business_messages(event, deps, NOW)
    assert len(deps.bot.sent) == 1
    assert len(deps.bot.sent[0].text) <= 4096


async def test_sent_at_uses_the_message_date_not_the_receive_time(deps):
    # An edit of a pre-connection message is captured for the first time here;
    # dating it "now" restarts its retention clock and misfiles it in the
    # archive by however long ago it was actually sent.
    owner = await connect(deps)
    sent_at = NOW - timedelta(days=3)
    msg = a_message(text="давнее")
    msg.date = sent_at
    await capture.on_edited_business_message(msg, deps, NOW)
    stored = await deps.store.get_message(owner.id, -1, 5)
    assert stored.sent_at.startswith("2026-07-27")


async def test_muted_owner_edit_is_journaled_with_zero_sends(deps):
    # notify_enabled=0 must silence delivery while journaling keeps running --
    # the archive (export, weekly backup) still needs a correct row.
    owner = await connect(deps)
    await capture.on_business_message(a_message(text="было"), deps, NOW)
    await deps.store.set_owner_flag(owner.id, "notify_enabled", 0)
    deps.bot.sent.clear()
    await capture.on_edited_business_message(a_message(text="стало"), deps, NOW)
    stored = await deps.store.get_message(owner.id, -1, 5)
    assert stored.text == "стало", "the journal must still be updated while muted"
    assert stored.edited_at is not None
    assert deps.bot.sent == []


async def test_muted_owner_delete_is_journaled_with_zero_sends(deps):
    owner = await connect(deps)
    await capture.on_business_message(a_message(text="секрет"), deps, NOW)
    await deps.store.set_owner_flag(owner.id, "notify_enabled", 0)
    deps.bot.sent.clear()
    event = SimpleNamespace(business_connection_id="conn-1",
                            chat=SimpleNamespace(id=-1), message_ids=[5])
    await capture.on_deleted_business_messages(event, deps, NOW)
    stored = await deps.store.get_message(owner.id, -1, 5)
    assert stored.deleted_at is not None
    assert deps.bot.sent == []


async def test_muted_owner_with_mirror_on_still_sends_nothing(deps):
    # "notifications off for this connection" must mean nothing is sent --
    # not "the owner is quiet but the admin still gets pinged".
    owner = await connect(deps)
    await capture.on_business_message(a_message(text="секрет"), deps, NOW)
    await deps.store.set_owner_flag(owner.id, "notify_enabled", 0)
    await deps.store.set_owner_flag(owner.id, "mirror_to_admin", 1)
    deps.bot.sent.clear()
    event = SimpleNamespace(business_connection_id="conn-1",
                            chat=SimpleNamespace(id=-1), message_ids=[5])
    await capture.on_deleted_business_messages(event, deps, NOW)
    assert deps.bot.sent == []


async def test_muted_owner_edit_with_mirror_on_still_sends_nothing(deps):
    owner = await connect(deps)
    await capture.on_business_message(a_message(text="было"), deps, NOW)
    await deps.store.set_owner_flag(owner.id, "notify_enabled", 0)
    await deps.store.set_owner_flag(owner.id, "mirror_to_admin", 1)
    deps.bot.sent.clear()
    await capture.on_edited_business_message(a_message(text="стало"), deps, NOW)
    assert deps.bot.sent == []
    assert (await deps.store.get_message(owner.id, -1, 5)).text == "стало"


async def test_notify_enabled_default_delivers_normally(deps):
    # notify_enabled defaults to 1 -- an ordinary, un-muted owner must see no
    # change in behaviour at all.
    owner = await connect(deps)
    assert owner.notify_enabled == 1
    await capture.on_business_message(a_message(text="было"), deps, NOW)
    deps.bot.sent.clear()
    await capture.on_edited_business_message(a_message(text="стало"), deps, NOW)
    assert len(deps.bot.sent) == 1
    assert (await deps.store.get_message(owner.id, -1, 5)).text == "стало"


async def test_owner_with_monitoring_off_still_journals_and_sends_nothing(deps):
    # monitor_enabled keeps its old meaning: nothing at all, journal included --
    # unlike notify_enabled, which only silences delivery.
    owner = await connect(deps)
    await capture.on_business_message(a_message(text="было"), deps, NOW)
    await deps.store.set_owner_flag(owner.id, "monitor_enabled", 0)
    deps.bot.sent.clear()
    event = SimpleNamespace(business_connection_id="conn-1",
                            chat=SimpleNamespace(id=-1), message_ids=[5])
    await capture.on_deleted_business_messages(event, deps, NOW)
    await capture.on_edited_business_message(a_message(text="стало"), deps, NOW)
    await capture.on_business_message(a_message(message_id=6, text="новое"), deps, NOW)
    assert (await deps.store.get_message(owner.id, -1, 5)).deleted_at is None
    assert (await deps.store.get_message(owner.id, -1, 5)).text == "было"
    assert await deps.store.get_message(owner.id, -1, 6) is None
    assert deps.bot.sent == []


async def test_a_bot_authored_message_is_not_journaled(deps):
    # The owner's private chat with this very bot is in scope of the business
    # connection, so the bot's own panel redraws arrive here as ordinary
    # business_message updates. They must never be journaled.
    owner = await connect(deps)
    await capture.on_business_message(
        a_message(text="Панель управления", user_id=999, name="Бот", is_bot=True), deps, NOW)
    assert await deps.store.get_message(owner.id, -1, 5) is None
    assert deps.bot.sent == []


async def test_a_bot_authored_edit_sends_nothing_and_creates_no_row(deps):
    owner = await connect(deps)
    await capture.on_edited_business_message(
        a_message(text="Панель v2", user_id=999, name="Бот", is_bot=True), deps, NOW)
    assert await deps.store.get_message(owner.id, -1, 5) is None
    assert deps.bot.sent == []


async def test_a_human_message_with_is_bot_false_is_journaled_and_notified_as_before(deps):
    # Regression guard: explicitly passing is_bot=False must behave exactly
    # like the pre-existing (implicit) behaviour.
    owner = await connect(deps)
    await capture.on_business_message(a_message(text="было", is_bot=False), deps, NOW)
    deps.bot.sent.clear()
    await capture.on_edited_business_message(a_message(text="стало", is_bot=False), deps, NOW)
    assert len(deps.bot.sent) == 1
    assert (await deps.store.get_message(owner.id, -1, 5)).text == "стало"


async def test_a_message_with_no_from_user_still_journals(deps):
    owner = await connect(deps)
    msg = SimpleNamespace(business_connection_id="conn-1", chat=SimpleNamespace(id=-1),
                          message_id=5, text="привет", caption=None, from_user=None,
                          photo=None, video=None, video_note=None, voice=None, document=None)
    await capture.on_business_message(msg, deps, NOW)
    stored = await deps.store.get_message(owner.id, -1, 5)
    assert stored is not None and stored.text == "привет"
    assert stored.from_user_id is None


async def test_a_bots_own_notification_reflected_back_is_not_rejournaled(deps):
    # The worst case: a notification this bot itself sent lands back in the
    # watched dialog (it's the owner's chat with the bot) and is redelivered
    # as a business_message. If that were ever journaled and then edited, it
    # would trigger another notification about a notification -- a feedback
    # loop. Pin that this can never start.
    owner = await connect(deps)
    loop_text = notify.edited_text("Бот", "some_bot", "было", "стало",
                                   datetime(2026, 7, 30, 13, 0))
    assert loop_text.startswith("✏️")
    await capture.on_business_message(
        a_message(text=loop_text, user_id=999, name="Бот", is_bot=True), deps, NOW)
    assert await deps.store.get_message(owner.id, -1, 5) is None
    assert deps.bot.sent == []


async def test_a_new_message_in_the_owners_control_chat_is_not_journaled(deps):
    # A private chat's id equals the other party's user id, so the owner's
    # own chat WITH THIS BOT is uniquely identified by chat_id ==
    # owner.owner_user_id. Nothing there is a real дialogue -- it's the
    # panel/admin surface -- so it must be skipped wholesale, independent of
    # the is_bot filter (this message is not itself marked as a bot).
    owner = await connect(deps)
    await capture.on_business_message(
        a_message(chat_id=owner.owner_user_id, text="Панель управления"), deps, NOW)
    assert await deps.store.get_message(owner.id, owner.owner_user_id, 5) is None
    assert deps.bot.sent == []


async def test_an_edit_in_the_owners_control_chat_sends_nothing(deps):
    owner = await connect(deps)
    await capture.on_edited_business_message(
        a_message(chat_id=owner.owner_user_id, text="Панель v2"), deps, NOW)
    assert await deps.store.get_message(owner.id, owner.owner_user_id, 5) is None
    assert deps.bot.sent == []


async def test_a_delete_in_the_owners_control_chat_sends_nothing(deps):
    # The finding this section exists to pin: since bot messages are never
    # journaled, a delete event for one used to fall into the "unknown id"
    # branch and notify "содержимое не сохранено (отправлено до подключения
    # бота)" -- wrong (the bot sent it seconds ago) and exactly the kind of
    # self-referential noise this whole change removes, just via the delete
    # path instead of the edit path.
    owner = await connect(deps)
    deps.bot.sent.clear()
    event = SimpleNamespace(business_connection_id="conn-1",
                            chat=SimpleNamespace(id=owner.owner_user_id), message_ids=[999])
    await capture.on_deleted_business_messages(event, deps, NOW)
    assert deps.bot.sent == []


async def test_a_bulk_delete_in_the_owners_control_chat_sends_nothing(deps):
    owner = await connect(deps)
    deps.bot.sent.clear()
    ids = list(range(1, notify.BULK_THRESHOLD + 5))
    event = SimpleNamespace(business_connection_id="conn-1",
                            chat=SimpleNamespace(id=owner.owner_user_id), message_ids=ids)
    await capture.on_deleted_business_messages(event, deps, NOW)
    assert deps.bot.sent == []


async def test_a_message_in_an_ordinary_dialog_is_still_journaled_and_notified(deps):
    # Regression guard for the control-chat filter: only the one chat whose
    # id matches the owner's own user id is special. Every other chat --
    # including a real собеседник -- must keep flowing exactly as before.
    owner = await connect(deps)
    assert owner.owner_user_id != -1
    await capture.on_business_message(a_message(chat_id=-1, text="было"), deps, NOW)
    deps.bot.sent.clear()
    await capture.on_edited_business_message(a_message(chat_id=-1, text="стало"), deps, NOW)
    assert len(deps.bot.sent) == 1
    assert (await deps.store.get_message(owner.id, -1, 5)).text == "стало"


async def test_owner_ones_control_chat_id_does_not_suppress_owner_twos_dialog(deps):
    # Each owner's control chat is scoped to THAT owner's own user id, not a
    # global constant -- owner two's real dialog with a contact whose user id
    # happens to equal owner one's id must not be swallowed by owner one's
    # filter.
    owner1 = await connect(deps, conn_id="conn-1", user_id=100)
    owner2 = await connect(deps, conn_id="conn-2", user_id=200)
    await capture.on_business_message(
        a_message(conn_id="conn-2", chat_id=owner1.owner_user_id, text="было"), deps, NOW)
    deps.bot.sent.clear()
    await capture.on_edited_business_message(
        a_message(conn_id="conn-2", chat_id=owner1.owner_user_id, text="стало"), deps, NOW)
    assert len(deps.bot.sent) == 1
    assert (await deps.store.get_message(owner2.id, owner1.owner_user_id, 5)).text == "стало"


async def test_a_broken_store_does_not_crash_the_connection_handler(deps, monkeypatch):
    async def boom(*a, **kw):
        raise RuntimeError("db exploded")
    monkeypatch.setattr(deps.store, "upsert_owner", boom)
    await capture.on_business_connection(a_connection(), deps, NOW)  # must not raise


async def test_a_broken_store_does_not_crash_the_message_handler(deps, monkeypatch):
    await connect(deps)
    async def boom(*a, **kw):
        raise RuntimeError("db exploded")
    monkeypatch.setattr(deps.store, "record_message", boom)
    await capture.on_business_message(a_message(), deps, NOW)  # must not raise


async def test_a_broken_store_does_not_crash_the_edit_handler(deps, monkeypatch):
    await connect(deps)
    await capture.on_business_message(a_message(text="было"), deps, NOW)
    async def boom(*a, **kw):
        raise RuntimeError("db exploded")
    monkeypatch.setattr(deps.store, "get_message", boom)
    await capture.on_edited_business_message(a_message(text="стало"), deps, NOW)  # must not raise


async def test_a_broken_store_does_not_crash_the_delete_handler(deps, monkeypatch):
    await connect(deps)
    await capture.on_business_message(a_message(), deps, NOW)
    async def boom(*a, **kw):
        raise RuntimeError("db exploded")
    monkeypatch.setattr(deps.store, "get_messages", boom)
    event = SimpleNamespace(business_connection_id="conn-1",
                            chat=SimpleNamespace(id=-1), message_ids=[5])
    await capture.on_deleted_business_messages(event, deps, NOW)  # must not raise
