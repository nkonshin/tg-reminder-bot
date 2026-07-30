from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.config import Config
from src.monitor import capture
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
              user_id=300, name="Собеседник", **media_fields):
    fields = dict(photo=None, video=None, video_note=None, voice=None, document=None)
    fields.update(media_fields)
    return SimpleNamespace(business_connection_id=conn_id,
                           chat=SimpleNamespace(id=chat_id),
                           message_id=message_id, text=text, caption=None,
                           from_user=SimpleNamespace(id=user_id, full_name=name),
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


async def test_edit_notifies_with_both_versions(deps):
    owner = await connect(deps)
    await capture.on_business_message(a_message(text="было"), deps, NOW)
    deps.bot.sent.clear()
    await capture.on_edited_business_message(a_message(text="стало"), deps, NOW)
    assert len(deps.bot.sent) == 1
    assert "было" in deps.bot.sent[0].text and "стало" in deps.bot.sent[0].text
    assert (await deps.store.get_message(owner.id, -1, 5)).text == "стало"


async def test_edit_with_unchanged_text_is_silent(deps):
    await connect(deps)
    await capture.on_business_message(a_message(text="привет"), deps, NOW)
    deps.bot.sent.clear()
    await capture.on_edited_business_message(a_message(text="привет"), deps, NOW)
    assert deps.bot.sent == []


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
