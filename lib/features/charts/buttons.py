"""Period buttons (1D 1M 1Y 5Y, reload) under a chart of tickers: a tap redraws it in place, without the model."""

from __future__ import annotations

import asyncio
import copy
import html
import json
import logging
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import ContextTypes

from ...context import Ctx
from ...textfmt import is_parse_error, plain_text
from . import ChartError, build, render

log = logging.getLogger("bot")

PRELOAD_MAX_AGE = 60 * 60  # seconds: an uploaded picture older than this is drawn again, with fresh prices
DAY_MAX_AGE = 15 * 60  # the 1D chart moves within the hour, so its picture goes stale sooner
_tasks: set[asyncio.Task] = set()  # preloads running; held so they aren't garbage-collected
BUTTONS = (("1D", "1d"), ("1M", "1mo"), ("1Y", "1y"), ("5Y", "5y"))
RELOAD = "reload"
RELOAD_ICON = "🔄"  # an emoji, which is drawn larger than the ⟳ glyph
WORDS = {"1d": "1 day", "5d": "5 days", "1mo": "1 month", "3mo": "3 months", "6mo": "6 months", "ytd": "year to date",
         "1y": "1 year", "2y": "2 years", "5y": "5 years", "10y": "10 years", "max": "all time"}


def redrawable(args: dict) -> bool:
    """Only a chart whose series are all tickers can be fetched again for another period."""
    series = args.get("series")
    return bool(series) and all(isinstance(s, dict) and s.get("ticker") for s in series)


def period_of(args: dict) -> str:
    return next((s.get("period") for s in args.get("series") or [] if isinstance(s, dict) and s.get("period")), "1y")


def keyboard(period: str) -> InlineKeyboardMarkup:
    """The period buttons, the one shown shaded (Telegram's "primary" button style), then reload. The style goes
    in api_kwargs, sent as it is, so it works with any python-telegram-bot version; a client that doesn't know the
    style shows a plain button."""
    row = [InlineKeyboardButton(label, callback_data=f"ch:{p}", api_kwargs={"style": "primary"} if p == period else None)
           for label, p in BUTTONS]
    return InlineKeyboardMarkup([row + [InlineKeyboardButton(RELOAD_ICON, callback_data=f"ch:{RELOAD}")]])


def with_period(args: dict, period: str) -> dict:
    out = copy.deepcopy(args)
    for s in out["series"]:
        s["period"] = period
    return out


def retitle(chart, period: str) -> None:
    """The model's title names its own period; another period gets a plain one."""
    names = [s.name for s in chart.series]
    head = ", ".join(names[:4]) + (" …" if len(names) > 4 else "")
    pct = " % change" if (chart.left_percent or not any(s.axis == "left" for s in chart.series)) and (
        chart.right_percent or not any(s.axis == "right" for s in chart.series)) else ""
    chart.title = f"{head}{pct}, {WORDS.get(period, period)}"


def caption_for(chart) -> str:
    """The caption for a period the model didn't comment on: just the chart's heading."""
    return html.escape(chart.title)


async def remember(ctx: Ctx, sent, args: dict) -> None:
    """Keep what's needed to redraw a chart that went out with buttons, and start uploading its other periods."""
    caption = getattr(sent, "caption_html", None) or ""
    period = period_of(args)
    await asyncio.to_thread(ctx.store.chart_put, sent.chat_id, sent.message_id, json.dumps(args), caption, period)
    if getattr(sent, "photo", None):  # the picture just sent can be swapped back in without drawing it again
        await asyncio.to_thread(ctx.store.chart_file_put, sent.chat_id, sent.message_id, period,
                                sent.photo[-1].file_id, caption)
    if ctx.st.preload_chat is not None:
        task = asyncio.create_task(preload(ctx, sent.chat_id, sent.message_id, args))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)


