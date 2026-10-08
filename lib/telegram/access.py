"""Who counts as an admin: a user in ADMIN_CHAT_IDS, or an admin of a group the bot has seen."""

from __future__ import annotations

import asyncio
import logging
import time

from telegram.error import TelegramError

from ..context import Ctx

log = logging.getLogger("bot")
CACHE_SECONDS = 300  # a demoted admin loses access within this long


async def _group_admins(ctx: Ctx, chat_id: int) -> frozenset[int]:
    cached = ctx.group_admins.get(chat_id)
    if cached and cached[1] > time.monotonic():
        return cached[0]
    try:
        admins = frozenset(a.user.id for a in await ctx.bot.get_chat_administrators(chat_id))
    except TelegramError as e:
        log.warning("Can't read the admins of chat %s: %s", chat_id, e)
        admins = frozenset()
    ctx.group_admins[chat_id] = (admins, time.monotonic() + CACHE_SECONDS)
    return admins


async def is_admin(ctx: Ctx, user_id: int) -> bool:
    """True for a user in ADMIN_CHAT_IDS or an administrator of any group the bot knows. Anonymous
    admins post as the group itself, so they can't be recognised."""
    if user_id in ctx.st.admin_chats:
        return True
    for chat_id, _ in await asyncio.to_thread(ctx.store.chats):
        if user_id in await _group_admins(ctx, chat_id):
            return True
    return False
