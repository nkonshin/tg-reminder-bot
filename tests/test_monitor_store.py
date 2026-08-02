import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from src.config import Config
from src.monitor.store import MonitorStore


def make_config(**over):
    base = dict(bot_token="t", her_user_id=100, admin_user_id=200, _env_file=None)
    base.update(over)
    return Config(**base)


@pytest.fixture
async def store(tmp_path):
    s = MonitorStore(str(tmp_path / "t.sqlite3"))
    await s.init()
    return s


def test_monitor_settings_defaults():
    cfg = make_config()
    assert cfg.monitor_enabled is True
    assert cfg.monitor_retention_days == 30
    assert cfg.monitor_media_dir == "data/media"
    assert cfg.monitor_media_timeout_seconds == 30
    assert cfg.monitor_export_part_mb == 48
    assert cfg.monitor_cleanup_hour == 4


async def test_init_creates_both_tables(store):
    con = sqlite3.connect(store.path)
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"owners", "messages"} <= names


async def test_init_is_idempotent(store):
    await store.init()  # must not raise on a second run


async def test_init_migrates_an_older_messages_table(tmp_path):
    # A file created before a column existed must gain it, not blow up later.
    path = str(tmp_path / "old.sqlite3")
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE messages (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          owner_id INTEGER NOT NULL,
          chat_id INTEGER NOT NULL,
          message_id INTEGER NOT NULL,
          sent_at TEXT NOT NULL
        );
    """)
    con.commit()
    con.close()

    s = MonitorStore(path)
    await s.init()

    con = sqlite3.connect(path)
    cols = {r[1] for r in con.execute("PRAGMA table_info(messages)")}
    assert {"text", "media_kind", "media_path", "edited_at", "deleted_at",
           "from_name", "from_username"} <= cols


async def test_init_migrates_an_older_owners_table_and_defaults_notify_enabled_on(tmp_path):
    # A live owners row created before notify_enabled existed must gain the
    # column and default to "on" -- an existing connection must keep being
    # notified after the upgrade, not fall silent.
    path = str(tmp_path / "old_owners.sqlite3")
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE owners (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          business_connection_id TEXT NOT NULL UNIQUE,
          owner_user_id INTEGER NOT NULL,
          owner_name TEXT,
          is_enabled INTEGER NOT NULL DEFAULT 1,
          monitor_enabled INTEGER NOT NULL DEFAULT 1,
          mirror_to_admin INTEGER NOT NULL DEFAULT 0,
          retention_days INTEGER,
          log_photo INTEGER NOT NULL DEFAULT 0,
          log_video INTEGER NOT NULL DEFAULT 0,
          log_video_note INTEGER NOT NULL DEFAULT 0,
          log_voice INTEGER NOT NULL DEFAULT 0,
          log_document INTEGER NOT NULL DEFAULT 0,
          connected_at TEXT NOT NULL
        );
        INSERT INTO owners (business_connection_id, owner_user_id, owner_name, connected_at)
        VALUES ('conn-old', 100, 'Owner', '2026-07-01T00:00:00+00:00');
    """)
    con.commit()
    con.close()

    s = MonitorStore(path)
    await s.init()

    con = sqlite3.connect(path)
    cols = {r[1] for r in con.execute("PRAGMA table_info(owners)")}
    assert "notify_enabled" in cols
    row = con.execute("SELECT owner_user_id, notify_enabled FROM owners "
                      "WHERE business_connection_id='conn-old'").fetchone()
    assert row == (100, 1)  # the pre-existing row survives and stays notified


