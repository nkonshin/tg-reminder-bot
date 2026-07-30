import os
import threading
from types import SimpleNamespace

# Telegram rejects a sendMessage whose text is longer than this, and aiogram
# surfaces that as TelegramBadRequest("message is too long"). The fake enforces
# it so a notification that only overflows in production cannot pass here.
TELEGRAM_MESSAGE_LIMIT = 4096


class FakeBot:
    def __init__(self):
        self.sent = []
        # Chat ids that must raise on send_message, simulating an
        # unreachable private chat (never pressed /start, blocked the bot,
        # deactivated the account).
        self.fail_chat_ids = set()
        # Optional callable(chat_id, text) -> bool for finer-grained control
        # than fail_chat_ids alone (e.g. failing only one reminder's pings
        # while others addressed to the same chat id keep succeeding).
        self.fail_predicate = None
        # Message of the exception raised by the two knobs above. Defaults to
        # wording aiogram/Telegram itself uses for a permanently blocked chat
        # (flow._is_permanent_delivery_failure matches on this text), since
        # that's the classic scenario fail_chat_ids/fail_predicate exist to
        # simulate. Override with something like "Bad Gateway" to simulate a
        # transient failure instead.
        self.fail_message = "Forbidden: bot was blocked by the user"
        # Monitor media download: files fetched via download(), and documents
        # sent via send_document() (used to re-share a deleted file).
        self.downloads = []
        self.fail_download = False
        self.documents = []
        # When set alongside fail_download, simulates a transfer that streamed
        # some bytes to disk before the failure (real network cutoff/timeout
        # mid-download), rather than failing before anything was written.
        self.partial_write_then_fail = False

    async def send_message(self, chat_id, text, reply_markup=None):
        if chat_id in self.fail_chat_ids or (self.fail_predicate and self.fail_predicate(chat_id, text)):
            raise RuntimeError(self.fail_message)
        if len(text) > TELEGRAM_MESSAGE_LIMIT:
            raise RuntimeError(
                "Telegram Bad Request: message is too long "
                f"({len(text)} chars > {TELEGRAM_MESSAGE_LIMIT})")
        self.sent.append(SimpleNamespace(chat_id=chat_id, text=text, kb=reply_markup))
        return SimpleNamespace(message_id=len(self.sent))

    async def download(self, file_id, destination):
        if self.fail_download:
            if self.partial_write_then_fail:
                with open(destination, "wb") as fh:
                    fh.write(b"partial")
            raise RuntimeError("download failed")
        self.downloads.append(file_id)
        with open(destination, "wb") as fh:
            fh.write(b"fake-bytes")

    async def send_document(self, chat_id, document, caption=None):
        """aiogram only *uploads* a local file when `document` is an InputFile
        (FSInputFile for a path on disk). A bare `str` is sent verbatim as the
        `document` form field, which Telegram reads as a file_id / HTTP URL —
        a filesystem path is neither, so the API answers
        "400 wrong file identifier/HTTP URL specified". Reproduce that here,
        otherwise a caller that passes a path str passes in tests and fails
        for every real user."""
        if isinstance(document, (str, bytes)):
            raise RuntimeError(
                "Telegram Bad Request: wrong file identifier/HTTP URL specified — "
                f"send_document got a bare {type(document).__name__} ({document!r}); "
                "wrap a filesystem path in aiogram.types.FSInputFile to upload it")
        path = getattr(document, "path", None)
        if path is None:
            raise RuntimeError(
                "send_document expects an FSInputFile-like object with a .path, "
                f"got {type(document).__name__}")
        if not os.path.exists(path):
            raise RuntimeError(f"Telegram Bad Request: file not found: {path}")
        self.documents.append(SimpleNamespace(chat_id=chat_id, document=document,
                                              path=str(path), caption=caption))
        return SimpleNamespace(message_id=len(self.documents))


class FakeCalendar:
    def __init__(self):
        self.created, self.deleted, self.fail = [], [], False
        # Monotonic, independent of `created` — tests clear that list to focus
        # on later events, and a real calendar never reissues a retired uid.
        self._next_uid = 1
        # Optional threading.Event: when set, create_event/delete_event block
        # until signalled. Both run under asyncio.to_thread (a real OS
        # thread), so this lets a test hold a CalDAV call open while driving
        # other concurrent asyncio work (e.g. a scheduler tick) from the
        # event loop without deadlocking it.
        self.gate: threading.Event | None = None
        self.delete_gate: threading.Event | None = None

    def create_event(self, title, start, end):
        if self.gate is not None:
            self.gate.wait()
        if self.fail:
            raise RuntimeError("caldav down")
        uid = f"uid-{self._next_uid}"
        self._next_uid += 1
        self.created.append((uid, title, start, end))
        return uid

    def delete_event(self, uid):
        if self.delete_gate is not None:
            self.delete_gate.wait()
        self.deleted.append(uid)
