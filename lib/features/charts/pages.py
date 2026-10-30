"""Several charts in one reply: the first as the image and a button for each, a tap swaps the image in place."""

from __future__ import annotations

import asyncio
import json
import logging
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Message, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import ContextTypes

from ...context import Ctx
from ...textfmt import is_parse_error, plain_text
from . import ChartError, build, render

log = logging.getLogger("bot")

PREFIX = "cp:"  # callback data: cp:<index of the chart>
LABEL_MAX = 20
_tasks: set[asyncio.Task] = set()  # uploads running; held so they aren't garbage-collected


def labels_for(titles: list[str]) -> list[str]:
    """A short button label for each chart: its title without the words they all start or end with ("NVDA Q2
    Revenue by Segment", "... by Platform" give "Segment", "Platform"), or its number if that leaves nothing useful."""
    words = [t.split() for t in titles]
    start = 0
    while all(len(w) > start for w in words) and len({w[start].lower() for w in words}) == 1:
        start += 1
    end = 0
    while all(len(w) - start > end for w in words) and len({w[-1 - end].lower() for w in words}) == 1:
        end += 1
    labels = []
    for w in words:
        label = re.sub(r"^[\W_]+|[\W_]+$", "", " ".join(w[start:len(w) - end]))
        label = label[:1].upper() + label[1:]
        labels.append(label if len(label) <= LABEL_MAX else label[:LABEL_MAX - 1].rstrip() + "…")
    if any(not label for label in labels) or len(set(labels)) < len(labels):
        return [f"Chart {i + 1}" for i in range(len(titles))]
    return labels


def keyboard(labels: list[str], current: int) -> InlineKeyboardMarkup:
    """One button per chart, the one shown shaded (Telegram's "primary" style, sent through api_kwargs)."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(label, callback_data=f"{PREFIX}{i}", api_kwargs={"style": "primary"} if i == current else None)
        for i, label in enumerate(labels)]])


async def remember(ctx: Ctx, sent: Message, calls: list[dict], images: list[bytes], labels: list[str]) -> None:
    """Keep the charts' calls so a tap can draw any of them, the picture just sent as chart 0, and, with
    TELEGRAM_PRELOAD_CHAT, upload the others (already drawn) so the first tap is instant too."""
    caption = getattr(sent, "caption_html", None) or ""
    await asyncio.to_thread(ctx.store.chart_put, sent.chat_id, sent.message_id,
                            json.dumps({"pages": calls, "labels": labels}), caption, "#0")
    if getattr(sent, "photo", None):
        await asyncio.to_thread(ctx.store.chart_file_put, sent.chat_id, sent.message_id, "#0", sent.photo[-1].file_id, caption)
    if ctx.st.preload_chat is not None and len(images) > 1:
        task = asyncio.create_task(upload(ctx, sent.chat_id, sent.message_id, images, labels))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)


async def upload(ctx: Ctx, chat_id: int, message_id: int, images: list[bytes], labels: list[str]) -> None:
    for i in range(1, len(images)):
        try:
            sent = await ctx.bot.send_photo(ctx.st.preload_chat, images[i], caption=labels[i], disable_notification=True)
        except TelegramError as e:  # not a chat the bot can post in: stop, one warning
            log.warning("Chart upload to chat %s failed: %s", ctx.st.preload_chat, e)
            return
        await asyncio.to_thread(ctx.store.chart_file_put, chat_id, message_id, f"#{i}", sent.photo[-1].file_id, "")


async def swap(query, photo, caption: str, markup):
    """Replace the picture (and keep the model's caption), HTML first and plain text if Telegram rejects it."""
    for html_ok in (True, False):
        text = (caption if html_ok else plain_text(caption)) or None
        try:
            return await query.edit_message_media(
                InputMediaPhoto(photo, caption=text, parse_mode=ParseMode.HTML if html_ok and text else None),
                reply_markup=markup)
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return None
            if not html_ok or not is_parse_error(e):
                raise
    return None


async def on_page(ctx: Ctx, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A button under several charts: show that chart, from its upload if there is one, else drawn from its call."""
    query = update.callback_query
    msg = query.message
    index = int(query.data[len(PREFIX):] or 0)
    row = await asyncio.to_thread(ctx.store.chart_get, msg.chat_id, msg.message_id)
    pages = json.loads(row[0]).get("pages") if row else None
    if not pages or not 0 <= index < len(pages):
        await query.answer("These charts can't be shown any more.")
        return
    spec, caption, shown = row
    labels = json.loads(spec)["labels"]
    if shown == f"#{index}":
        await query.answer()
        return
    stored = await asyncio.to_thread(ctx.store.chart_file_get, msg.chat_id, msg.message_id, f"#{index}")
    try:
        if stored:
            photo = stored[0]
        else:
            chart, _ = await build(ctx, pages[index])
            photo = await asyncio.to_thread(render.png, chart)
        edited = await swap(query, photo, caption, keyboard(labels, index))
    except ChartError as e:
        await query.answer(str(e)[:200], show_alert=True)
        return
    except TelegramError as e:
        log.warning("Couldn't show chart %s of message %s in chat %s: %s", index, msg.message_id, msg.chat_id, e)
        await query.answer("Couldn't show that chart.")
        return
    if getattr(edited, "photo", None):  # edited is the new message; next time this chart is an upload, not a drawing
        await asyncio.to_thread(ctx.store.chart_file_put, msg.chat_id, msg.message_id, f"#{index}",
                                edited.photo[-1].file_id, caption)
    await asyncio.to_thread(ctx.store.chart_set_period, msg.chat_id, msg.message_id, f"#{index}")
    await query.answer()