async def preload(ctx: Ctx, chat_id: int, message_id: int, args: dict) -> None:
    """Draw the other periods and upload them to TELEGRAM_PRELOAD_CHAT, keeping their file IDs for the buttons."""
    shown = period_of(args)
    for _, period in BUTTONS:
        if period == shown:
            continue
        try:
            chart, _ = await build(ctx, with_period(args, period))
            retitle(chart, period)
            image = await asyncio.to_thread(render.png, chart)
            caption = caption_for(chart)
            sent = await ctx.bot.send_photo(ctx.st.preload_chat, image, caption=caption, parse_mode=ParseMode.HTML,
                                            disable_notification=True)
            await asyncio.to_thread(ctx.store.chart_file_put, chat_id, message_id, period, sent.photo[-1].file_id, caption)
        except ChartError as e:
            log.debug("Chart preload of %s skipped: %s", period, e)
        except TelegramError as e:  # not a chat the bot can post in: stop, one warning
            log.warning("Chart preload to chat %s failed: %s", ctx.st.preload_chat, e)
            return
    log.debug("Chart %s in chat %s preloaded", message_id, chat_id)


async def from_preload(ctx: Ctx, query, period: str) -> bool:
    """Swap in an already-uploaded picture for `period`; False if there is none fresh, or Telegram refused it."""
    msg = query.message
    row = await asyncio.to_thread(ctx.store.chart_file_get, msg.chat_id, msg.message_id, period)
    if row is None or time.time() - row[2] > (DAY_MAX_AGE if period == "1d" else PRELOAD_MAX_AGE):
        return False
    file_id, caption, _ = row
    try:
        await query.edit_message_media(InputMediaPhoto(file_id, caption=caption or None,
                                                       parse_mode=ParseMode.HTML if caption else None),
                                       reply_markup=keyboard(period))
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            log.warning("Couldn't reuse an uploaded chart (%s); drawing it again", e)
            return False
    await asyncio.to_thread(ctx.store.chart_set_period, msg.chat_id, msg.message_id, period)
    log.info("Chart %s in chat %s swapped to %s from an upload", msg.message_id, msg.chat_id, period)
    await query.answer()
    return True


async def on_button(ctx: Ctx, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A period or reload tap: fetch, redraw and swap the photo; the model's caption comes back on its own period."""
    query = update.callback_query
    msg = query.message
    action = query.data.split(":", 1)[1]
    row = await asyncio.to_thread(ctx.store.chart_get, msg.chat_id, msg.message_id)
    if row is None:
        await query.answer("This chart can't be redrawn any more.")
        return
    spec, model_caption, shown = row
    args = json.loads(spec)
    period = shown if action == RELOAD else action
    if period not in WORDS:
        await query.answer()
        return
    if action != RELOAD and await from_preload(ctx, query, period):
        return
    try:
        chart, _ = await build(ctx, with_period(args, period))
    except ChartError as e:
        await query.answer(str(e)[:200], show_alert=True)
        return
    original = period == period_of(args)
    if not original:
        retitle(chart, period)
    image = await asyncio.to_thread(render.png, chart)
    caption = model_caption if original else caption_for(chart)
    for html_ok in (True, False):
        text = (caption if html_ok else plain_text(caption)) or None
        try:
            await query.edit_message_media(InputMediaPhoto(image, caption=text, parse_mode=ParseMode.HTML if html_ok and text else None),
                                           reply_markup=keyboard(period))
            break
        except BadRequest as e:
            if "not modified" in str(e).lower():
                break
            if not html_ok or not is_parse_error(e):
                log.warning("Couldn't redraw chart %s in chat %s: %s", msg.message_id, msg.chat_id, e)
                await query.answer("Couldn't update the chart.")
                return
    await asyncio.to_thread(ctx.store.chart_set_period, msg.chat_id, msg.message_id, period)
    log.info("Chart %s in chat %s redrawn for %s", msg.message_id, msg.chat_id, period)
    await query.answer()
