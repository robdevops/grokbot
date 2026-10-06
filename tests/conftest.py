"""Shared fixtures and fakes."""

from __future__ import annotations

from types import SimpleNamespace as N

import pytest
from telegram.error import TelegramError

from lib import config
from lib.store import Store

BASE_ENV = {"TELEGRAM_BOT_TOKEN": "1:x", "OPENROUTER_API_KEY": "key"}


@pytest.fixture
def env():
    return dict(BASE_ENV)


@pytest.fixture
def settings(env):
    return config.load(env)


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "chat.db"))
    s.bot_id = 111
    return s


def make_msg(mid, text, sender="Alex", chat_id=-100, ts=1, reply_to=None, username=None,
             is_bot=False, chat_title=None, **extra):
    """A minimal fake telegram.Message."""
    media = dict(photo=None, video=None, animation=None, voice=None, video_note=None, audio=None,
                 document=None, sticker=None, poll=None, location=None, contact=None)
    media.update(extra)
    return N(
        chat=N(id=chat_id, type="private" if chat_id > 0 else "supergroup", title=chat_title),
        chat_id=chat_id, message_id=mid, text=text, caption=None, text_html=text,
        caption_html=None, sender_chat=None, date=N(timestamp=lambda: ts),
        reply_to_message=N(message_id=reply_to) if reply_to else None,
        from_user=N(full_name=sender, username=username, id=chat_id if chat_id > 0 else 5,
                    is_bot=is_bot),
        **media,
    )


class FakeBot:
    """Records what the bot sends; message IDs count up from 1000."""

    def __init__(self, bot_id=111, username="stockbot", first_name="Stock"):
        self.id, self.username, self.first_name = bot_id, username, first_name
        self.sent: list[dict] = []
        self.drafts: list[str] = []
        self.actions: list[str] = []
        self.fail_send: dict[int, Exception] = {}
        self.admins: dict[int, list[int]] = {}  # chat id -> admin user IDs; a missing chat raises
        self._next = 1000

    def _msg(self, chat_id, text):
        self._next += 1
        m = make_msg(self._next, text, sender=self.first_name, chat_id=chat_id, username=self.username,
                     is_bot=True)
        m.from_user.id = self.id
        return m

    async def send_message(self, chat_id, text, **kw):
        if chat_id in self.fail_send:
            raise self.fail_send[chat_id]
        self.sent.append({"chat_id": chat_id, "text": text, **kw})
        return self._msg(chat_id, text)

    async def get_chat_administrators(self, chat_id):
        if chat_id not in self.admins:
            raise TelegramError("Bad Request: chat not found")
        return [N(user=N(id=uid)) for uid in self.admins[chat_id]]

    async def send_chat_action(self, chat_id, action):
        self.actions.append(action)

    async def send_message_draft(self, chat_id, draft_id, text):
        self.drafts.append(text)


def user_msg(bot: FakeBot, text, *, chat_id=-100, mid=1, sender="Alex", user_id=5, **kw):
    """A message from a person that records bot replies in `.replies`."""
    m = make_msg(mid, text, sender=sender, chat_id=chat_id, username="alex", **kw)
    m.from_user.id = user_id
    m.replies = []

    async def reply_text(text, **rkw):
        m.replies.append({"text": text, **rkw})
        return bot._msg(chat_id, text)

    m.reply_text = reply_text
    m.parse_entities = lambda types=None: {}
    m.parse_caption_entities = lambda types=None: {}
    return m


def update_for(msg, edited=False):
    return N(effective_message=msg, edited_message=msg if edited else None, effective_user=msg.from_user)
