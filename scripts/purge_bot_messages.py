"""Delete already-journaled bot-authored rows from the monitor's message
journal, and the media files that belong to them.

The owner's private chat with a bot falls inside a business connection's
scope, so a bot's own messages (panel redraws, its own notifications, ...)
used to be journaled back as if a real собеседник had sent them --
src/monitor/capture.py now filters that out for every *new* message, but
this cleans up rows written before that filter existed.

Usage:
    python scripts/purge_bot_messages.py BOT_USER_ID              # dry run
    python scripts/purge_bot_messages.py BOT_USER_ID --apply      # deletes

BOT_USER_ID is a required argument, never hardcoded here: this repository is
public and the production bot's Telegram user id must not appear in it. Find
it via the bot's own getMe response (or e.g. @userinfobot) and pass it on the
command line.

By default this only reports what WOULD be deleted (a dry run). Pass --apply
to actually delete the rows and their media files. This script never points
itself at a database on its own initiative -- point it at a copy first if
you are not sure, and never at the live production database without having
decided that deliberately.
"""
import argparse
import asyncio
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

# Running this file directly (as documented above) puts scripts/ on
# sys.path[0], not the project root, so `import src...` fails with
# ModuleNotFoundError unless the root is added explicitly first.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiosqlite

from src.monitor import media
from src.monitor.store import MESSAGE_COLS, StoredMessage

EXAMPLES_SHOWN = 5


async def _total_rows(db_path: str) -> int:
    async with aiosqlite.connect(db_path) as c:
        cur = await c.execute("SELECT COUNT(*) FROM messages")
        (count,) = await cur.fetchone()
        return count


async def _matching(db_path: str, bot_user_id: int) -> list[StoredMessage]:
    async with aiosqlite.connect(db_path) as c:
        cur = await c.execute(
            f"SELECT {MESSAGE_COLS} FROM messages WHERE from_user_id=?", (bot_user_id,))
        return [StoredMessage(*row) for row in await cur.fetchall()]


async def _delete(db_path: str, bot_user_id: int) -> int:
    async with aiosqlite.connect(db_path) as c:
        cur = await c.execute("DELETE FROM messages WHERE from_user_id=?", (bot_user_id,))
        await c.commit()
        return cur.rowcount or 0


def _describe(row: StoredMessage) -> str:
    body = (row.text or f"<{row.media_kind or 'no text/media'}>").replace("\n", " ")
    return f"  id={row.id} chat={row.chat_id} msg={row.message_id}: {body[:80]!r}"


def _resolve_paths(db_arg: str | None, media_dir_arg: str | None) -> tuple[str, str]:
    """Only touch Config -- which needs a real .env with production secrets --
    when the caller did not override both --db and --media-dir. That keeps
    this script usable against a throwaway database with nothing production
    nearby."""
    if db_arg is not None and media_dir_arg is not None:
        return db_arg, media_dir_arg
    from src.config import Config
    cfg = Config()
    return db_arg or cfg.db_path, media_dir_arg or cfg.monitor_media_dir


async def run(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "bot_user_id", type=int,
        help="Telegram user id of the bot whose journaled messages to purge")
    parser.add_argument(
        "--apply", action="store_true",
        help="actually delete the matching rows and their media files "
             "(default: dry run -- report only, delete nothing)")
    parser.add_argument(
        "--db", default=None,
        help="path to the sqlite database (default: Config().db_path)")
    parser.add_argument(
        "--media-dir", default=None,
        help="path to the media directory (default: Config().monitor_media_dir)")
    args = parser.parse_args(argv)

    db_path, media_dir = _resolve_paths(args.db, args.media_dir)

    before = await _total_rows(db_path)
    matches = await _matching(db_path, args.bot_user_id)

    print(f"database: {db_path}")
    print(f"rows before: {before}")
    print(f"rows matching bot user id {args.bot_user_id}: {len(matches)}")
    if matches:
        print(f"examples (up to {EXAMPLES_SHOWN}):")
        for row in matches[:EXAMPLES_SHOWN]:
            print(_describe(row))

    if not args.apply:
        print("dry run -- nothing deleted. Re-run with --apply to delete.")
        return 0

    if not matches:
        print("nothing to delete.")
        return 0

    media_cfg = SimpleNamespace(monitor_media_dir=media_dir)
    removed_files = 0
    for row in matches:
        if row.media_path:
            media.remove_file(media_cfg, row.media_path)
            removed_files += 1

    deleted = await _delete(db_path, args.bot_user_id)
    after = await _total_rows(db_path)
    print(f"deleted rows: {deleted}")
    print(f"deleted media files: {removed_files}")
    print(f"rows after: {after}")
    return 0


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()
