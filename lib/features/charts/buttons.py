"""Period buttons (1D 1M 1Y 5Y, reload) under a chart of tickers: a tap redraws it in place, without the model."""

from __future__ import annotations

import asyncio
import copy
import html
import json
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from ...context import Ctx
from ...textfmt import is_parse_error, plain_text
from . import ChartError, build, render

log = logging.getLogger("bot")

BUTTONS = (("1D", "1d"), ("1M", "1mo"), ("1Y", "1y"), ("5Y", "5y"))
RELOAD = "reload"
WORDS = {"1d": "1 day", "5d": "5 days", "1mo": "1 month", "3mo": "3 months", "6mo": "6 months", "ytd": "year to date",
         "1y": "1 year", "2y": "2 years", "5y": "5 years", "10y": "10 years", "max": "all time"}


def redrawable(args: dict) -> bool:
    """Only a chart whose series are all tickers can be fetched again for another period."""
    series = args.get("series")
    return bool(series) and all(isinstance(s, dict) and s.get("ticker") for s in series)


def period_of(args: dict) -> str:
    return next((s.get("period") for s in args.get("series") or [] if isinstance(s, dict) and s.get("period")), "1y")


def keyboard(period: str) -> InlineKeyboardMarkup:
    """The period buttons, the one shown marked, then reload."""
    row = [InlineKeyboardButton(f"• {label}" if p == period else label, callback_data=f"ch:{p}") for label, p in BUTTONS]
    return InlineKeyboardMarkup([row + [InlineKeyboardButton("⟳", callback_data=f"ch:{RELOAD}")]])


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


def caption_for(chart, period: str) -> str:
    """A short caption for a period the model didn't comment on: each series' total."""
    totals = ", ".join(f"{s.name} {s.total}".strip() for s in chart.series)
    return html.escape(f"{WORDS.get(period, period)}: {totals}")


async def remember(ctx: Ctx, sent, args: dict) -> None:
    """Keep what's needed to redraw a chart that went out with buttons."""
    caption = getattr(sent, "caption_html", None) or ""
    await asyncio.to_thread(ctx.store.chart_put, sent.chat_id, sent.message_id, json.dumps(args), caption, period_of(args))


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
    try:
        chart, _ = await build(ctx, with_period(args, period))
    except ChartError as e:
        await query.answer(str(e)[:200], show_alert=True)
        return
    original = period == period_of(args)
    if not original:
        retitle(chart, period)
    image = await asyncio.to_thread(render.png, chart)
    caption = model_caption if original else caption_for(chart, period)
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
