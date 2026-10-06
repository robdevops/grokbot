"""The typing indicator and streaming drafts (private chats)."""

from __future__ import annotations

import asyncio
import html
import logging
import random
import time

from telegram.constants import ChatAction

from ..config import MAX_TG_MESSAGE
from ..textfmt import TG_TAG_RE, TOOL_SYNTAX_RE

log = logging.getLogger("bot")


async def start_typing(bot, chat_id: int) -> asyncio.Task:
    """Send the typing indicator right now, then keep it going until the task is cancelled
    (Telegram's lasts ~5 s). The first one is sent directly rather than from the task, which
    wouldn't run until the next await. A failed send is logged, never fatal."""
    async def send() -> None:
        try:
            await bot.send_chat_action(chat_id, ChatAction.TYPING)
        except Exception as e:
            log.warning("Typing indicator failed: %s", e)

    async def keep() -> None:
        while True:
            await asyncio.sleep(4)
            await send()

    await send()
    return asyncio.create_task(keep())


class Draft:
    """Streams a reply into a Telegram message draft (private chats only).

    Starts with an empty draft, which Telegram shows as "Thinking...", then shows the model's
    text as it arrives. Drafts vanish after 30 s without an update, so it's re-sent at least
    every KEEPALIVE seconds, e.g. while tools run. The finished reply is a normal message."""

    INTERVAL = 1.0  # seconds between updates while text is arriving
    KEEPALIVE = 20  # re-send before Telegram's 30 s draft timeout

    def __init__(self, bot, chat_id: int):
        self.bot, self.chat_id = bot, chat_id
        self.draft_id = random.randint(1, 2**31 - 1)
        self.text = ""  # the model's latest raw text (Telegram HTML, possibly half-written)
        self._shown: str | None = None
        self._last = 0.0
        self._task: asyncio.Task | None = None
        self._typing: asyncio.Task | None = None

    async def start(self) -> None:
        self._typing = await start_typing(self.bot, self.chat_id)  # until text starts arriving
        await self._send("")  # "Thinking..."
        self._task = asyncio.create_task(self._loop())

    def update(self, text: str) -> None:
        self.text = text

    def stop(self) -> None:
        for task in (self._task, self._typing):
            if task:
                task.cancel()

    def _render(self) -> str:
        # Half-written HTML would be rejected, so drafts are plain text.
        plain = html.unescape(TG_TAG_RE.sub("", TOOL_SYNTAX_RE.sub("", self.text))).strip()
        if len(plain) > MAX_TG_MESSAGE:
            plain = "..." + plain[-(MAX_TG_MESSAGE - 3):]
        return plain

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.INTERVAL)
            text = self._render()
            if text and self._typing:
                self._typing.cancel()
                self._typing = None
            if text != self._shown or time.monotonic() - self._last >= self.KEEPALIVE:
                await self._send(text)

    async def _send(self, text: str) -> None:
        try:
            if hasattr(self.bot, "send_message_draft"):
                await self.bot.send_message_draft(self.chat_id, self.draft_id, text)
            else:  # python-telegram-bot versions from before drafts existed
                await self.bot.do_api_request(
                    "sendMessageDraft",
                    api_kwargs={"chat_id": self.chat_id, "draft_id": self.draft_id, "text": text})
        except Exception as e:
            log.warning("sendMessageDraft failed: %s", e)
        self._shown, self._last = text, time.monotonic()
