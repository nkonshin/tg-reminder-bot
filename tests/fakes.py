from types import SimpleNamespace


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append(SimpleNamespace(chat_id=chat_id, text=text, kb=reply_markup))
        return SimpleNamespace(message_id=len(self.sent))


class FakeCalendar:
    def __init__(self):
        self.created, self.deleted, self.fail = [], [], False
        # Monotonic, independent of `created` — tests clear that list to focus
        # on later events, and a real calendar never reissues a retired uid.
        self._next_uid = 1

    def create_event(self, title, start, end):
        if self.fail:
            raise RuntimeError("caldav down")
        uid = f"uid-{self._next_uid}"
        self._next_uid += 1
        self.created.append((uid, title, start, end))
        return uid

    def delete_event(self, uid):
        self.deleted.append(uid)
