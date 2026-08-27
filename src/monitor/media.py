import asyncio
import logging
import os

log = logging.getLogger(__name__)

# Extension per kind: Telegram re-encodes photos to JPEG, voice to OGG,
# video notes/animations to MP4 and audio to MP3, so a fixed extension is
# accurate enough for an archive. A kind with no entry has a format that
# varies per message (a sticker is .webp, .tgs or .webm) -- download() resolves
# its real extension from Telegram instead of guessing. venue/location/contact/
# poll/dice/story/game are never downloaded at all (see detect_kind).
EXTENSIONS = {"photo": "jpg", "video": "mp4", "video_note": "mp4",
              "voice": "ogg", "document": "bin", "animation": "mp4", "audio": "mp3"}


def detect_kind(msg) -> tuple[str | None, str | None]:
    if getattr(msg, "photo", None):
        return "photo", msg.photo[-1].file_id  # last entry is the largest size
    # Checked before the "document" branch below: some clients also surface a
    # GIF's animation as a document field, but `animation` is the accurate
    # kind and must win.
    if getattr(msg, "animation", None):
        return "animation", msg.animation.file_id
    for kind in ("video", "video_note", "voice", "document", "audio"):
        obj = getattr(msg, kind, None)
        if obj is not None:
            return kind, obj.file_id
    if getattr(msg, "sticker", None):
        return "sticker", msg.sticker.file_id
    # No downloadable file for these -- metadata only, so a later deletion can
    # still say what kind of message it was. `venue` is checked before
    # `location`: a venue message also carries a location field, but `venue`
    # is the more specific kind and must win.
    if getattr(msg, "venue", None):
        return "venue", None
    if getattr(msg, "location", None):
        return "location", None
    if getattr(msg, "contact", None):
        return "contact", None
    if getattr(msg, "poll", None):
        return "poll", None
    if getattr(msg, "dice", None):
        return "dice", None
    if getattr(msg, "story", None):
        return "story", None
    if getattr(msg, "game", None):
        return "game", None
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
    ext = EXTENSIONS.get(kind)
    target = file_id
    if ext is None:
        # Variable-format kind (a sticker can be .webp, .tgs or .webm): ask
        # Telegram for the real file_path and take its extension, so the archive
        # stores a file the viewer can actually open rather than a guessed one.
        # Reuse the resolved File as the download target so get_file isn't
        # called twice. A get_file failure falls back to a neutral extension.
        try:
            target = await bot.get_file(file_id)
            ext = os.path.splitext(target.file_path or "")[1].lstrip(".").lower() or "bin"
        except Exception:
            ext, target = "bin", file_id
    rel = f"{owner_id}/{chat_id}/{message_id}.{ext}"
    dest = _abs_path(cfg, rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    try:
        await asyncio.wait_for(bot.download(target, destination=tmp),
                               timeout=cfg.monitor_media_timeout_seconds)
        os.replace(tmp, dest)
        return rel
    except Exception:
        log.warning("could not download %s for message %s", kind, message_id, exc_info=True)
        try:
            os.remove(tmp)
        except Exception:
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
