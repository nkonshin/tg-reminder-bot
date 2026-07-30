import os
import tarfile
import threading
from datetime import datetime, timezone

import pytest

from src.config import Config
from src.monitor import capture, export
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
    owner = await store.upsert_owner("conn-1", 100, "Владелец", True, NOW)
    await store.record_message(owner.id, -1, 1, 100, "A", "привет", None, None, NOW)
    os.makedirs(os.path.join(cfg.monitor_media_dir, str(owner.id)), exist_ok=True)
    with open(os.path.join(cfg.monitor_media_dir, str(owner.id), "1.jpg"), "wb") as fh:
        fh.write(b"x" * 1000)
    return d


def test_archive_with_text_only_has_no_media(deps, tmp_path):
    path = export.build_archive(deps.cfg, deps.store.path, False, str(tmp_path), "20260730")
    with tarfile.open(path) as tar:
        names = tar.getnames()
    assert any(n.endswith(".sqlite3") for n in names)
    assert not any(".jpg" in n for n in names)


def test_archive_with_media_includes_the_files(deps, tmp_path):
    path = export.build_archive(deps.cfg, deps.store.path, True, str(tmp_path), "20260730")
    with tarfile.open(path) as tar:
        names = tar.getnames()
    assert any(n.endswith(".jpg") for n in names)


def test_a_media_file_that_vanishes_mid_archive_is_skipped_not_fatal(deps, tmp_path, monkeypatch):
    # build_archive now runs in a worker thread (send_export), which removed
    # the serialisation that used to keep the media tree static while
    # archiving. tarfile.add(directory) lists a directory and then lstats
    # each entry; media.download's os.replace(tmp, dest) and the sweeper's
    # media.remove_file can both delete a file in that window. Reproduce the
    # race deterministically: remove the second file the instant tar.add is
    # about to lstat it, simulating "present at listing time, gone by the
    # time we get to it." The archive must still complete and keep the
    # survivor -- not abort and lose everything, including files that were
    # already added.
    # deps fixture already wrote "<owner_id>/1.jpg"; add a second file that
    # will vanish, alphabetically after the first so it is reached second.
    owners_dir = deps.cfg.monitor_media_dir
    owner_subdir = next(d for d in os.listdir(owners_dir)
                        if os.path.isdir(os.path.join(owners_dir, d)))
    victim = os.path.join(owners_dir, owner_subdir, "2.jpg")
    with open(victim, "wb") as fh:
        fh.write(b"y" * 10)

    real_add = tarfile.TarFile.add

    def flaky_add(self, name, arcname=None, recursive=True, **kw):
        if os.path.basename(name) == "2.jpg":
            os.remove(name)  # gone by the time tar.add would lstat it
        return real_add(self, name, arcname, recursive, **kw)

    monkeypatch.setattr(tarfile.TarFile, "add", flaky_add)

    path = export.build_archive(deps.cfg, deps.store.path, True, str(tmp_path), "20260730")

    with tarfile.open(path) as tar:
        names = tar.getnames()
    assert any(n.endswith("1.jpg") for n in names), "the survivor must still be archived"
    assert not any(n.endswith("2.jpg") for n in names), "the vanished file must be skipped"
    assert any(n.endswith(".sqlite3") for n in names)


def test_small_file_is_not_split(tmp_path):
    p = tmp_path / "a.tar.gz"
    p.write_bytes(b"x" * 100)
    assert export.split_file(str(p), 1024) == [str(p)]


def test_big_file_is_split_into_parts_that_rejoin(tmp_path):
    p = tmp_path / "a.tar.gz"
    original = bytes(range(256)) * 20  # 5120 bytes
    p.write_bytes(original)
    parts = export.split_file(str(p), 1024)
    assert len(parts) == 5
    rejoined = b"".join(open(part, "rb").read() for part in parts)
    assert rejoined == original


def test_split_pads_part_numbers_so_a_glob_sort_matches_order(tmp_path):
    # Regression: the rejoin caption tells the user to run
    # `cat export-*.tar.gz.part-*`, which relies on the shell's lexicographic
    # glob sort. With plain %02d numbering, more than 99 parts would put
    # "part-100" between "part-1" and "part-11" and rejoin them out of order.
    p = tmp_path / "a.tar.gz"
    original = bytes(range(105))  # 105 bytes, one byte per part
    p.write_bytes(original)
    parts = export.split_file(str(p), 1)
    assert len(parts) == 105
    rejoined = b"".join(open(part, "rb").read() for part in sorted(parts))
    assert rejoined == original


async def test_send_export_delivers_every_part(deps):
    sent = await export.send_export(deps, 200, False, "20260730")
    assert sent >= 1
    assert len(deps.bot.documents) == sent
    assert deps.bot.documents[0].chat_id == 200


async def test_send_export_does_its_blocking_work_off_the_event_loop(deps, monkeypatch):
    # The SQLite backup, tar, gzip and the split all block. Awaited straight
    # from the admin callback, one "Текст + медиа" press with a few GB on disk
    # freezes polling, the scheduler and every reminder ping for the duration.
    loop_thread = threading.get_ident()
    seen = {}
    real_build, real_split = export.build_archive, export.split_file

    def build(*a, **kw):
        seen["build"] = threading.get_ident()
        return real_build(*a, **kw)

    def split(*a, **kw):
        seen["split"] = threading.get_ident()
        return real_split(*a, **kw)

    monkeypatch.setattr(export, "build_archive", build)
    monkeypatch.setattr(export, "split_file", split)

    await export.send_export(deps, 200, True, "20260730")

    assert seen["build"] != loop_thread, "build_archive ran on the event loop"
    assert seen["split"] != loop_thread, "split_file ran on the event loop"


async def test_send_export_captions_multiple_parts_with_a_rejoin_hint(deps):
    # None of the other send_export tests produce more than one part, so this
    # covers the actual multi-part path: every part captioned "N/total", and
    # the rejoin command only on the first one.
    owner = (await deps.store.list_owners())[0]
    big_path = os.path.join(deps.cfg.monitor_media_dir, str(owner.id), "1.jpg")
    with open(big_path, "wb") as fh:
        fh.write(os.urandom(1_500_000))  # incompressible, so gzip won't shrink it below 1 MB
    deps.cfg.monitor_export_part_mb = 1

    sent = await export.send_export(deps, 200, True, "20260730")

    assert sent == 2
    docs = deps.bot.documents
    assert all(d.chat_id == 200 for d in docs)
    assert docs[0].caption.startswith("Часть 1/2")
    assert "cat export-*.tar.gz.part-* > export.tar.gz" in docs[0].caption
    assert docs[1].caption.startswith("Часть 2/2")
    assert "cat " not in docs[1].caption
