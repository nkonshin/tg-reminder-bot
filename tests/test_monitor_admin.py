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
    data = f"adm:toggle:{owner.id}:mirror_to_admin"
    await admin.handle_callback(data, deps, 200, NOW)
    assert (await deps.store.get_owner_by_id(owner.id)).mirror_to_admin == 1
    await admin.handle_callback(data, deps, 200, NOW)
    assert (await deps.store.get_owner_by_id(owner.id)).mirror_to_admin == 0


async def test_toggle_is_ignored_for_an_unknown_owner(deps):
    assert await admin.handle_callback("adm:toggle:9999:mirror_to_admin",
                                       deps, 200, NOW) is None


async def test_notify_toggle_flips_the_flag(deps):
    owner = (await deps.store.list_owners())[0]
    data = f"adm:toggle:{owner.id}:notify_enabled"
    await admin.handle_callback(data, deps, 200, NOW)
    assert (await deps.store.get_owner_by_id(owner.id)).notify_enabled == 0
    await admin.handle_callback(data, deps, 200, NOW)
    assert (await deps.store.get_owner_by_id(owner.id)).notify_enabled == 1


async def test_notify_toggle_redraws_the_owners_screen_not_media(deps):
    owner = (await deps.store.list_owners())[0]
    _text, kb = await admin.handle_callback(
        f"adm:toggle:{owner.id}:notify_enabled", deps, 200, NOW)
    # redrawn as the Мониторинг screen: still shows the mirror toggle,
    # not the media-download screen
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert f"adm:toggle:{owner.id}:mirror_to_admin" in data


async def test_toggle_still_rejects_a_field_outside_toggleable_after_adding_notify(deps):
    owner = (await deps.store.list_owners())[0]
    result = await admin.handle_callback(f"adm:toggle:{owner.id}:owner_name",
                                         deps, 200, NOW)
    assert result is None


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


async def test_the_panel_reports_the_configured_state_before_any_connection(empty_deps):
    # any() over an empty owner list is False, so the first /admin after a
    # deploy said "Мониторинг: выключен" while MONITOR_ENABLED was 1.
    text, _kb = await admin.open_panel(empty_deps, 200)
    assert "Мониторинг: включён" in text
    empty_deps.cfg.monitor_enabled = False
    text, _kb = await admin.open_panel(empty_deps, 200)
    assert "Мониторинг: выключен" in text


async def test_toggle_with_a_non_numeric_owner_id_is_ignored(deps):
    owner = (await deps.store.list_owners())[0]
    assert await admin.handle_callback("adm:toggle:abc:log_photo", deps, 200, NOW) is None
    assert (await deps.store.get_owner_by_id(owner.id)).log_photo == 0


async def test_retention_with_a_non_numeric_value_is_ignored(deps):
    assert await admin.handle_callback("adm:retention:notanumber", deps, 200, NOW) is None


async def test_mirror_with_a_non_numeric_owner_id_is_ignored(deps):
    assert await admin.handle_callback("adm:toggle:abc:mirror_to_admin",
                                       deps, 200, NOW) is None


def _callbacks(kb) -> list[str]:
    return [b.callback_data for row in kb.inline_keyboard for b in row]


@pytest.fixture
async def two_owner_deps(tmp_path):
    """The shape this design actually targets: two business connections."""
    store = MonitorStore(str(tmp_path / "two.sqlite3"))
    await store.init()
    cfg = Config(bot_token="t", her_user_id=100, admin_user_id=200,
                 monitor_media_dir=str(tmp_path / "media"), _env_file=None)
    await store.upsert_owner("conn-1", 100, "Первый", True, NOW)
    await store.upsert_owner("conn-2", 300, "Второй", True, NOW)
    return capture.MonitorDeps(bot=FakeBot(), store=store, cfg=cfg)


async def test_the_monitoring_screen_offers_both_toggles_for_every_owner(two_owner_deps):
    # The spec asks the Мониторинг screen for a monitoring switch AND the
    # mirror toggle per connection. monitor_enabled was whitelisted in
    # store.TOGGLEABLE but no button ever emitted it -- a dead capability.
    _text, kb = await admin.handle_callback("adm:owners", two_owner_deps, 200, NOW)
    data = _callbacks(kb)
    for owner in await two_owner_deps.store.list_owners():
        assert f"adm:toggle:{owner.id}:monitor_enabled" in data
        assert f"adm:toggle:{owner.id}:mirror_to_admin" in data


async def test_monitoring_toggle_pauses_that_owner_and_redraws_its_own_screen(two_owner_deps):
    second = (await two_owner_deps.store.list_owners())[1]
    _text, kb = await admin.handle_callback(
        f"adm:toggle:{second.id}:monitor_enabled", two_owner_deps, 200, NOW)
    assert (await two_owner_deps.store.get_owner_by_id(second.id)).monitor_enabled == 0
    # redrawn as the Мониторинг screen, not the Медиа one
    assert f"adm:toggle:{second.id}:mirror_to_admin" in _callbacks(kb)


async def test_the_media_screen_reaches_every_owner_not_just_the_first(two_owner_deps):
    # With `_first_owner`, the second connection's media toggles were simply
    # unreachable from the panel.
    owners = await two_owner_deps.store.list_owners()
    _text, kb = await admin.handle_callback("adm:media", two_owner_deps, 200, NOW)
    picker = _callbacks(kb)
    for owner in owners:
        assert f"adm:media:{owner.id}" in picker

    second = owners[1]
    text, kb = await admin.handle_callback(f"adm:media:{second.id}",
                                           two_owner_deps, 200, NOW)
    assert "Второй" in text
    assert f"adm:toggle:{second.id}:log_photo" in _callbacks(kb)

    await admin.handle_callback(f"adm:toggle:{second.id}:log_photo",
                                two_owner_deps, 200, NOW)
    assert (await two_owner_deps.store.get_owner_by_id(second.id)).log_photo == 1
    assert (await two_owner_deps.store.get_owner_by_id(owners[0].id)).log_photo == 0


async def test_the_media_screen_skips_the_picker_with_a_single_owner(deps):
    owner = (await deps.store.list_owners())[0]
    _text, kb = await admin.handle_callback("adm:media", deps, 200, NOW)
    assert f"adm:toggle:{owner.id}:log_photo" in _callbacks(kb)


async def test_media_for_an_unknown_owner_is_ignored(two_owner_deps):
    assert await admin.handle_callback("adm:media:9999", two_owner_deps, 200, NOW) is None
    assert await admin.handle_callback("adm:media:abc", two_owner_deps, 200, NOW) is None


async def test_the_retention_screen_says_it_applies_to_every_connection(two_owner_deps):
    text, _kb = await admin.handle_callback("adm:retention", two_owner_deps, 200, NOW)
    assert "всех подключений" in text and "2" in text


@pytest.mark.parametrize("days", [-1, -365, 3651, 100000])
async def test_retention_outside_the_allowed_range_is_ignored(deps, days):
    # A negative retention puts the sweep's cutoff in the FUTURE, so the next
    # nightly run deletes the entire journal.
    assert await admin.handle_callback(f"adm:retention:{days}", deps, 200, NOW) is None
    assert (await deps.store.list_owners())[0].retention_days is None


async def test_zero_day_retention_is_still_allowed(deps):
    # 0 means "delete immediately" and retention.sweep honours it explicitly.
    assert await admin.handle_callback("adm:retention:0", deps, 200, NOW) is not None
    assert (await deps.store.list_owners())[0].retention_days == 0
