import asyncio
import os
import sqlite3
import tarfile
import tempfile

from src.monitor import notify


def _snapshot_db(db_path: str, dest: str) -> None:
    """SQLite's backup API gives a consistent copy even while the bot writes."""
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(dest)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()


def build_archive(cfg, db_path: str, include_media: bool, out_dir: str, stamp: str) -> str:
    os.makedirs(out_dir, exist_ok=True)
    archive = os.path.join(out_dir, f"export-{stamp}.tar.gz")
    with tempfile.TemporaryDirectory() as tmp:
        snapshot = os.path.join(tmp, "messages.sqlite3")
        _snapshot_db(db_path, snapshot)
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(snapshot, arcname="messages.sqlite3")
            if include_media and os.path.isdir(cfg.monitor_media_dir):
                tar.add(cfg.monitor_media_dir, arcname="media")
    return archive


def split_file(path: str, part_bytes: int) -> list[str]:
    size = os.path.getsize(path)
    if size <= part_bytes:
        return [path]
    # Zero-pad wide enough that a shell glob's lexicographic sort ("part-100"
    # sorting before "part-11") still matches numeric order -- otherwise the
    # "cat part-*" rejoin command would silently reassemble the parts out of
    # order once there are more than 99 of them.
    total = -(-size // part_bytes)  # ceil division
    width = max(2, len(str(total)))
    parts = []
    with open(path, "rb") as src:
        for index in range(1, total + 1):
            chunk = src.read(part_bytes)
            part = f"{path}.part-{index:0{width}d}"
            with open(part, "wb") as out:
                out.write(chunk)
            parts.append(part)
    os.remove(path)
    return parts


async def send_export(deps, chat_id: int, include_media: bool, stamp: str) -> int:
    with tempfile.TemporaryDirectory() as out_dir:
        # The SQLite backup, tar, gzip and the split are blocking calls that
        # run for minutes once there are gigabytes of media on disk. Awaited
        # straight from the admin callback they would freeze the event loop --
        # polling, the reminder scheduler and every ping -- for that whole
        # time, so they go to a worker thread. (_snapshot_db is inside
        # build_archive and is covered by the same hop.)
        archive = await asyncio.to_thread(build_archive, deps.cfg, deps.store.path,
                                          include_media, out_dir, stamp)
        parts = await asyncio.to_thread(
            split_file, archive, deps.cfg.monitor_export_part_mb * 1024 * 1024)
        total = len(parts)
        for i, part in enumerate(parts, start=1):
            caption = notify.export_part_caption(i, total) if total > 1 else None
            await deps.bot.send_document(chat_id, notify.as_document(part),
                                         caption=caption)
        return total
