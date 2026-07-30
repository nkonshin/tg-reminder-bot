import logging
import os
from dataclasses import dataclass
from datetime import datetime
from functools import wraps
from zoneinfo import ZoneInfo

from src.config import Config
from src.monitor import media, notify
from src.monitor.store import MonitorStore

log = logging.getLogger(__name__)


@dataclass
class MonitorDeps:
    bot: object
    store: MonitorStore
    cfg: Config


def _local(deps: MonitorDeps, now: datetime) -> datetime:
    return now.astimezone(ZoneInfo(deps.cfg.timezone))


def _isolated(handler):
    """Wrap an update handler so an unexpected failure (a DB hiccup, a
    malformed update, ...) is logged and never escapes into aiogram: this
    module's callbacks run in the same process as the reminder scheduler, and
    one bad business update must not take the whole bot down."""
    @wraps(handler)
    async def wrapper(*args, **kwargs):
        try:
            await handler(*args, **kwargs)
        except Exception:
            log.exception("monitor handler %s failed", handler.__name__)
    return wrapper


async def _active_owner(deps: MonitorDeps, business_connection_id: str):
    owner = await deps.store.get_owner(business_connection_id)
    if owner is None or not owner.is_enabled or not owner.monitor_enabled:
        return None
    return owner


async def _capture_new_message(deps: MonitorDeps, owner, msg, now: datetime, text) -> None:
    """Record a message this journal has not seen before. Metadata is written
    unconditionally -- so a later edit or deletion can still describe it (e.g.
    "deleted a photo") -- while the file itself is only fetched when the owner
    has that media kind's download switched on."""
    kind, file_id = media.detect_kind(msg)
    await deps.store.record_message(owner.id, msg.chat.id, msg.message_id,
                                    msg.from_user.id if msg.from_user else None,
                                    getattr(msg.from_user, "full_name", None),
                                    text, kind, None, now)
    if kind and media.is_enabled_for(owner, kind):
        rel = await media.download(deps.bot, deps.cfg, owner.id, msg.chat.id,
                                   msg.message_id, kind, file_id)
        if rel:
            await deps.store.set_media_path(owner.id, msg.chat.id, msg.message_id, rel)


async def notify_owner(deps: MonitorDeps, owner, text: str, file_rel_path=None) -> None:
    """Send to the owner's own chat, and -- if they opted in and are not
    already the admin -- also mirror the same event to the admin chat.
    Each recipient's delivery is isolated: a blocked/deactivated chat for one
    recipient must not stop the other from being notified."""
    targets = [owner.owner_user_id]
    if owner.mirror_to_admin and deps.cfg.admin_user_id not in targets:
        targets.append(deps.cfg.admin_user_id)
    for i, chat_id in enumerate(targets):
        body = text if i == 0 else notify.mirrored_prefix(owner.owner_name) + text
        try:
            await deps.bot.send_message(chat_id, body)
            if file_rel_path:
                path = os.path.join(deps.cfg.monitor_media_dir, file_rel_path)
                if os.path.exists(path):
                    await deps.bot.send_document(chat_id, path)
        except Exception:
            log.warning("could not deliver a monitor notification", exc_info=True)


@_isolated
async def on_business_connection(conn, deps: MonitorDeps, now: datetime) -> None:
    await deps.store.upsert_owner(conn.id, conn.user.id,
                                  getattr(conn.user, "full_name", None),
                                  bool(conn.is_enabled), now)
    log.info("business connection %s enabled=%s", conn.id, conn.is_enabled)


@_isolated
async def on_business_message(msg, deps: MonitorDeps, now: datetime) -> None:
    owner = await _active_owner(deps, msg.business_connection_id)
    if owner is None:
        return
    text = msg.text or getattr(msg, "caption", None)
    await _capture_new_message(deps, owner, msg, now, text)


@_isolated
async def on_edited_business_message(msg, deps: MonitorDeps, now: datetime) -> None:
    owner = await _active_owner(deps, msg.business_connection_id)
    if owner is None:
        return
    stored = await deps.store.get_message(owner.id, msg.chat.id, msg.message_id)
    new_text = msg.text or getattr(msg, "caption", None)
    if stored is None:
        # Edited a message from before the connection: nothing to compare
        # against, so capture it as if seen for the first time and report
        # only future edits of it.
        await _capture_new_message(deps, owner, msg, now, new_text)
        return
    if (stored.text or "") == (new_text or ""):
        # Telegram re-sends the message when a link preview finishes loading;
        # the text is identical, so there is nothing to report.
        return
    name = stored.from_name or getattr(msg.from_user, "full_name", None)
    await notify_owner(deps, owner,
                       notify.edited_text(name, stored.text, new_text, _local(deps, now)))
    await deps.store.mark_edited(owner.id, msg.chat.id, msg.message_id, new_text, now)


@_isolated
async def on_deleted_business_messages(event, deps: MonitorDeps, now: datetime) -> None:
    owner = await _active_owner(deps, event.business_connection_id)
    if owner is None:
        return
    chat_id = event.chat.id
    known = {m.message_id: m for m in
             await deps.store.get_messages(owner.id, chat_id, list(event.message_ids))}
    when = _local(deps, now)
    for message_id in event.message_ids:
        stored = known.get(message_id)
        if stored is None:
            await notify_owner(deps, owner, notify.deleted_unknown_text(None, when))
            continue
        await notify_owner(deps, owner, notify.deleted_text(stored.from_name, stored, when),
                           file_rel_path=stored.media_path)
        await deps.store.mark_deleted(owner.id, chat_id, message_id, now)
