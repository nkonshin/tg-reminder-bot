import os
from dataclasses import dataclass, fields
from datetime import datetime

import aiosqlite

from src.db import to_iso

# Same pattern as src/db.py: one source of truth per table, used both to CREATE
# and to ALTER an existing file forward. Add columns here, never by hand-writing
# a migration, so init() can never drift from the current schema.
OWNER_COLUMNS = [
    ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
    ("business_connection_id", "TEXT NOT NULL UNIQUE"),
    ("owner_user_id", "INTEGER NOT NULL"),
    ("owner_name", "TEXT"),
    ("is_enabled", "INTEGER NOT NULL DEFAULT 1"),
    ("monitor_enabled", "INTEGER NOT NULL DEFAULT 1"),
    ("mirror_to_admin", "INTEGER NOT NULL DEFAULT 0"),
    ("retention_days", "INTEGER"),
    ("log_photo", "INTEGER NOT NULL DEFAULT 0"),
    ("log_video", "INTEGER NOT NULL DEFAULT 0"),
    ("log_video_note", "INTEGER NOT NULL DEFAULT 0"),
    ("log_voice", "INTEGER NOT NULL DEFAULT 0"),
    ("log_document", "INTEGER NOT NULL DEFAULT 0"),
    ("connected_at", "TEXT NOT NULL"),
]

MESSAGE_COLUMNS = [
    ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
    ("owner_id", "INTEGER NOT NULL"),
    ("chat_id", "INTEGER NOT NULL"),
    ("message_id", "INTEGER NOT NULL"),
    ("from_user_id", "INTEGER"),
    ("from_name", "TEXT"),
    ("text", "TEXT"),
    ("media_kind", "TEXT"),
    ("media_path", "TEXT"),
    ("sent_at", "TEXT NOT NULL"),
    ("edited_at", "TEXT"),
    ("deleted_at", "TEXT"),
]


@dataclass
class Owner:
    id: int
    business_connection_id: str
    owner_user_id: int
    owner_name: str | None
    is_enabled: int
    monitor_enabled: int
    mirror_to_admin: int
    retention_days: int | None
    log_photo: int
    log_video: int
    log_video_note: int
    log_voice: int
    log_document: int
    connected_at: str


@dataclass
class StoredMessage:
    id: int
    owner_id: int
    chat_id: int
    message_id: int
    from_user_id: int | None
    from_name: str | None
    text: str | None
    media_kind: str | None
    media_path: str | None
    sent_at: str
    edited_at: str | None
    deleted_at: str | None


OWNER_COLS = ", ".join(f.name for f in fields(Owner))
MESSAGE_COLS = ", ".join(f.name for f in fields(StoredMessage))

MEDIA_KINDS = ("photo", "video", "video_note", "voice", "document")

# Fields set_owner_flag() is allowed to write. The admin panel builds these
# callbacks from user-tapped buttons, so this whitelist is what stops a
# crafted callback from overwriting an arbitrary column.
TOGGLEABLE = ("monitor_enabled", "mirror_to_admin",
              "log_photo", "log_video", "log_video_note", "log_voice", "log_document")


def _create_sql(table: str, columns) -> str:
    body = ",\n  ".join(f"{name} {decl}" for name, decl in columns)
    return f"CREATE TABLE IF NOT EXISTS {table} (\n  {body}\n);"


