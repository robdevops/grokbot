"""Sending Telegram HTML with a plain-text fallback, silently and without link previews."""

from __future__ import annotations

import asyncio
import logging

from telegram import InputMediaPhoto, LinkPreviewOptions, Message
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError

from ..store import Store
from ..textfmt import is_parse_error, plain_text, split_html

log = logging.getLogger("bot")
# Otherwise a list of Yahoo-linked tickers gets a big preview card under it.
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
CAPTION_MAX = 1024  # Telegram's limit on a photo's caption (characters of text, not markup)
ALBUM_MAX = 10  # photos in one album


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


async def reply_with_photos(msg: Message, images: list[bytes], html_text: str, store: Store, *, quote: bool = True,
                            last_markup=None) -> None:
    """Reply with pictures and the answer as the caption of the first one (one message, or one album). An answer
    too long for a caption goes out as normal text after the pictures. If Telegram refuses the pictures the
    answer is still sent as text."""
    caption = html_text if len(plain_text(html_text)) <= CAPTION_MAX else None
    try:
        for start in range(0, len(images), ALBUM_MAX):
            batch = images[start:start + ALBUM_MAX]
            text = caption if start == 0 else None
            for sent in await _send_photos(msg, batch, text, quote and start == 0,
                                           last_markup if caption and start + ALBUM_MAX >= len(images) else None):
                await asyncio.to_thread(store.save, sent)
    except TelegramError as e:
        log.warning("Couldn't send the chart(s), sending the answer as text: %s", e)
        await reply_chunks(msg, html_text, store, quote=quote, last_markup=last_markup)
        return
    if caption is None:
        await reply_chunks(msg, html_text, store, quote=False, last_markup=last_markup)


async def _send_photos(msg: Message, batch: list[bytes], caption: str | None, quote: bool, markup) -> list[Message]:
    """One photo, or an album of 2-10; the caption (Telegram HTML, plain text if it is rejected) rides on the first."""
    for text, mode in ((caption, ParseMode.HTML), (plain_text(caption) if caption else None, None)):
        try:
            if len(batch) == 1:
                return [await msg.reply_photo(batch[0], caption=text, parse_mode=mode, do_quote=quote,
                                              disable_notification=True, reply_markup=markup)]
            media = [InputMediaPhoto(img, caption=text if i == 0 else None, parse_mode=mode if i == 0 else None)
                     for i, img in enumerate(batch)]
            return list(await msg.reply_media_group(media, do_quote=quote, disable_notification=True))
        except BadRequest as e:
            if not is_parse_error(e) or caption is None or mode is None:
                raise
            log.warning("Bad HTML from the model, sending the caption as plain text: %s", e)
    raise AssertionError("unreachable")
