import os
from dataclasses import dataclass, fields
from datetime import datetime, timezone

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  day TEXT,
  due_at TEXT,
  status TEXT NOT NULL,
  calendar_uid TEXT,
  calendar_pending INTEGER NOT NULL DEFAULT 0,
  awaiting_manual_time INTEGER NOT NULL DEFAULT 0,
  pings_sent INTEGER NOT NULL DEFAULT 0,
  last_ping_at TEXT,
  created_at TEXT NOT NULL
);
"""


def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def from_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


@dataclass
class Reminder:
    id: int
    title: str
    day: str | None
    due_at: str | None
    status: str
    calendar_uid: str | None
    calendar_pending: int
    awaiting_manual_time: int
    pings_sent: int
    last_ping_at: str | None
    created_at: str


COLS = ", ".join(f.name for f in fields(Reminder))


class Database:
    def __init__(self, path: str):
        self.path = path

    async def init(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        async with aiosqlite.connect(self.path) as c:
            await c.execute(SCHEMA)
            await c.commit()

    async def _exec(self, sql: str, args: tuple = ()) -> int:
        async with aiosqlite.connect(self.path) as c:
            cur = await c.execute(sql, args)
            await c.commit()
            return cur.lastrowid

    async def _row(self, sql: str, args: tuple = ()) -> "Reminder | None":
        async with aiosqlite.connect(self.path) as c:
            cur = await c.execute(sql, args)
            row = await cur.fetchone()
            return Reminder(*row) if row else None

    async def add_pending(self, title: str, due: datetime) -> "Reminder":
        rid = await self._exec(
            "INSERT INTO reminders (title, due_at, status, created_at) VALUES (?,?,?,?)",
            (title, to_iso(due), "pending", to_iso(datetime.now(timezone.utc))))
        return await self.get(rid)

    async def add_clarify(self, title: str, day: str) -> "Reminder":
        rid = await self._exec(
            "INSERT INTO reminders (title, day, status, created_at) VALUES (?,?,?,?)",
            (title, day, "pending_clarify", to_iso(datetime.now(timezone.utc))))
        return await self.get(rid)

    async def get(self, rid: int) -> "Reminder | None":
        return await self._row(f"SELECT {COLS} FROM reminders WHERE id=?", (rid,))

    async def set_status(self, rid: int, status: str) -> None:
        await self._exec("UPDATE reminders SET status=? WHERE id=?", (status, rid))

    async def promote(self, rid: int, due: datetime) -> None:
        await self._exec(
            "UPDATE reminders SET status='pending', due_at=?, pings_sent=0, "
            "last_ping_at=NULL, awaiting_manual_time=0 WHERE id=?", (to_iso(due), rid))

    async def set_calendar(self, rid: int, uid: str | None, pending: int) -> None:
        await self._exec("UPDATE reminders SET calendar_uid=?, calendar_pending=? WHERE id=?",
                         (uid, pending, rid))

    async def set_awaiting_manual(self, rid: int) -> None:
        # Both updates run on a single connection/transaction so a concurrent
        # call can't interleave between "clear all" and "set this one" and
        # leave more than one row flagged.
        async with aiosqlite.connect(self.path) as c:
            await c.execute("UPDATE reminders SET awaiting_manual_time=0 WHERE awaiting_manual_time=1")
            await c.execute("UPDATE reminders SET awaiting_manual_time=1 WHERE id=?", (rid,))
            await c.commit()

    async def get_awaiting_manual(self) -> "Reminder | None":
        return await self._row(
            f"SELECT {COLS} FROM reminders WHERE awaiting_manual_time=1 "
            "AND status='pending_clarify' ORDER BY id DESC LIMIT 1")

    async def list_pending(self) -> list["Reminder"]:
        async with aiosqlite.connect(self.path) as c:
            cur = await c.execute(f"SELECT {COLS} FROM reminders WHERE status='pending'")
            return [Reminder(*row) for row in await cur.fetchall()]

    async def record_ping(self, rid: int, now: datetime) -> None:
        await self._exec("UPDATE reminders SET pings_sent=pings_sent+1, last_ping_at=? WHERE id=?",
                         (to_iso(now), rid))
