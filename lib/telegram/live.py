"""A message that fills in as text streams into it: written every second or so as plain text, then once as HTML."""

from __future__ import annotations

import asyncio
import html
import logging

from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError

from ..config import MAX_TG_MESSAGE
from ..textfmt import TG_TAG_RE, TOOL_SYNTAX_RE, is_parse_error, plain_text
from .send import NO_PREVIEW

log = logging.getLogger("bot")


class LiveMessage:
    """The reply to `msg`, or, with `query`, the message a button sits under. The first write sends it (or edits the
    one under the button) and later ones edit it. `update` is the callback the model's text arrives through; half-written
    HTML would be rejected, so what streams is plain text, and `finish` writes the final text as HTML (plain if Telegram
    refuses it). Nothing is written before `INTERVAL` has passed, so an answer that is ready at once is written once."""

    INTERVAL = 1.5  # seconds between writes (Telegram allows about one edit a second in a chat)

    def __init__(self, msg, *, query=None, markup=None, quote: bool = True):
        self.msg, self.query, self.markup, self.quote = msg, query, markup, quote
        self.sent = None  # the message written so far (not for a button, whose message already exists)
        self.started = False
        self.text = ""
        self._shown = ""
        self._task: asyncio.Task | None = None

    def update(self, text: str) -> None:
        self.text = text

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)  # a write in flight lands before the next one
            self._task = None

    def _preview(self) -> str:
        plain = html.unescape(TG_TAG_RE.sub("", TOOL_SYNTAX_RE.sub("", self.text))).strip()
        return plain if len(plain) <= MAX_TG_MESSAGE else plain[:MAX_TG_MESSAGE - 1] + "\u2026"

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.INTERVAL)
            text = self._preview()
            if text and text != self._shown:
                try:
                    await self._write(text, as_html=False)
                    self._shown = text
                except TelegramError as e:  # a flood limit or an edit that changed nothing: the next round tries again
                    log.debug("Live message write skipped: %s", e)

    async def _write(self, text: str, *, as_html: bool) -> None:
        kw = {"reply_markup": self.markup, "link_preview_options": NO_PREVIEW}
        if as_html:
            kw["parse_mode"] = ParseMode.HTML
        if self.query:
            await self.query.edit_message_text(text, **kw)
        elif self.sent:
            self.sent = await self.sent.edit_text(text, **kw)
        else:
            self.sent = await self.msg.reply_text(text, do_quote=self.quote, disable_notification=True, **kw)
        self.started = True

    async def finish(self, text: str):
        """Write the final text as HTML and return the message (None for a button's, which isn't new)."""
        await self.stop()
        for as_html in (True, False):
            try:
                await self._write(text if as_html else plain_text(text), as_html=as_html)
                break
            except BadRequest as e:
                if "not modified" in str(e).lower():
                    break
                if not as_html or not is_parse_error(e):
                    raise
        return self.sent
