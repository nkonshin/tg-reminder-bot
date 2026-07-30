from types import SimpleNamespace

import pytest

from src.config import Config
from src.monitor import media
from src.monitor.store import MonitorStore
from tests.fakes import FakeBot


def cfg_for(tmp_path):
    return Config(bot_token="t", her_user_id=100, admin_user_id=200,
                  monitor_media_dir=str(tmp_path / "media"), _env_file=None)


def test_detect_kind_reads_the_biggest_photo():
    msg = SimpleNamespace(photo=[SimpleNamespace(file_id="small"),
                                 SimpleNamespace(file_id="big")],
                          video=None, video_note=None, voice=None, document=None)
    assert media.detect_kind(msg) == ("photo", "big")


def test_detect_kind_handles_a_voice_message():
    msg = SimpleNamespace(photo=None, video=None, video_note=None,
                          voice=SimpleNamespace(file_id="v1"), document=None)
    assert media.detect_kind(msg) == ("voice", "v1")


def test_detect_kind_returns_nothing_for_plain_text():
    msg = SimpleNamespace(photo=None, video=None, video_note=None, voice=None, document=None)
    assert media.detect_kind(msg) == (None, None)


@pytest.fixture
async def owner(tmp_path):
    s = MonitorStore(str(tmp_path / "t.sqlite3"))
    await s.init()
    from datetime import datetime, timezone
    return await s.upsert_owner("c", 100, "O", True, datetime(2026, 7, 30, tzinfo=timezone.utc))


def test_is_enabled_for_is_off_by_default(owner):
    assert media.is_enabled_for(owner, "photo") is False


async def test_download_writes_a_file_and_returns_its_relative_path(tmp_path):
    cfg = cfg_for(tmp_path)
    bot = FakeBot()
    rel = await media.download(bot, cfg, 1, -1, 55, "photo", "file-1")
    assert rel == "1/-1/55.jpg"
    assert (tmp_path / "media" / rel).read_bytes() == b"fake-bytes"


async def test_download_returns_none_when_telegram_fails(tmp_path):
    cfg = cfg_for(tmp_path)
    bot = FakeBot()
    bot.fail_download = True
    assert await media.download(bot, cfg, 1, -1, 55, "photo", "file-1") is None


async def test_remove_file_is_quiet_when_it_is_already_gone(tmp_path):
    media.remove_file(cfg_for(tmp_path), "1/-1/55.jpg")  # must not raise


async def test_download_leaves_no_partial_file_when_the_transfer_fails(tmp_path):
    cfg = cfg_for(tmp_path)
    bot = FakeBot()
    bot.fail_download = True
    bot.partial_write_then_fail = True
    rel = await media.download(bot, cfg, 1, -1, 55, "photo", "file-1")
    assert rel is None
    assert not (tmp_path / "media" / "1" / "-1" / "55.jpg").exists()
    assert not (tmp_path / "media" / "1" / "-1" / "55.jpg.part").exists()
