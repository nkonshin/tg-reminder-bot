import asyncio
import logging
import os

log = logging.getLogger(__name__)

# Extension per kind: Telegram re-encodes photos to JPEG, voice to OGG and
# video notes to MP4, so a fixed extension is accurate enough for an archive.
EXTENSIONS = {"photo": "jpg", "video": "mp4", "video_note": "mp4",
              "voice": "ogg", "document": "bin"}


def detect_kind(msg) -> tuple[str | None, str | None]:
    if getattr(msg, "photo", None):
        return "photo", msg.photo[-1].file_id  # last entry is the largest size
    for kind in ("video", "video_note", "voice", "document"):
        obj = getattr(msg, kind, None)
        if obj is not None:
            return kind, obj.file_id
    return None, None


def is_enabled_for(owner, kind: str) -> bool:
    return bool(getattr(owner, f"log_{kind}", 0))


def _abs_path(cfg, rel_path: str) -> str:
    return os.path.join(cfg.monitor_media_dir, rel_path)


async def download(bot, cfg, owner_id, chat_id, message_id, kind, file_id) -> str | None:
    """Fetch the file now — after the message is deleted the file_id may stop
    resolving, so there is no second chance. A failure is never fatal: the
    metadata row is written either way.

    Streams into a `.part` sibling and only `os.replace()`s it into place on
    success, so a failure partway through (a timeout cancelling the transfer
    mid-stream, a network cutoff) never leaves a truncated file at `dest` —
    dir_size() and anything that treats "file exists at the recorded path" as
    "download succeeded" would otherwise be fooled by the wreckage."""
    rel = f"{owner_id}/{chat_id}/{message_id}.{EXTENSIONS.get(kind, 'bin')}"
    dest = _abs_path(cfg, rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    try:
        await asyncio.wait_for(bot.download(file_id, destination=tmp),
                               timeout=cfg.monitor_media_timeout_seconds)
        os.replace(tmp, dest)
        return rel
    except Exception:
        log.warning("could not download %s for message %s", kind, message_id, exc_info=True)
        try:
            os.remove(tmp)
        except FileNotFoundError:
            pass
        return None


def remove_file(cfg, rel_path: str) -> None:
    try:
        os.remove(_abs_path(cfg, rel_path))
    except FileNotFoundError:
        pass
    except OSError:
        log.warning("could not remove media file %s", rel_path, exc_info=True)


def dir_size(cfg) -> int:
    total = 0
    for root, _dirs, files in os.walk(cfg.monitor_media_dir):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total
