import asyncio
import logging
import os
from datetime import timedelta

from src.monitor import media, notify
from src.monitor.store import TOGGLEABLE

log = logging.getLogger(__name__)

# Toggles that belong to the Мониторинг screen; everything else in TOGGLEABLE
# is a media-download switch and redraws the Медиа screen instead.
OWNER_SCREEN_FIELDS = ("monitor_enabled", "mirror_to_admin")

# A negative retention makes the sweep's cutoff a moment in the FUTURE, so the
# next nightly run deletes the whole journal. 3650 days is ten years, well past
# anything the panel offers and far short of overflowing timedelta.
RETENTION_MIN_DAYS = 0
RETENTION_MAX_DAYS = 3650


def _is_admin(deps, user_id: int) -> bool:
    return user_id == deps.cfg.admin_user_id


def _parse_int(raw: str) -> int | None:
    """Parse a callback-data numeric segment, tolerating malformed input."""
    try:
        return int(raw)
    except ValueError:
        return None


async def _main_screen(deps):
    owners = await deps.store.list_owners()
    # With no connections yet there is nothing to AND against: any() over an
    # empty list is False, so the first /admin after a deploy reported
    # "Мониторинг: выключен" while MONITOR_ENABLED was 1. Report the config.
    monitor_on = bool(deps.cfg.monitor_enabled) and (
        any(o.monitor_enabled for o in owners) if owners else True)
    return notify.admin_menu_text(len(owners), monitor_on), notify.kb_admin_main()


async def open_panel(deps, user_id: int):
    if not _is_admin(deps, user_id):
        return None
    return await _main_screen(deps)


async def _storage_screen(deps, now):
    db_bytes = os.path.getsize(deps.store.path) if os.path.exists(deps.store.path) else 0
    total = await deps.store.stats()
    day = await deps.store.stats(since=now - timedelta(days=1))
    week = await deps.store.stats(since=now - timedelta(days=7))
    month = await deps.store.stats(since=now - timedelta(days=30))
    # Walking the media tree is a full recursive stat of every downloaded file;
    # on the event loop it stalls polling and the reminder scheduler for as
    # long as it takes.
    media_bytes = await asyncio.to_thread(media.dir_size, deps.cfg)
    text = notify.admin_storage_text(db_bytes, media_bytes, total, day, week, month)
    return text, notify.kb_admin_storage()


async def _owners_screen(deps):
    owners = await deps.store.list_owners()
    return (notify.admin_owners_text(owners, bool(deps.cfg.monitor_enabled)),
            notify.kb_admin_owners(owners))


async def _media_screen(deps, owner, owners_count: int):
    """With more than one connection the media toggles are per-owner, so the
    screen has to name whose it is showing and lead back to the picker."""
    back = "adm:media" if owners_count > 1 else "adm:main"
    whose = owner.owner_name if owners_count > 1 else None
    return notify.admin_media_text(whose), notify.kb_admin_media(owner, back)


async def handle_callback(data: str, deps, user_id: int, now):
    """Returns the screen to show, or None when the callback is ignored."""
    if not _is_admin(deps, user_id) or not data.startswith("adm:"):
        return None
    parts = data.split(":")
    action = parts[1]

    if action == "main":
        return await _main_screen(deps)

    if action == "media":
        owners = await deps.store.list_owners()
        if not owners:
            return await _main_screen(deps)
        if len(parts) == 3:
            owner_id = _parse_int(parts[2])
            owner = (await deps.store.get_owner_by_id(owner_id)
                     if owner_id is not None else None)
            if owner is None:
                return None
            return await _media_screen(deps, owner, len(owners))
        if len(owners) > 1:
            return notify.admin_media_picker_text(), notify.kb_admin_media_owners(owners)
        return await _media_screen(deps, owners[0], 1)

    if action == "storage":
        return await _storage_screen(deps, now)

    if action == "retention":
        # Unlike медиа and мониторинг, this one deliberately applies to every
        # connection at once; the screen text says so.
        owners = await deps.store.list_owners()
        if len(parts) == 2:
            days = (owners[0].retention_days
                    if owners and owners[0].retention_days is not None
                    else deps.cfg.monitor_retention_days)
            return (notify.admin_retention_text(days, len(owners)),
                    notify.kb_admin_retention())
        days = _parse_int(parts[2])
        if days is None or not RETENTION_MIN_DAYS <= days <= RETENTION_MAX_DAYS:
            return None
        for owner in owners:
            await deps.store.set_retention_days(owner.id, days)
        return notify.admin_retention_text(days, len(owners)), notify.kb_admin_retention()

    if action == "owners":
        return await _owners_screen(deps)

    if action == "toggle" and len(parts) == 4:
        owner_id, field = _parse_int(parts[2]), parts[3]
        if owner_id is None or field not in TOGGLEABLE:
            return None
        owner = await deps.store.get_owner_by_id(owner_id)
        if owner is None:
            return None
        await deps.store.set_owner_flag(owner_id, field, 0 if getattr(owner, field) else 1)
        if field in OWNER_SCREEN_FIELDS:
            return await _owners_screen(deps)
        owners = await deps.store.list_owners()
        owner = await deps.store.get_owner_by_id(owner_id)
        return await _media_screen(deps, owner, len(owners))

    if action == "export":
        if len(parts) == 2:
            return notify.admin_export_text(), notify.kb_admin_export()
        from src.monitor import export as export_mod
        include_media = parts[2] == "full"
        stamp = now.strftime("%Y%m%d-%H%M")
        try:
            await export_mod.send_export(deps, user_id, include_media, stamp)
        except Exception:
            log.exception("export failed")
        return await _main_screen(deps)

    return None
