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
    """Gates journaling only: is_enabled/monitor_enabled off means nothing at
    all happens for this owner, archive included. notify_enabled is a
    separate, narrower gate -- it only silences delivery -- so it is checked
    at send time (see _notify_if_enabled), never here."""
    owner = await deps.store.get_owner(business_connection_id)
    if owner is None or not owner.is_enabled or not owner.monitor_enabled:
        return None
    return owner


def _authored_by_owner(owner, from_user_id) -> bool:
    """Business updates carry the owner's own messages too (src/handlers.py
    relies on that). They are journaled like any other, but the owner must not
    be told that they themselves edited or deleted something -- the spec is
    about what the *собеседник* did."""
    return from_user_id is not None and from_user_id == owner.owner_user_id


def _authored_by_a_bot(msg) -> bool:
    """The owner's private chat with this very bot falls inside the business
    connection's scope, so this bot's own messages -- panel redraws chief
    among them -- are delivered back here as ordinary business updates.
    Skipping every bot, not just this one, also covers another bot editing
    its own messages in the owner's dialogs, and is the only way to catch our
    own panel here: its user id isn't known at this point.

    Duck-typed on the standard aiogram field (`from_user.is_bot`) rather than
    an aiogram import -- this module stays free of those. A missing
    from_user, or a from_user with no is_bot attribute at all, is simply
    "not a bot", never a crash."""
    return bool(getattr(getattr(msg, "from_user", None), "is_bot", False))


def _is_owner_control_chat(owner, chat_id) -> bool:
    """In a Telegram private chat the chat id equals the other party's user
    id, so the owner's own chat WITH THIS BOT -- where the /admin panel lives
    and this bot's own notifications are sent -- is uniquely identified by
    chat_id == owner.owner_user_id. Nothing in that chat is a conversation
    with a third party, so it is skipped wholesale on every path: a new
    message, an edit, and a delete alike.

    This is what makes the delete path safe even though a delete event
    carries only bare message ids with no author attached -- _authored_by_a_bot
    cannot be checked there (there is no message to read from_user off of),
    but the chat the deletion happened in is always known, and checking that
    is enough: since nothing from this chat is ever journaled to begin with,
    a delete of an id in it has nothing legitimate to report either.

    Complementary to _authored_by_a_bot, not a replacement for it: this
    removes the owner's one control chat wholesale; that one still catches a
    *different* bot's noise inside the owner's real dialogs elsewhere."""
    return chat_id == owner.owner_user_id


async def _capture_new_message(deps: MonitorDeps, owner, msg, now: datetime, text) -> None:
    """Record a message this journal has not seen before. Metadata is written
    unconditionally -- so a later edit or deletion can still describe it (e.g.
    "deleted a photo") -- while the file itself is only fetched when the owner
    has that media kind's download switched on."""
    kind, file_id = media.detect_kind(msg)
    # The message's own timestamp, not the moment this process saw it: an edit
    # of a pre-connection message reaches the journal here for the first time,
    # and dating it "now" would restart its retention clock and misfile it.
    sent_at = getattr(msg, "date", None) or now
    await deps.store.record_message(owner.id, msg.chat.id, msg.message_id,
                                    msg.from_user.id if msg.from_user else None,
                                    getattr(msg.from_user, "full_name", None),
                                    text, kind, None, sent_at,
                                    from_username=getattr(msg.from_user, "username", None))
    if kind and media.is_enabled_for(owner, kind):
        rel = await media.download(deps.bot, deps.cfg, owner.id, msg.chat.id,
                                   msg.message_id, kind, file_id)
        if rel:
            await deps.store.set_media_path(owner.id, msg.chat.id, msg.message_id, rel)


async def notify_owner(deps: MonitorDeps, owner, text: str, file_rel_path=None) -> bool:
    """Send to the owner's own chat, and -- if they opted in and are not
    already the admin -- also mirror the same event to the admin chat.
    Each recipient's delivery is isolated: a blocked/deactivated chat for one
    recipient must not stop the other from being notified.

    Returns whether the OWNER's own copy went out. Callers that are about to
    destroy journal state (mark_edited overwrites the pre-edit text, the only
    surviving copy of it) must not do so until this says yes. A failed mirror
    to the admin is not the owner's problem and never changes the answer; nor
    does a failed media re-share, since the text itself did arrive."""
    targets = [owner.owner_user_id]
    if owner.mirror_to_admin and deps.cfg.admin_user_id not in targets:
        targets.append(deps.cfg.admin_user_id)
    delivered = False
    for i, chat_id in enumerate(targets):
        body = text if i == 0 else notify.mirrored_prefix(owner.owner_name) + text
        try:
            # HTML per call, never globally on the Bot object: the reminder
            # half shares this same Bot instance and interpolates
            # user-written titles into its cards (see src/main.py).
            await deps.bot.send_message(chat_id, body, parse_mode="HTML")
            if i == 0:
                delivered = True
            if file_rel_path:
                path = os.path.join(deps.cfg.monitor_media_dir, file_rel_path)
                if os.path.exists(path):
                    await deps.bot.send_document(chat_id, notify.as_document(path))
        except Exception:
            log.warning("could not deliver a monitor notification", exc_info=True)
    return delivered


