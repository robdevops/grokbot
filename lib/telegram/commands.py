"""Admin command: /credits (provider balance, where there is one, and the token ledger)."""

from __future__ import annotations

import asyncio
import logging
import time

from telegram import Update
from telegram.ext import ContextTypes

from ..context import Ctx
from .access import is_admin

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
    """/credits - admins only: the provider balance (OpenRouter has one), then tokens and cost per
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
