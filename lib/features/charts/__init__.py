"""send_chart: draw a pie or bar chart from numbers the model got from its tools and post it in the chat."""

from __future__ import annotations

import asyncio
import html
import logging

from telegram import Message
from telegram.constants import ParseMode
from telegram.error import TelegramError

from ...context import Ctx
from ...mcp.schema import ToolDef
from . import render

log = logging.getLogger("bot")

CAPTION_MAX = 900
TOOL = ToolDef(
    "send_chart",
    "Draw a pie chart (shares of a whole, e.g. sectors) or a horizontal bar chart (values per category or per "
    "period, negatives allowed) and post the picture in this chat. Use it whenever someone asks for a pie chart, "
    "bar chart, chart or graph. Every value must come from tool results, never invented; group categories "
    "yourself if you want different slices. Call it once per chart, then write one or two sentences, not the "
    "numbers again.",
    {"type": "object", "required": ["kind", "title", "items"], "properties": {
        "kind": {"type": "string", "enum": ["pie", "bar"]},
        "title": {"type": "string", "description": "Short title shown on the picture"},
        "items": {"type": "array", "maxItems": 200, "items": {"type": "object", "required": ["label", "value"], "properties": {
            "label": {"type": "string"}, "value": {"type": "number"}}}},
        "subtitle": {"type": "string", "description": "Optional line under the title, e.g. the total"},
        "caption": {"type": "string", "description": "Optional caption under the picture"}}})


def available() -> bool:
    return render.available()


def tool(ctx: Ctx, msg: Message):
    """(definition, function) of send_chart, posting as a reply to `msg`."""

    async def send(args: dict) -> str:
        kind, title = str(args.get("kind", "")).lower(), str(args.get("title", "")).strip()
        try:
            png = await asyncio.to_thread(render.draw, kind, title, args.get("items"), str(args.get("subtitle") or ""))
        except render.ChartError as e:
            return f"Error: {e}."
        except Exception as e:  # a drawing bug must not sink the answer: the model falls back to text
            log.exception("Chart drawing failed")
            return f"Error: couldn't draw the chart ({type(e).__name__}); answer in text instead."
        caption = html.escape(str(args.get("caption") or "").strip())[:CAPTION_MAX]
        try:
            sent = await ctx.bot.send_photo(msg.chat_id, photo=png, caption=caption or None, parse_mode=ParseMode.HTML,
                                            reply_to_message_id=msg.message_id, disable_notification=True)
        except TelegramError as e:
            return f"Error: couldn't send the chart: {e}."
        await asyncio.to_thread(ctx.store.save, sent)
        log.info("Sent a %s chart (%d bytes): %s", kind, len(png), title[:60])
        return "Chart sent to the chat. Add one or two sentences at most; don't repeat the numbers."

    return TOOL, send
