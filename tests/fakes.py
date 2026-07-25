import threading
from types import SimpleNamespace


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

    async def send_message(self, chat_id, text, reply_markup=None):
        if chat_id in self.fail_chat_ids or (self.fail_predicate and self.fail_predicate(chat_id, text)):
            raise RuntimeError("simulated delivery failure")
        self.sent.append(SimpleNamespace(chat_id=chat_id, text=text, kb=reply_markup))
        return SimpleNamespace(message_id=len(self.sent))


class FakeCalendar:
    def __init__(self):
        self.created, self.deleted, self.fail = [], [], False
        # Monotonic, independent of `created` — tests clear that list to focus
        # on later events, and a real calendar never reissues a retired uid.
        self._next_uid = 1
        # Optional threading.Event: when set, create_event blocks until it is
        # signalled. create_event runs under asyncio.to_thread (a real OS
        # thread), so this lets a test hold a CalDAV call open while driving
        # other concurrent asyncio work (e.g. a scheduler tick) from the
        # event loop without deadlocking it.
        self.gate: threading.Event | None = None

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
        self.deleted.append(uid)