async def _notify_if_enabled(deps: MonitorDeps, owner, text: str, file_rel_path=None):
    """Wrap notify_owner with the notify_enabled gate. A muted owner gets no
    send at all -- not to themselves, not to a mirrored admin -- so "off"
    truly means silence, not "the owner is quiet but the admin still hears".

    Returns None when the notification was deliberately never attempted,
    distinct from notify_owner's own True/False. Callers that gate a journal
    write on delivery (mark_edited, below) must tell a genuine send failure
    apart from a deliberate mute: `result is False` only happens after a real
    attempt, so a mute never blocks the write the way an actual failure does.
    """
    if not owner.notify_enabled:
        return None
    return await notify_owner(deps, owner, text, file_rel_path=file_rel_path)


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
    if _is_owner_control_chat(owner, msg.chat.id):
        return
    if _authored_by_a_bot(msg):
        return
    text = msg.text or getattr(msg, "caption", None)
    await _capture_new_message(deps, owner, msg, now, text)


@_isolated
async def on_edited_business_message(msg, deps: MonitorDeps, now: datetime) -> None:
    owner = await _active_owner(deps, msg.business_connection_id)
    if owner is None:
        return
    if _is_owner_control_chat(owner, msg.chat.id):
        return
    if _authored_by_a_bot(msg):
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
    author_id = msg.from_user.id if msg.from_user else stored.from_user_id
    if not _authored_by_owner(owner, author_id):
        name = stored.from_name or getattr(msg.from_user, "full_name", None)
        username = stored.from_username or getattr(msg.from_user, "username", None)
        delivered = await _notify_if_enabled(
            deps, owner,
            notify.edited_text(name, username, stored.text, new_text, _local(deps, now)))
        if delivered is False:
            # A real send was attempted and failed. mark_edited would
            # overwrite the pre-edit text -- gone from the server and from
            # every future backup -- while the owner still does not know an
            # edit happened. Keep the original instead: the next edit of this
            # message compares against it and reports again.
            #
            # delivered is None (not False) when the owner is deliberately
            # muted (notify_enabled=0): no send was attempted at all, so this
            # gate must not apply -- a muted owner still gets a correctly
            # maintained journal.
            log.warning("edit notification undelivered for message %s; "
                        "keeping the stored original", msg.message_id)
            return
    await deps.store.mark_edited(owner.id, msg.chat.id, msg.message_id, new_text, now)


@_isolated
async def on_deleted_business_messages(event, deps: MonitorDeps, now: datetime) -> None:
    owner = await _active_owner(deps, event.business_connection_id)
    if owner is None:
        return
    chat_id = event.chat.id
    if _is_owner_control_chat(owner, chat_id):
        return
    message_ids = list(event.message_ids)
    known = {m.message_id: m for m in
             await deps.store.get_messages(owner.id, chat_id, message_ids)}
    when = _local(deps, now)
    # None stands for an id that predates the journal -- still worth reporting,
    # just without content. The owner's own deletions are journaled but not
    # reported back to them.
    reportable = []
    for message_id in message_ids:
        stored = known.get(message_id)
        author_id = stored.from_user_id if stored else None
        if not _authored_by_owner(owner, author_id):
            reportable.append(stored)
    if len(reportable) >= notify.BULK_THRESHOLD:
        # "Clear history" arrives as one event with every id in it. One send
        # per id makes Telegram 429 most of the burst, and since nothing
        # retries, those deletions are simply never reported.
        await _notify_if_enabled(deps, owner, notify.deleted_bulk_text(reportable, when))
    else:
        for stored in reportable:
            if stored is None:
                await _notify_if_enabled(deps, owner, notify.deleted_unknown_text(None, when))
                continue
            await _notify_if_enabled(deps, owner,
                                     notify.deleted_text(stored.from_name, stored.from_username,
                                                         stored, when),
                                     file_rel_path=stored.media_path)
    # Unlike mark_edited, this destroys nothing -- it only stamps deleted_at --
    # so it is not gated on delivery.
    for message_id in message_ids:
        if message_id in known:
            await deps.store.mark_deleted(owner.id, chat_id, message_id, now)
