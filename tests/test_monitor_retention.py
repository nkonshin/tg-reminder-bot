import asyncio
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from src.config import Config
from src.monitor import capture, retention
from src.monitor.store import MonitorStore
from tests.fakes import FakeBot

NOW = datetime(2026, 7, 30, 13, 0, tzinfo=timezone.utc)
TZ = ZoneInfo("Asia/Yekaterinburg")


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


def _drive_the_loop(monkeypatch, clock):
    """Replace run_sweeper's two sources of real time: the clock it checks and
    the sleep between checks. The fake sleep ends the loop after the last tick
    by raising CancelledError -- a BaseException, so run_sweeper's own
    `except Exception` (the thing under test in one of these) cannot swallow
    the test's way out of a `while True`."""
    from src import flow

    times = iter(clock)
    monkeypatch.setattr(flow, "now_local", lambda _cfg: next(times))
    remaining = len(clock)

    async def stop_after_the_last_tick(_seconds):
        nonlocal remaining
        remaining -= 1
        if remaining <= 0:
            raise asyncio.CancelledError

    monkeypatch.setattr(retention, "asyncio",
                        SimpleNamespace(sleep=stop_after_the_last_tick))


async def test_run_sweeper_sweeps_once_per_cleanup_hour(deps, monkeypatch):
    # Checking every ten minutes means the cleanup hour is seen six times in a
    # row; only the first of them may sweep, and the next day must sweep again.
    swept = []

    async def fake_sweep(_deps, now):
        swept.append(now)
        return {"rows": 0, "files": 0}

    monkeypatch.setattr(retention, "sweep", fake_sweep)
    _drive_the_loop(monkeypatch, [
        datetime(2026, 7, 30, 3, 50, tzinfo=TZ),   # before the hour
        datetime(2026, 7, 30, 4, 0, tzinfo=TZ),    # sweep
        datetime(2026, 7, 30, 4, 10, tzinfo=TZ),   # same hour, same day
        datetime(2026, 7, 30, 4, 50, tzinfo=TZ),   # ditto
        datetime(2026, 7, 30, 19, 0, tzinfo=TZ),   # later that day
        datetime(2026, 7, 31, 4, 5, tzinfo=TZ),    # sweep again, new day
    ])

    with pytest.raises(asyncio.CancelledError):
        await retention.run_sweeper(deps)

    assert [t.day for t in swept] == [30, 31]


async def test_a_failing_sweep_does_not_kill_the_loop(deps, monkeypatch):
    # The sweeper is a bare `while True` in the same process as the reminder
    # scheduler. If one bad night killed the task, retention would silently
    # stop forever and the server would grow without bound until someone
    # noticed the disk.
    attempts = []

    async def boom(_deps, now):
        attempts.append(now)
        raise RuntimeError("database is locked")

    monkeypatch.setattr(retention, "sweep", boom)
    _drive_the_loop(monkeypatch, [
        datetime(2026, 7, 30, 4, 0, tzinfo=TZ),
        datetime(2026, 7, 30, 12, 0, tzinfo=TZ),
        datetime(2026, 7, 31, 4, 0, tzinfo=TZ),
    ])

    with pytest.raises(asyncio.CancelledError):
        await retention.run_sweeper(deps)

    assert len(attempts) == 2, "the loop must keep trying on the following days"
