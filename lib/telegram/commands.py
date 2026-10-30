"""Commands: /tokens (also /usage, /credits; admins) and /narrative and /events (anyone), and the admin cache flush."""

from __future__ import annotations

import asyncio
import html
import logging
import time

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import ContextTypes

from ..context import Ctx
from ..features import filings
from ..features.periods import backfill
from ..features.periods.windows import describe_scope, flush_scope
from ..store import is_dm
from ..textfmt import is_parse_error, plain_text
from .access import is_admin
from .draft import start_typing
from .send import NO_PREVIEW, reply_chunks, reply_html

log = logging.getLogger("bot")


async def _gate(ctx: Ctx, update: Update) -> bool:
    """Log the command message (these handlers take it before on_message would) and say
    whether the sender is an admin (see access.is_admin)."""
    await asyncio.to_thread(ctx.store.save, update.effective_message)
    user = update.effective_user
    if user and await is_admin(ctx, user.id):
        return True
    log.info("Ignored %s from user %s: not an admin", update.effective_message.text, user and user.id)
    return False


async def _reply(ctx: Ctx, update: Update, text: str) -> None:
    sent = await update.effective_message.reply_text(text, disable_notification=True)
    await asyncio.to_thread(ctx.store.save, sent)


def usage_text(rows: list[tuple], label: str) -> str:
    if not rows:
        return f"No usage recorded in the {label}."
    lines = [f"Usage, {label}:"]
    for model, n, tin, cached, tout, cost in rows:
        pct = 100 * (cached or 0) / tin if tin else 0
        lines.append(f"{model}: {n} requests, in {tin or 0:,} ({pct:.0f}% cached), "
                     f"out {tout or 0:,}, ${cost or 0:.4f}")
    return "\n".join(lines)


async def credits(ctx: Ctx, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/tokens - admins only: the provider balance (OpenRouter has one), then tokens and cost per
    model for the last 24 hours and 7 days."""
    if not await _gate(ctx, update):
        return
    try:
        balance = await ctx.backend.credits() or "This provider has no credit balance to show."
    except Exception as e:
        log.exception("Credit check failed")
        balance = f"Couldn't fetch credits: {e}"
    now = int(time.time())
    parts = [balance]
    for label, secs in (("last 24 hours", 86_400), ("last 7 days", 7 * 86_400)):
        rows = await asyncio.to_thread(ctx.store.usage_summary, now - secs)
        parts.append(usage_text(rows, label))
    await _reply(ctx, update, "\n\n".join(parts))


async def flush_cache(ctx: Ctx, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/flushcache [portfolio_id] [year | month | day] - admins only: forget the cached Sharesight period
    returns (all, or of one portfolio, or the ones overlapping a year/month/day), e.g. after a backdated
    change. The background fill, if on, re-fetches them."""
    if not await _gate(ctx, update):
        return
    scope = flush_scope(context.args or [])
    if scope is None:
        await _reply(ctx, update, "Usage: /flushcache [portfolio_id] [2024 | 2024-05 | 2024-05-17]")
        return
    pid, first, last = scope
    n = await asyncio.to_thread(ctx.store.period_flush, pid, first and first.isoformat(), last and last.isoformat())
    backfill.reset()
    what = describe_scope(pid, first, last)
    log.info("Flushed %d cached period returns (%s)", n, what)
    await _reply(ctx, update, f"Flushed {n} cached periods ({what})."
                 + (" Refilling in the background." if ctx.st.period_backfill else ""))


async def edit_in_place(query, text: str, markup) -> None:
    """Replace the text of the message a button sits under, as HTML first and plain text if Telegram rejects it."""
    for html_ok in (True, False):
        try:
            await query.edit_message_text(
                text if html_ok else plain_text(text), parse_mode=ParseMode.HTML if html_ok else None,
                reply_markup=markup, link_preview_options=NO_PREVIEW)
            return
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return
            if not html_ok or not is_parse_error(e):
                raise


async def show_filing(ctx: Ctx, msg, kind: filings.Kind, ticker: str, index: int | None, section: str,
                      query=None) -> None:
    """One section of a ticker's narrative or events as one message, with a button for each of the others: a reply
    to the command, or, from a button (`query`), the message the button sits under, edited."""
    typing = await start_typing(ctx.bot, msg.chat_id)
    try:
        text = await filings.message_for(ctx, kind, ticker, section, msg.chat_id)
    finally:
        typing.cancel()
    markup = filings.keyboard(kind, ticker, index)
    try:
        if query and text:
            await edit_in_place(query, text, markup)
            await query.answer()
        elif query:
            await query.answer(f"No {kind.noun} found for {ticker}.", show_alert=True)
        elif text:
            sent = await reply_html(msg, text, quote=not is_dm(msg.chat_id), reply_markup=markup)
            await asyncio.to_thread(ctx.store.save, sent)
        else:
            await reply_chunks(msg, f"No {kind.noun} found for {html.escape(ticker)}.", ctx.store, quote=False)
    except TelegramError as e:
        log.warning("Couldn't show %s %s for %s: %s", kind.noun, section, ticker, e)
        if query:
            await query.answer("Couldn't show that section.")


async def _filing_command(ctx: Ctx, update: Update, context: ContextTypes.DEFAULT_TYPE, kind: filings.Kind) -> None:
    msg, words = update.effective_message, list(context.args or [])
    await asyncio.to_thread(ctx.store.save, msg)
    ticker = words[0].lstrip("$").upper() if words else ""
    if not filings.TICKER.match(ticker):
        await reply_chunks(msg, f"Usage: /{kind.command} TICKER [section]", ctx.store, quote=False)
        return
    index, section = filings.pick(kind, words[1:])
    await show_filing(ctx, msg, kind, ticker, index, section)


async def narrative(ctx: Ctx, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/narrative TICKER [section]: the 10-K/10-Q narrative (business by default), from TickerScout."""
    await _filing_command(ctx, update, context, filings.KINDS["n"])


async def events(ctx: Ctx, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/events TICKER [section]: the 8-K events digest (earnings by default), from TickerScout."""
    await _filing_command(ctx, update, context, filings.KINDS["e"])


async def on_filing_button(ctx: Ctx, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A section button under a narrative or events reply: send that section as a new reply."""
    query = update.callback_query
    try:
        code, ticker, number = query.data[len(filings.PREFIX):].split(":")
        kind = filings.KINDS[code]
        index = int(number)
        section = kind.sections[index][1]
    except (ValueError, KeyError, IndexError):
        await query.answer("That button is out of date.")
        return
    await show_filing(ctx, query.message, kind, ticker, index, section, query=query)  # answers the tap itself