class MonitorStore:
    """Owners and their message journal. Shares the SQLite file with the
    reminder tables, so it never opens its own database."""

    def __init__(self, path: str):
        self.path = path

    async def init(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        async with aiosqlite.connect(self.path) as c:
            await c.execute("PRAGMA journal_mode=WAL")
            await c.execute(_create_sql("owners", OWNER_COLUMNS))
            await c.execute(_create_sql("messages", MESSAGE_COLUMNS))
            await self._migrate(c, "owners", OWNER_COLUMNS)
            await self._migrate(c, "messages", MESSAGE_COLUMNS)
            await c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_messages_key "
                            "ON messages(owner_id, chat_id, message_id)")
            await c.execute("CREATE INDEX IF NOT EXISTS ix_messages_sent_at "
                            "ON messages(sent_at)")
            await c.commit()

    async def _migrate(self, c: aiosqlite.Connection, table: str, columns) -> None:
        cur = await c.execute(f"PRAGMA table_info({table})")
        existing = {row[1] for row in await cur.fetchall()}
        for name, decl in columns:
            if name in existing:
                continue
            # SQLite cannot ADD a NOT NULL column without a default, and cannot
            # ADD a UNIQUE column at all -- if a future column needs either, it
            # must ship with a DEFAULT and any uniqueness enforced via a
            # separate CREATE UNIQUE INDEX IF NOT EXISTS instead of an inline
            # constraint, the same way ix_messages_key is handled above.
            await c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    async def _row(self, sql: str, args: tuple, cls):
        async with aiosqlite.connect(self.path) as c:
            cur = await c.execute(sql, args)
            row = await cur.fetchone()
            return cls(*row) if row else None

    async def _exec(self, sql: str, args: tuple = ()) -> None:
        async with aiosqlite.connect(self.path) as c:
            await c.execute(sql, args)
            await c.commit()

    async def upsert_owner(self, business_connection_id, owner_user_id, owner_name,
                           is_enabled, now) -> Owner:
        # Re-connecting must not reset the owner's own settings, so only the
        # identity fields and the enabled flag are overwritten.
        await self._exec(
            "INSERT INTO owners (business_connection_id, owner_user_id, owner_name, "
            "is_enabled, connected_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(business_connection_id) DO UPDATE SET "
            "owner_user_id=excluded.owner_user_id, owner_name=excluded.owner_name, "
            "is_enabled=excluded.is_enabled",
            (business_connection_id, owner_user_id, owner_name, int(is_enabled), to_iso(now)))
        return await self.get_owner(business_connection_id)

    async def get_owner(self, business_connection_id: str) -> "Owner | None":
        return await self._row(
            f"SELECT {OWNER_COLS} FROM owners WHERE business_connection_id=?",
            (business_connection_id,), Owner)

    async def get_owner_by_id(self, owner_id: int) -> "Owner | None":
        return await self._row(f"SELECT {OWNER_COLS} FROM owners WHERE id=?",
                               (owner_id,), Owner)

    async def list_owners(self) -> list[Owner]:
        async with aiosqlite.connect(self.path) as c:
            cur = await c.execute(f"SELECT {OWNER_COLS} FROM owners ORDER BY id")
            return [Owner(*row) for row in await cur.fetchall()]

    async def set_owner_flag(self, owner_id: int, field: str, value: int) -> None:
        if field not in TOGGLEABLE:
            raise ValueError(f"not a toggleable field: {field}")
        await self._exec(f"UPDATE owners SET {field}=? WHERE id=?", (int(value), owner_id))

    async def set_retention_days(self, owner_id: int, days: int | None) -> None:
        await self._exec("UPDATE owners SET retention_days=? WHERE id=?", (days, owner_id))

    async def record_message(self, owner_id, chat_id, message_id, from_user_id,
                             from_name, text, media_kind, media_path, sent_at) -> None:
        # OR IGNORE: Telegram can redeliver an update; the first version wins so
        # a redelivery never overwrites an edit we already recorded.
        await self._exec(
            "INSERT OR IGNORE INTO messages (owner_id, chat_id, message_id, from_user_id, "
            "from_name, text, media_kind, media_path, sent_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (owner_id, chat_id, message_id, from_user_id, from_name, text,
             media_kind, media_path, to_iso(sent_at)))

    async def get_message(self, owner_id, chat_id, message_id) -> "StoredMessage | None":
        return await self._row(
            f"SELECT {MESSAGE_COLS} FROM messages "
            "WHERE owner_id=? AND chat_id=? AND message_id=?",
            (owner_id, chat_id, message_id), StoredMessage)

    async def get_messages(self, owner_id, chat_id, message_ids) -> list[StoredMessage]:
        if not message_ids:
            return []
        marks = ",".join("?" * len(message_ids))
        async with aiosqlite.connect(self.path) as c:
            cur = await c.execute(
                f"SELECT {MESSAGE_COLS} FROM messages "
                f"WHERE owner_id=? AND chat_id=? AND message_id IN ({marks})",
                (owner_id, chat_id, *message_ids))
            return [StoredMessage(*row) for row in await cur.fetchall()]

    async def mark_edited(self, owner_id, chat_id, message_id, text, now) -> None:
        await self._exec(
            "UPDATE messages SET text=?, edited_at=? "
            "WHERE owner_id=? AND chat_id=? AND message_id=?",
            (text, to_iso(now), owner_id, chat_id, message_id))

    async def mark_deleted(self, owner_id, chat_id, message_id, now) -> None:
        await self._exec(
            "UPDATE messages SET deleted_at=? "
            "WHERE owner_id=? AND chat_id=? AND message_id=?",
            (to_iso(now), owner_id, chat_id, message_id))

    async def set_media_path(self, owner_id, chat_id, message_id, path) -> None:
        await self._exec(
            "UPDATE messages SET media_path=? "
            "WHERE owner_id=? AND chat_id=? AND message_id=?",
            (path, owner_id, chat_id, message_id))

    async def stats(self, since: "datetime | None" = None) -> dict:
        where, args = "", ()
        if since is not None:
            where, args = " WHERE sent_at >= ?", (to_iso(since),)
        async with aiosqlite.connect(self.path) as c:
            # CAST ... AS BLOB forces byte-length semantics: SQLite's LENGTH()
            # on a TEXT value counts characters, which undercounts any
            # non-ASCII text (this bot's traffic is mostly Cyrillic).
            cur = await c.execute(
                f"SELECT COUNT(*), COALESCE(SUM(LENGTH(CAST(COALESCE(text,'') AS BLOB))),0) "
                f"FROM messages{where}", args)
            count, text_bytes = await cur.fetchone()
            cur = await c.execute(
                f"SELECT media_kind, COUNT(*) FROM messages{where}"
                f"{' AND' if where else ' WHERE'} media_kind IS NOT NULL GROUP BY media_kind",
                args)
            by_kind = {kind: n for kind, n in await cur.fetchall()}
        return {"messages": count, "text_bytes": text_bytes, "by_kind": by_kind}
