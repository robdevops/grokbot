"""Shared fixtures and fakes."""

from __future__ import annotations

from types import SimpleNamespace as N

import pytest

from tgbot import config
from tgbot.store import Store

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


def make_msg(mid, text, sender="Rob", chat_id=-100, ts=1, reply_to=None, username=None,
             is_bot=False, **extra):
    """A minimal fake telegram.Message."""
    media = dict(photo=None, video=None, animation=None, voice=None, video_note=None, audio=None,
                 document=None, sticker=None, poll=None, location=None, contact=None)
    media.update(extra)
    return N(
        chat_id=chat_id, message_id=mid, text=text, caption=None, text_html=text,
        caption_html=None, sender_chat=None, date=N(timestamp=lambda: ts),
        reply_to_message=N(message_id=reply_to) if reply_to else None,
        from_user=N(full_name=sender, username=username, id=chat_id if chat_id > 0 else 5,
                    is_bot=is_bot),
        **media,
    )
