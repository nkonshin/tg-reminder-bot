import sqlite3

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
    assert {"text", "media_kind", "media_path", "edited_at", "deleted_at", "from_name"} <= cols
