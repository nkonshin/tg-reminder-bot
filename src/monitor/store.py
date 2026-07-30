import os
from dataclasses import dataclass, fields

import aiosqlite

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