async def test_init_migrates_an_owners_table_missing_log_animation(tmp_path):
    # A live DB created before log_animation existed must gain the column and
    # default OFF -- an existing connection must not suddenly start
    # downloading gifs just because the process was upgraded.
    path = str(tmp_path / "old_no_animation.sqlite3")
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE owners (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          business_connection_id TEXT NOT NULL UNIQUE,
          owner_user_id INTEGER NOT NULL,
          owner_name TEXT,
          is_enabled INTEGER NOT NULL DEFAULT 1,
          monitor_enabled INTEGER NOT NULL DEFAULT 1,
          mirror_to_admin INTEGER NOT NULL DEFAULT 0,
          notify_enabled INTEGER NOT NULL DEFAULT 1,
          retention_days INTEGER,
          log_photo INTEGER NOT NULL DEFAULT 0,
          log_video INTEGER NOT NULL DEFAULT 0,
          log_video_note INTEGER NOT NULL DEFAULT 0,
          log_voice INTEGER NOT NULL DEFAULT 0,
          log_document INTEGER NOT NULL DEFAULT 0,
          connected_at TEXT NOT NULL
        );
        INSERT INTO owners (business_connection_id, owner_user_id, owner_name, connected_at)
        VALUES ('conn-old', 100, 'Owner', '2026-07-01T00:00:00+00:00');
    """)
    con.commit()
    con.close()

    s = MonitorStore(path)
    await s.init()

    con = sqlite3.connect(path)
    cols = {r[1] for r in con.execute("PRAGMA table_info(owners)")}
    assert "log_animation" in cols
    row = con.execute("SELECT log_animation FROM owners "
                      "WHERE business_connection_id='conn-old'").fetchone()
    assert row == (0,)  # must default off, not silently start downloading gifs


NOW = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)


async def make_owner(store, conn_id="conn-1", user_id=100):
    return await store.upsert_owner(conn_id, user_id, "Owner", True, NOW)


async def test_upsert_owner_creates_then_updates(store):
    o = await make_owner(store)
    assert o.owner_user_id == 100 and o.is_enabled == 1
    again = await store.upsert_owner("conn-1", 100, "Owner", False, NOW)
    assert again.id == o.id and again.is_enabled == 0
    assert len(await store.list_owners()) == 1


async def test_owner_toggle_roundtrip(store):
    o = await make_owner(store)
    assert o.log_photo == 0
    await store.set_owner_flag(o.id, "log_photo", 1)
    assert (await store.get_owner_by_id(o.id)).log_photo == 1


async def test_owner_animation_toggle_defaults_off_and_roundtrips(store):
    o = await make_owner(store)
    assert o.log_animation == 0
    await store.set_owner_flag(o.id, "log_animation", 1)
    assert (await store.get_owner_by_id(o.id)).log_animation == 1


async def test_record_and_read_message(store):
    o = await make_owner(store)
    await store.record_message(o.id, -1, 55, 100, "Кто-то", "привет", None, None, NOW)
    m = await store.get_message(o.id, -1, 55)
    assert m.text == "привет" and m.deleted_at is None
    assert m.from_username is None  # not passed above -- must not raise, must default


async def test_record_message_stores_the_username_when_given(store):
    o = await make_owner(store)
    await store.record_message(o.id, -1, 55, 100, "Кто-то", "привет", None, None, NOW,
                               from_username="someone")
    m = await store.get_message(o.id, -1, 55)
    assert m.from_username == "someone"


async def test_record_is_idempotent_on_the_same_key(store):
    o = await make_owner(store)
    await store.record_message(o.id, -1, 55, 100, "Кто-то", "первое", None, None, NOW)
    await store.record_message(o.id, -1, 55, 100, "Кто-то", "второе", None, None, NOW)
    assert (await store.get_message(o.id, -1, 55)).text == "первое"


async def test_two_owners_keep_separate_rows_for_one_chat(store):
    a = await make_owner(store, "conn-a", 100)
    b = await make_owner(store, "conn-b", 200)
    await store.record_message(a.id, -1, 55, 100, "A", "видит A", None, None, NOW)
    await store.record_message(b.id, -1, 55, 100, "A", "видит B", None, None, NOW)
    assert (await store.get_message(a.id, -1, 55)).text == "видит A"
    assert (await store.get_message(b.id, -1, 55)).text == "видит B"


async def test_mark_edited_and_deleted(store):
    o = await make_owner(store)
    await store.record_message(o.id, -1, 55, 100, "Кто-то", "было", None, None, NOW)
    await store.mark_edited(o.id, -1, 55, "стало", NOW + timedelta(minutes=1))
    m = await store.get_message(o.id, -1, 55)
    assert m.text == "стало" and m.edited_at is not None
    await store.mark_deleted(o.id, -1, 55, NOW + timedelta(minutes=2))
    assert (await store.get_message(o.id, -1, 55)).deleted_at is not None


async def test_get_messages_returns_only_known_ids(store):
    o = await make_owner(store)
    await store.record_message(o.id, -1, 55, 100, "Кто-то", "есть", None, None, NOW)
    found = await store.get_messages(o.id, -1, [55, 999])
    assert [m.message_id for m in found] == [55]


async def test_stats_counts_messages_and_kinds(store):
    o = await make_owner(store)
    await store.record_message(o.id, -1, 1, 100, "A", "два", None, None, NOW)
    await store.record_message(o.id, -1, 2, 100, "A", None, "photo", "p.jpg", NOW)
    old = NOW - timedelta(days=10)
    await store.record_message(o.id, -1, 3, 100, "A", "старое", None, None, old)
    total = await store.stats()
    assert total["messages"] == 3
    assert total["by_kind"]["photo"] == 1
    recent = await store.stats(since=NOW - timedelta(days=1))
    assert recent["messages"] == 2


async def test_stats_text_bytes_counts_utf8_bytes_not_characters(store):
    o = await make_owner(store)
    first, second = "привет", "мир"
    await store.record_message(o.id, -1, 1, 100, "A", first, None, None, NOW)
    await store.record_message(o.id, -1, 2, 100, "A", second, None, None, NOW)
    expected = len(first.encode("utf-8")) + len(second.encode("utf-8"))
    assert expected != len(first) + len(second)  # sanity: Cyrillic is multi-byte in UTF-8
    assert (await store.stats())["text_bytes"] == expected


async def test_upsert_owner_preserves_settings_on_reconnect(store):
    o = await make_owner(store)
    await store.set_owner_flag(o.id, "log_photo", 1)
    again = await store.upsert_owner("conn-1", 100, "Owner", True, NOW)
    assert again.log_photo == 1
    assert again.is_enabled == 1


async def test_owner_defaults_to_notifications_enabled(store):
    o = await make_owner(store)
    assert o.notify_enabled == 1


async def test_notify_enabled_toggle_roundtrip(store):
    o = await make_owner(store)
    await store.set_owner_flag(o.id, "notify_enabled", 0)
    assert (await store.get_owner_by_id(o.id)).notify_enabled == 0
    await store.set_owner_flag(o.id, "notify_enabled", 1)
    assert (await store.get_owner_by_id(o.id)).notify_enabled == 1


async def test_set_owner_flag_rejects_field_outside_toggleable(store):
    o = await make_owner(store)
    with pytest.raises(ValueError):
        await store.set_owner_flag(o.id, "owner_user_id", 999)
    assert (await store.get_owner_by_id(o.id)).owner_user_id == 100


async def test_get_messages_with_empty_ids_returns_empty_list(store):
    o = await make_owner(store)
    assert await store.get_messages(o.id, -1, []) == []
