import asyncio
import logging
from datetime import timedelta

import aiosqlite

from src.db import to_iso
from src.monitor import media

log = logging.getLogger(__name__)

SWEEP_CHECK_SECONDS = 600


async def sweep(deps, now) -> dict:
    """Delete rows past their owner's retention and the files they owned."""
    rows_deleted = files_deleted = 0
    for owner in await deps.store.list_owners():
        # An explicit 0 (delete-immediately) must win over the global
        # default too, so this checks "was it set" rather than "is it
        # truthy" -- `owner.retention_days or default` would silently treat
        # 0 the same as "not set".
        days = (owner.retention_days if owner.retention_days is not None
                else deps.cfg.monitor_retention_days)
        cutoff = to_iso(now - timedelta(days=days))
        async with aiosqlite.connect(deps.store.path) as c:
            cur = await c.execute(
                "SELECT media_path FROM messages WHERE owner_id=? AND sent_at < ? "
                "AND media_path IS NOT NULL", (owner.id, cutoff))
            doomed = [row[0] for row in await cur.fetchall()]
            cur = await c.execute(
                "DELETE FROM messages WHERE owner_id=? AND sent_at < ?", (owner.id, cutoff))
            rows_deleted += cur.rowcount or 0
            await c.commit()
        for rel in doomed:
            media.remove_file(deps.cfg, rel)
            files_deleted += 1
    if rows_deleted or files_deleted:
        log.info("retention sweep removed %s rows and %s files", rows_deleted, files_deleted)
    return {"rows": rows_deleted, "files": files_deleted}


async def run_sweeper(deps) -> None:
    """Sweep once a day, at cfg.monitor_cleanup_hour local time. Checking every
    ten minutes keeps this simple and restart-safe: a missed hour is caught on
    the next day rather than tracked in state."""
    from src.flow import now_local
    last_swept_date = None
    while True:
        try:
            now = now_local(deps.cfg)
            if now.hour == deps.cfg.monitor_cleanup_hour and now.date() != last_swept_date:
                await sweep(deps, now)
                last_swept_date = now.date()
        except Exception:
            log.exception("retention sweep failed")
        await asyncio.sleep(SWEEP_CHECK_SECONDS)
