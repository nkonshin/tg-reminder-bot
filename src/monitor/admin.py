import logging
import os
from datetime import timedelta

from src.monitor import media, notify
from src.monitor.store import TOGGLEABLE

log = logging.getLogger(__name__)


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
    monitor_on = deps.cfg.monitor_enabled and any(o.monitor_enabled for o in owners)
    return notify.admin_menu_text(len(owners), monitor_on), notify.kb_admin_main()


async def _first_owner(deps):
    owners = await deps.store.list_owners()
    return owners[0] if owners else None


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
    text = notify.admin_storage_text(db_bytes, media.dir_size(deps.cfg),
                                     total, day, week, month)
    return text, notify.kb_admin_storage()


async def _owners_screen(deps):
    owners = await deps.store.list_owners()
    return notify.admin_owners_text(owners), notify.kb_admin_owners(owners)


async def handle_callback(data: str, deps, user_id: int, now):
    """Returns the screen to show, or None when the callback is ignored."""
    if not _is_admin(deps, user_id) or not data.startswith("adm:"):
        return None
    parts = data.split(":")
    action = parts[1]

    if action == "main":
        return await _main_screen(deps)

    if action == "media":
        owner = await _first_owner(deps)
        if owner is None:
            return await _main_screen(deps)
        return notify.admin_media_text(), notify.kb_admin_media(owner)

    if action == "storage":
        return await _storage_screen(deps, now)

    if action == "retention":
        if len(parts) == 2:
            owner = await _first_owner(deps)
            days = (owner.retention_days if owner and owner.retention_days
                    else deps.cfg.monitor_retention_days)
            return notify.admin_retention_text(days), notify.kb_admin_retention()
        days = _parse_int(parts[2])
        if days is None:
            return None
        for owner in await deps.store.list_owners():
            await deps.store.set_retention_days(owner.id, days)
        return notify.admin_retention_text(days), notify.kb_admin_retention()

    if action == "owners":
        return await _owners_screen(deps)

    if action == "mirror" and len(parts) == 3:
        owner_id = _parse_int(parts[2])
        if owner_id is None:
            return None
        owner = await deps.store.get_owner_by_id(owner_id)
        if owner is None:
            return None
        await deps.store.set_owner_flag(owner.id, "mirror_to_admin",
                                        0 if owner.mirror_to_admin else 1)
        return await _owners_screen(deps)

    if action == "toggle" and len(parts) == 4:
        owner_id, field = _parse_int(parts[2]), parts[3]
        if owner_id is None or field not in TOGGLEABLE:
            return None
        owner = await deps.store.get_owner_by_id(owner_id)
        if owner is None:
            return None
        await deps.store.set_owner_flag(owner_id, field, 0 if getattr(owner, field) else 1)
        owner = await deps.store.get_owner_by_id(owner_id)
        return notify.admin_media_text(), notify.kb_admin_media(owner)

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
