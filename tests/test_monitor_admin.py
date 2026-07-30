import threading
from datetime import datetime, timedelta, timezone

import pytest

from src.config import Config
from src.monitor import admin, capture
from src.monitor.store import MonitorStore
from tests.fakes import FakeBot

NOW = datetime(2026, 7, 30, 13, 0, tzinfo=timezone.utc)


@pytest.fixture
async def deps(tmp_path):
    store = MonitorStore(str(tmp_path / "t.sqlite3"))
    await store.init()
    cfg = Config(bot_token="t", her_user_id=100, admin_user_id=200,
                 monitor_media_dir=str(tmp_path / "media"), _env_file=None)
    d = capture.MonitorDeps(bot=FakeBot(), store=store, cfg=cfg)
    await store.upsert_owner("conn-1", 100, "Владелец", True, NOW)
    return d


async def test_panel_opens_only_for_the_admin(deps):
    assert await admin.open_panel(deps, 200) is not None
    assert await admin.open_panel(deps, 100) is None


async def test_media_toggle_flips_the_flag(deps):
    owner = (await deps.store.list_owners())[0]
    await admin.handle_callback(f"adm:toggle:{owner.id}:log_photo", deps, 200, NOW)
    assert (await deps.store.get_owner_by_id(owner.id)).log_photo == 1
    await admin.handle_callback(f"adm:toggle:{owner.id}:log_photo", deps, 200, NOW)
    assert (await deps.store.get_owner_by_id(owner.id)).log_photo == 0


async def test_toggle_is_ignored_for_a_non_admin(deps):
    owner = (await deps.store.list_owners())[0]
    assert await admin.handle_callback(f"adm:toggle:{owner.id}:log_photo", deps, 100, NOW) is None
    assert (await deps.store.get_owner_by_id(owner.id)).log_photo == 0


async def test_toggle_rejects_a_field_that_is_not_toggleable(deps):
    owner = (await deps.store.list_owners())[0]
    result = await admin.handle_callback(f"adm:toggle:{owner.id}:owner_user_id",
                                         deps, 200, NOW)
    assert result is None
    assert (await deps.store.get_owner_by_id(owner.id)).owner_user_id == 100


async def test_retention_change_is_stored(deps):
    await admin.handle_callback("adm:retention:7", deps, 200, NOW)
    owner = (await deps.store.list_owners())[0]
    assert owner.retention_days == 7


async def test_storage_screen_reports_counts(deps):
    owner = (await deps.store.list_owners())[0]
    await deps.store.record_message(owner.id, -1, 1, 100, "A", "привет", None, None, NOW)
    await deps.store.record_message(owner.id, -1, 2, 100, "A", None, "photo", None,
                                    NOW - timedelta(days=10))
    text, _kb = await admin.handle_callback("adm:storage", deps, 200, NOW)
    assert "2" in text  # total messages


async def test_storage_screen_measures_the_media_dir_off_the_event_loop(deps, monkeypatch):
    # dir_size() recursively stats every downloaded file; on the event loop
    # that stalls polling and the reminder scheduler for as long as it takes.
    loop_thread = threading.get_ident()
    seen = {}

    def dir_size(cfg):
        seen["thread"] = threading.get_ident()
        return 0

    monkeypatch.setattr(admin.media, "dir_size", dir_size)
    await admin.handle_callback("adm:storage", deps, 200, NOW)
    assert seen["thread"] != loop_thread, "dir_size ran on the event loop"


async def test_unknown_callback_is_ignored(deps):
    assert await admin.handle_callback("adm:nonsense", deps, 200, NOW) is None


async def test_owners_screen_lists_the_connection(deps):
    text, _kb = await admin.handle_callback("adm:owners", deps, 200, NOW)
    assert "Владелец" in text


async def test_mirror_toggle_flips_the_flag(deps):
    owner = (await deps.store.list_owners())[0]
    await admin.handle_callback(f"adm:mirror:{owner.id}", deps, 200, NOW)
    assert (await deps.store.get_owner_by_id(owner.id)).mirror_to_admin == 1
    await admin.handle_callback(f"adm:mirror:{owner.id}", deps, 200, NOW)
    assert (await deps.store.get_owner_by_id(owner.id)).mirror_to_admin == 0


async def test_mirror_toggle_is_ignored_for_an_unknown_owner(deps):
    assert await admin.handle_callback("adm:mirror:9999", deps, 200, NOW) is None


@pytest.fixture
async def empty_deps(tmp_path):
    store = MonitorStore(str(tmp_path / "empty.sqlite3"))
    await store.init()
    cfg = Config(bot_token="t", her_user_id=100, admin_user_id=200,
                 monitor_media_dir=str(tmp_path / "media"), _env_file=None)
    return capture.MonitorDeps(bot=FakeBot(), store=store, cfg=cfg)


async def test_screens_do_not_crash_with_zero_owners(empty_deps):
    assert await admin.open_panel(empty_deps, 200) is not None
    for data in ("adm:main", "adm:media", "adm:storage", "adm:retention",
                 "adm:owners", "adm:export"):
        result = await admin.handle_callback(data, empty_deps, 200, NOW)
        assert result is not None


async def test_toggle_with_a_non_numeric_owner_id_is_ignored(deps):
    owner = (await deps.store.list_owners())[0]
    assert await admin.handle_callback("adm:toggle:abc:log_photo", deps, 200, NOW) is None
    assert (await deps.store.get_owner_by_id(owner.id)).log_photo == 0


async def test_retention_with_a_non_numeric_value_is_ignored(deps):
    assert await admin.handle_callback("adm:retention:notanumber", deps, 200, NOW) is None


async def test_mirror_with_a_non_numeric_owner_id_is_ignored(deps):
    assert await admin.handle_callback("adm:mirror:abc", deps, 200, NOW) is None
