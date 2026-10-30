"""Sending Telegram HTML with a plain-text fallback, silently and without link previews."""

from __future__ import annotations

import asyncio
import logging

from telegram import LinkPreviewOptions, Message
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError

from ..store import Store
from ..textfmt import is_parse_error, plain_text, split_html

log = logging.getLogger("bot")
# Otherwise a list of Yahoo-linked tickers gets a big preview card under it.
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
CAPTION_MAX = 1000  # Telegram allows 1024 characters of caption (after the HTML is parsed)


async def send_html(bot, chat_id: int, text: str, reply_markup=None):
    """Send Telegram HTML to a chat, falling back to plain text if it's rejected."""
    try:
        return await bot.send_message(
            chat_id, text, parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW,
            disable_notification=True, reply_markup=reply_markup)
    except BadRequest as e:
        if not is_parse_error(e):
            raise
        return await bot.send_message(
            chat_id, plain_text(text), link_preview_options=NO_PREVIEW,
            disable_notification=True, reply_markup=reply_markup)


async def reply_html(msg: Message, text: str, *, quote: bool = True, reply_markup=None):
    """Reply to a message with Telegram HTML, falling back to plain text if it's rejected."""
    try:
        return await msg.reply_text(
            text, parse_mode=ParseMode.HTML, do_quote=quote, disable_notification=True,
            link_preview_options=NO_PREVIEW, reply_markup=reply_markup)
    except BadRequest as e:
        if not is_parse_error(e):
            raise
        log.warning("Bad HTML from the model, sending as plain text: %s", e)
        log.debug("The text that failed: %r", text[:600])
        return await msg.reply_text(
            plain_text(text), do_quote=quote, disable_notification=True,
            link_preview_options=NO_PREVIEW, reply_markup=reply_markup)


async def reply_chunks(msg: Message, html_text: str, store: Store, *, quote: bool = True,
                       last_markup=None) -> None:
    """Reply with an answer, split to fit Telegram's limit, logging each message sent (bots
    don't receive their own messages). `last_markup` rides on the last chunk."""
    chunks = split_html(html_text)
    for i, chunk in enumerate(chunks):
        markup = last_markup if i == len(chunks) - 1 else None
        sent = await reply_html(msg, chunk, quote=quote, reply_markup=markup)
        await asyncio.to_thread(store.save, sent)


async def reply_with_photo(msg: Message, photo: bytes, html_text: str, store: Store, *, quote: bool = True,
                           markup=None, photo_markup=None) -> Message | None:
    """An answer with a chart as one reply: the photo with the answer as its caption. Text too long for a caption
    continues in text messages; if Telegram refuses the photo, the text goes alone. `photo_markup` (the chart's
    buttons) takes the place of `markup` on the photo. Returns the photo message."""
    chunks = split_html(html_text, CAPTION_MAX) if html_text else [""]
    rest = "\n".join(chunks[1:])  # split_html cuts at newlines where it can
    try:
        sent = await _send_photo(msg, photo, chunks[0], quote, photo_markup or (None if rest else markup))
    except TelegramError as e:
        log.warning("Couldn't send the chart, replying with text only: %s", e)
        await reply_chunks(msg, html_text, store, quote=quote, last_markup=markup)
        return None
    await asyncio.to_thread(store.save, sent)
    if rest:
        await reply_chunks(msg, rest, store, quote=quote, last_markup=markup)
    return sent


async def _send_photo(msg: Message, photo: bytes, caption: str, quote: bool, markup) -> Message:
    kw = {"do_quote": quote, "disable_notification": True, "reply_markup": markup}
    for html_ok in (True, False):  # Telegram HTML, then plain text if it is rejected
        text = (caption if html_ok else plain_text(caption)) or None
        try:
            return await msg.reply_photo(photo, caption=text, parse_mode=ParseMode.HTML if html_ok and text else None, **kw)
        except BadRequest as e:
            if not html_ok or not is_parse_error(e):
                raise
            log.warning("Bad HTML from the model, sending the caption as plain text: %s", e)
            log.debug("The caption that failed: %r", text[:600])
    raise RuntimeError("unreachable")
