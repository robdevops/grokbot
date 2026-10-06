"""POST_TO_GROUPS_FROM_DM: a group's admin can have the bot post in that group from a DM."""

from __future__ import annotations

import asyncio
import difflib
import html
import logging
import re

from telegram.error import TelegramError

from ..config import MAX_TG_MESSAGE
from ..context import Ctx
from ..mcp.schema import ToolDef
from ..telegram.send import send_html

log = logging.getLogger("bot")

GROUP_TYPES = ("group", "supergroup")
POST_VERB_RE = re.compile(r"\b(say|post|send|tell|write|announce|message|greet|wish)\b", re.I)
GROUP_WORD_RE = re.compile(r"\b(group|chat|channel)\b", re.I)
FILLER = {"the", "a", "an", "our", "my", "of", "in", "to", "on", "group", "chat", "channel"}
TOOL = ToolDef(
    "send_to_group",
    "Post a message in a Telegram group the user administers, when they ask you to say something "
    "there. Write it in your own voice, as a spontaneous message of your own: never say or hint "
    "that someone asked you to, and never name them. Post only what was asked, nothing extra.",
    {"type": "object", "required": ["group", "text"], "properties": {
        "group": {"type": "string", "description": "The group's name as the user said it"},
        "text": {"type": "string", "description": "The message to post, in your own voice"}}})


def wants_post(text: str) -> bool:
    """Does this DM look like a request to say something in a group?"""
    return bool(POST_VERB_RE.search(text) and GROUP_WORD_RE.search(text))


def _words(text: str) -> list[str]:
    return re.sub(r"[\W_]+", " ", text.casefold()).split()


def find_groups(chats: list[tuple[int, str]], query: str) -> list[tuple[int, str]]:
    """Known groups whose title holds every word of the query (ignoring case, emoji and filler
    words like "group"), else the closest titles."""
    words = [w for w in _words(query) if w not in FILLER]
    if not words:
        return []
    hits = [(i, t) for i, t in chats if set(words) <= set(_words(t))]
    if hits:
        return hits
    titles = {" ".join(_words(t)): (i, t) for i, t in chats}
    return [titles[c] for c in difflib.get_close_matches(" ".join(words), list(titles), n=3, cutoff=0.75)]


async def _administers(bot, chat_id: int, user_id: int) -> bool:
    try:
        admins = await bot.get_chat_administrators(chat_id)
    except TelegramError as e:
        log.warning("Can't read the admins of chat %s: %s", chat_id, e)
        return False
    return any(a.user.id == user_id for a in admins)


def tool(ctx: Ctx, user_id: int, name: str = ""):
    """(definition, function) of send_to_group for this requester: it only posts in a group the
    requester administers, answers the same way whether or not other groups exist, and refuses a
    post that names the requester (the post should read as the bot's own)."""
    given_names = {w for w in _words(name) if len(w) >= 3}

    async def send(args: dict) -> str:
        group, text = str(args.get("group", "")).strip(), str(args.get("text", "")).strip()
        if not group or not text:
            return "Error: give the group's name and the text to post."
        if len(text) > MAX_TG_MESSAGE:
            return f"Error: the text is {len(text)} characters; the limit is {MAX_TG_MESSAGE}. Shorten it."
        if given_names & set(_words(text)):
            return "Error: the text names the person who asked. Reword it without their name, as your own message."
        known = find_groups(await asyncio.to_thread(ctx.store.chats), group)
        mine = [(i, t) for i, t in known if await _administers(ctx.bot, i, user_id)]
        if not mine:
            return f"I don't know a group called '{group}' that you administer."
        if len(mine) > 1:
            return "Several of your groups match: " + ", ".join(t for _, t in mine) + ". Ask which one."
        chat_id, title = mine[0]
        try:
            sent = await send_html(ctx.bot, chat_id, html.escape(text))
        except TelegramError as e:
            return f"Couldn't post in {title}: {e}"
        await asyncio.to_thread(ctx.store.save, sent)
        log.info("Posted in group %s for user %s", chat_id, user_id)
        return f"Posted in {title}."

    return TOOL, send


async def on_membership(ctx: Ctx, update, context) -> None:
    """The bot was added to or removed from a chat: keep the list of groups it can post in."""
    event = update.my_chat_member
    if event is None or event.chat.type not in GROUP_TYPES:
        return
    if event.new_chat_member.status in ("member", "administrator"):
        await asyncio.to_thread(ctx.store.remember_chat, event.chat.id, event.chat.title or "")
    else:
        await asyncio.to_thread(ctx.store.forget_chat, event.chat.id)
