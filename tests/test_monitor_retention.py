import os
from datetime import datetime, timedelta, timezone

import pytest

from src.config import Config
from src.monitor import capture, retention
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


async def test_sweep_removes_rows_older_than_retention(deps):
    owner = await deps.store.upsert_owner("c", 100, "O", True, NOW)
    await deps.store.record_message(owner.id, -1, 1, 100, "A", "старое", None, None,
                                    NOW - timedelta(days=40))
    await deps.store.record_message(owner.id, -1, 2, 100, "A", "свежее", None, None, NOW)
    result = await retention.sweep(deps, NOW)
    assert result["rows"] == 1
    assert await deps.store.get_message(owner.id, -1, 1) is None
    assert await deps.store.get_message(owner.id, -1, 2) is not None


async def test_sweep_honours_a_per_owner_retention(deps):
    owner = await deps.store.upsert_owner("c", 100, "O", True, NOW)
    await deps.store.set_retention_days(owner.id, 3)
    await deps.store.record_message(owner.id, -1, 1, 100, "A", "пять дней", None, None,
                                    NOW - timedelta(days=5))
    await retention.sweep(deps, NOW)
    assert await deps.store.get_message(owner.id, -1, 1) is None


async def test_sweep_deletes_the_media_file_of_a_removed_row(deps):
    owner = await deps.store.upsert_owner("c", 100, "O", True, NOW)
    rel = f"{owner.id}/-1/1.jpg"
    path = os.path.join(deps.cfg.monitor_media_dir, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "wb").write(b"x")
    await deps.store.record_message(owner.id, -1, 1, 100, "A", None, "photo", rel,
                                    NOW - timedelta(days=40))
    result = await retention.sweep(deps, NOW)
    assert result["files"] == 1
    assert not os.path.exists(path)


async def test_sweep_keeps_files_of_rows_that_stay(deps):
    owner = await deps.store.upsert_owner("c", 100, "O", True, NOW)
    rel = f"{owner.id}/-1/2.jpg"
    path = os.path.join(deps.cfg.monitor_media_dir, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "wb").write(b"x")
    await deps.store.record_message(owner.id, -1, 2, 100, "A", None, "photo", rel, NOW)
    await retention.sweep(deps, NOW)
    assert os.path.exists(path)


async def test_zero_day_owner_retention_is_not_treated_as_unset(deps):
    # Regression: `owner.retention_days or cfg.monitor_retention_days` treats
    # an explicit 0 (delete-immediately) the same as "not set", silently
    # falling back to the global default and keeping rows the owner asked to
    # purge right away.
    owner = await deps.store.upsert_owner("c", 100, "O", True, NOW)
    await deps.store.set_retention_days(owner.id, 0)
    await deps.store.record_message(owner.id, -1, 1, 100, "A", "только что", None, None,
                                    NOW - timedelta(minutes=5))
    result = await retention.sweep(deps, NOW)
    assert result["rows"] == 1
    assert await deps.store.get_message(owner.id, -1, 1) is None
