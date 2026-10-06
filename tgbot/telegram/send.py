"""Sending Telegram HTML with a plain-text fallback, silently and without link previews."""

from __future__ import annotations

import asyncio
import logging

from telegram import LinkPreviewOptions, Message
from telegram.constants import ParseMode
from telegram.error import BadRequest

from ..store import Store
from ..textfmt import is_parse_error, plain_text, split_html

log = logging.getLogger("bot")
# Otherwise a list of Yahoo-linked tickers gets a big preview card under it.
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


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
