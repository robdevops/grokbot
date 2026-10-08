"""Owner commands: /credits (provider balance, where there is one) and /usage (token ledger)."""

from __future__ import annotations

import asyncio
import logging
import time

from telegram import Update
from telegram.ext import ContextTypes

from ..context import Ctx

log = logging.getLogger("bot")


async def _gate(ctx: Ctx, update: Update) -> bool:
    """Log the command message (these handlers take it before on_message would) and say
    whether the sender is an admin (a user ID in ADMIN_CHAT_IDS)."""
    await asyncio.to_thread(ctx.store.save, update.effective_message)
    user = update.effective_user
    if user and user.id in ctx.st.admin_chats:
        return True
    log.info("Ignored %s from user %s: not in ADMIN_CHAT_IDS", update.effective_message.text, user and user.id)
    return False


async def _reply(ctx: Ctx, update: Update, text: str) -> None:
    sent = await update.effective_message.reply_text(text, disable_notification=True)
    await asyncio.to_thread(ctx.store.save, sent)


async def credits(ctx: Ctx, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/credits - admins only: how much provider credit is left (OpenRouter)."""
    if not await _gate(ctx, update):
        return
    try:
        text = await ctx.backend.credits() or "This provider has no credit balance to show."
    except Exception as e:
        log.exception("Credit check failed")
        text = f"Couldn't fetch credits: {e}"
    await _reply(ctx, update, text)


def usage_text(rows: list[tuple], label: str) -> str:
    if not rows:
        return f"No usage recorded in the {label}."
    lines = [f"Usage, {label}:"]
    for model, n, tin, cached, tout, cost in rows:
        pct = 100 * (cached or 0) / tin if tin else 0
        lines.append(f"{model}: {n} requests, in {tin or 0:,} ({pct:.0f}% cached), "
                     f"out {tout or 0:,}, ${cost or 0:.4f}")
    return "\n".join(lines)


async def usage(ctx: Ctx, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/usage - admins only: tokens and cost per model for the last 24 hours and 7 days."""
    if not await _gate(ctx, update):
        return
    now = int(time.time())
    parts = []
    for label, secs in (("last 24 hours", 86_400), ("last 7 days", 7 * 86_400)):
        rows = await asyncio.to_thread(ctx.store.usage_summary, now - secs)
        parts.append(usage_text(rows, label))
    await _reply(ctx, update, "\n\n".join(parts))
