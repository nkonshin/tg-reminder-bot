import os
from dataclasses import dataclass, fields
from datetime import datetime, timedelta, timezone

import aiosqlite

# Single source of truth for the reminders table's columns, used both to
# build the initial CREATE TABLE and to migrate an existing (older) database
# file forward -- see Database._migrate. Add new columns here, not just in a
# hand-written ALTER TABLE, so init() never drifts from the current schema.
COLUMN_DEFS = [
    ("id", "INTEGER PRIMARY KEY AUTOINCREMENT"),
    ("title", "TEXT NOT NULL"),
    ("day", "TEXT"),
    ("due_at", "TEXT"),
    ("status", "TEXT NOT NULL"),
    ("calendar_uid", "TEXT"),
    ("calendar_pending", "INTEGER NOT NULL DEFAULT 0"),
    ("calendar_attempts", "INTEGER NOT NULL DEFAULT 0"),
    ("calendar_last_attempt_at", "TEXT"),
    ("awaiting_manual_time", "INTEGER NOT NULL DEFAULT 0"),
    ("awaiting_manual_since", "TEXT"),
    ("pings_sent", "INTEGER NOT NULL DEFAULT 0"),
    ("last_ping_at", "TEXT"),
    ("delivery_failures", "INTEGER NOT NULL DEFAULT 0"),
    ("created_at", "TEXT NOT NULL"),
]

SCHEMA = "CREATE TABLE IF NOT EXISTS reminders (\n  " + ",\n  ".join(
    f"{name} {decl}" for name, decl in COLUMN_DEFS) + "\n);"


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
    calendar_attempts: int
    calendar_last_attempt_at: str | None
    awaiting_manual_time: int
    awaiting_manual_since: str | None
    pings_sent: int
    last_ping_at: str | None
    delivery_failures: int
    created_at: str


COLS = ", ".join(f.name for f in fields(Reminder))


