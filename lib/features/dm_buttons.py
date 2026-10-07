"""TELEGRAM_DM_BUTTONS: preset-prompt buttons in private chats, refreshed on startup."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from itertools import batched

from telegram import ReplyKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import Forbidden, TelegramError

from ..context import Ctx

log = logging.getLogger("bot")

PRESETS = {  # button label -> what it asks for; laid out 2 per row
    "News": "today's top headlines",
    "News (AU)": "today's top headlines in Australia",
    "Finance": "today's market news, US and Australia",
    "AI": "today's AI news",
    "Sci-fi": "top trending sci-fi series or movies",
    "SpaceX & Tesla": "latest milestones or announcements from Musk's companies",
}
KV_KEY = "dm_keyboard"
PUSH_DELAY = 0.1  # seconds between startup messages (Telegram allows ~30 per second)


def keyboard() -> ReplyKeyboardMarkup:
    labels = list(PRESETS)
    return ReplyKeyboardMarkup([list(row) for row in batched(labels, 2, strict=False)],
                               resize_keyboard=True, is_persistent=True)


def preset_for(text: str) -> str | None:
    return PRESETS.get(text.strip())


def keyboard_hash() -> str:
    return hashlib.sha1(json.dumps(PRESETS, sort_keys=False).encode()).hexdigest()[:12]


async def push_on_startup(ctx: Ctx) -> int:
    """If the buttons changed since the last run, send everyone this bot has a private chat with
    one short message carrying the new keyboard (an unchanged keyboard sends nothing). People who
    blocked the bot are skipped. Returns how many were sent."""
    current = keyboard_hash()
    if await asyncio.to_thread(ctx.store.kv_get, KV_KEY) == current:
        return 0
    chats = await asyncio.to_thread(ctx.store.dm_chats)
    sent = 0
    for chat_id in chats:
        try:
            await ctx.bot.send_message(chat_id, "Buttons updated.", reply_markup=keyboard(),
                                       parse_mode=ParseMode.HTML, disable_notification=True)
            sent += 1
        except Forbidden:
            log.info("DM buttons: chat %s has blocked the bot", chat_id)
        except TelegramError as e:
            log.warning("DM buttons: couldn't update chat %s: %s", chat_id, e)
        await asyncio.sleep(PUSH_DELAY)
    await asyncio.to_thread(ctx.store.kv_set, KV_KEY, current)
    log.info("DM buttons updated for %d of %d chats", sent, len(chats))
    return sent