class Database:
    def __init__(self, path: str):
        self.path = path

    async def init(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        async with aiosqlite.connect(self.path) as c:
            # Insurance against handler/scheduler write collisions: both hit
            # this same file concurrently (aiogram's update handling and the
            # scheduler loop are separate asyncio tasks in the same process).
            await c.execute("PRAGMA journal_mode=WAL")
            # CREATE TABLE IF NOT EXISTS is a no-op against a database file
            # created by an older version of this schema -- it does NOT add
            # columns to an existing table. Without the migration below, every
            # query against such a file would fail with "no such column" the
            # moment it touched a column added after the file was created.
            await c.execute(SCHEMA)
            await self._migrate(c)
            await c.commit()

    async def _migrate(self, c: aiosqlite.Connection) -> None:
        cur = await c.execute("PRAGMA table_info(reminders)")
        existing = {row[1] for row in await cur.fetchall()}
        for name, decl in COLUMN_DEFS:
            if name in existing:
                continue
            await c.execute(f"ALTER TABLE reminders ADD COLUMN {name} {decl}")

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
        # calendar_pending starts at 1: the row exists before its calendar event
        # does, so a crash between this INSERT and set_calendar must leave the
        # reminder visibly in need of repair rather than looking already synced.
        rid = await self._exec(
            "INSERT INTO reminders (title, due_at, status, calendar_pending, created_at) "
            "VALUES (?,?,?,1,?)",
            (title, to_iso(due), "pending", to_iso(datetime.now(timezone.utc))))
        return await self.get(rid)

    async def add_clarify(self, title: str, day: str) -> "Reminder":
        # A new clarify card supersedes any older "Напишу время" flag: her
        # next free-text reply is meant to answer this new prompt, not some
        # abandoned one from a previous reminder.
        async with aiosqlite.connect(self.path) as c:
            await c.execute(
                "UPDATE reminders SET awaiting_manual_time=0, awaiting_manual_since=NULL "
                "WHERE awaiting_manual_time=1")
            cur = await c.execute(
                "INSERT INTO reminders (title, day, status, created_at) VALUES (?,?,?,?)",
                (title, day, "pending_clarify", to_iso(datetime.now(timezone.utc))))
            await c.commit()
            rid = cur.lastrowid
        return await self.get(rid)

    async def get(self, rid: int) -> "Reminder | None":
        return await self._row(f"SELECT {COLS} FROM reminders WHERE id=?", (rid,))

    async def set_status(self, rid: int, status: str) -> None:
        await self._exec("UPDATE reminders SET status=? WHERE id=?", (status, rid))

    async def promote(self, rid: int, due: datetime) -> None:
        # calendar_pending=1 for the same reason as in add_pending: the new due
        # time is persisted before the calendar catches up, so an interruption
        # in between leaves the row repairable by the scheduler. calendar_attempts
        # / calendar_last_attempt_at reset too: this is a fresh scheduling, not a
        # continuation of whatever backoff a previous due time had accrued.
        await self._exec(
            "UPDATE reminders SET status='pending', due_at=?, pings_sent=0, "
            "last_ping_at=NULL, delivery_failures=0, awaiting_manual_time=0, "
            "awaiting_manual_since=NULL, calendar_pending=1, calendar_attempts=0, "
            "calendar_last_attempt_at=NULL WHERE id=?",
            (to_iso(due), rid))

    async def set_calendar(self, rid: int, uid: str | None, pending: int) -> None:
        if pending:
            await self._exec(
                "UPDATE reminders SET calendar_uid=?, calendar_pending=? WHERE id=?",
                (uid, pending, rid))
        else:
            await self._exec(
                "UPDATE reminders SET calendar_uid=?, calendar_pending=?, calendar_attempts=0, "
                "calendar_last_attempt_at=NULL WHERE id=?", (uid, pending, rid))

    async def record_calendar_attempt(self, rid: int, now: datetime) -> None:
        """Bookkeeping for the scheduler's retry backoff: called right before
        each repair attempt so a dead CalDAV server gets retried with
        increasing delay instead of every tick — see flow._calendar_retry_due."""
        await self._exec(
            "UPDATE reminders SET calendar_attempts=calendar_attempts+1, "
            "calendar_last_attempt_at=? WHERE id=?", (to_iso(now), rid))

    async def set_awaiting_manual(self, rid: int, now: datetime) -> None:
        # Both updates run on a single connection/transaction so a concurrent
        # call can't interleave between "clear all" and "set this one" and
        # leave more than one row flagged.
        async with aiosqlite.connect(self.path) as c:
            await c.execute(
                "UPDATE reminders SET awaiting_manual_time=0, awaiting_manual_since=NULL "
                "WHERE awaiting_manual_time=1")
            await c.execute(
                "UPDATE reminders SET awaiting_manual_time=1, awaiting_manual_since=? WHERE id=?",
                (to_iso(now), rid))
            await c.commit()

    async def clear_awaiting_manual(self) -> None:
        await self._exec(
            "UPDATE reminders SET awaiting_manual_time=0, awaiting_manual_since=NULL "
            "WHERE awaiting_manual_time=1")

    async def get_awaiting_manual(self, now: datetime, timeout_minutes: int) -> "Reminder | None":
        r = await self._row(
            f"SELECT {COLS} FROM reminders WHERE awaiting_manual_time=1 "
            "AND status='pending_clarify' ORDER BY id DESC LIMIT 1")
        if r is None or r.awaiting_manual_since is None:
            return r
        # She tapped "Напишу время", got distracted, and never answered: a
        # much later unrelated private message must not be silently consumed
        # as the answer to a reminder she has long forgotten about.
        if now - from_iso(r.awaiting_manual_since) > timedelta(minutes=timeout_minutes):
            return None
        return r

    async def list_pending(self) -> list["Reminder"]:
        async with aiosqlite.connect(self.path) as c:
            cur = await c.execute(f"SELECT {COLS} FROM reminders WHERE status='pending'")
            return [Reminder(*row) for row in await cur.fetchall()]

    async def record_ping(self, rid: int, now: datetime) -> None:
        # A successful send clears any earlier delivery failures for this row
        # — see record_delivery_failure / flow._ping.
        await self._exec(
            "UPDATE reminders SET pings_sent=pings_sent+1, last_ping_at=?, "
            "delivery_failures=0 WHERE id=?", (to_iso(now), rid))

    async def record_delivery_failure(self, rid: int) -> int:
        """Count consecutive failed delivery attempts (send_message raised —
        she never pressed /start, blocked the bot, deactivated). Returns the
        new count so the caller can decide whether to give up on the row."""
        async with aiosqlite.connect(self.path) as c:
            await c.execute(
                "UPDATE reminders SET delivery_failures=delivery_failures+1 WHERE id=?", (rid,))
            await c.commit()
            cur = await c.execute("SELECT delivery_failures FROM reminders WHERE id=?", (rid,))
            row = await cur.fetchone()
            return row[0] if row else 0
