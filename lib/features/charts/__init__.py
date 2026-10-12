"""send_chart: draw a pie or bar chart from numbers the model got from its tools and post it in the chat."""

from __future__ import annotations

import asyncio
import html
import json
import logging
from datetime import datetime

from telegram import Message
from telegram.constants import ParseMode
from telegram.error import TelegramError

from ...context import Ctx
from ...mcp.results import table_records
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


async def _send(ctx: Ctx, msg: Message, kind: str, title: str, items: object, subtitle: str, caption: str) -> str:
    """Draw the chart and post it as a reply to `msg`; "Error: ..." text if that fails, else "sent"."""
    try:
        png = await asyncio.to_thread(render.draw, kind, title, items, subtitle)
    except render.ChartError as e:
        return f"Error: {e}."
    except Exception as e:  # a drawing bug must not sink the answer: the model falls back to text
        log.exception("Chart drawing failed")
        return f"Error: couldn't draw the chart ({type(e).__name__}); answer in text instead."
    caption = html.escape(caption.strip())[:CAPTION_MAX]
    try:
        sent = await ctx.bot.send_photo(msg.chat_id, photo=png, caption=caption or None, parse_mode=ParseMode.HTML,
                                        reply_to_message_id=msg.message_id, disable_notification=True)
    except TelegramError as e:
        return f"Error: couldn't send the chart: {e}."
    await asyncio.to_thread(ctx.store.save, sent)
    log.info("Sent a %s chart (%d bytes): %s", kind, len(png), title[:60])
    return "sent"


def tool(ctx: Ctx, msg: Message):
    """(definition, function) of send_chart, posting as a reply to `msg`."""

    async def send(args: dict) -> str:
        out = await _send(ctx, msg, str(args.get("kind", "")).lower(), str(args.get("title", "")).strip(),
                          args.get("items"), str(args.get("subtitle") or ""), str(args.get("caption") or ""))
        return out if out.startswith("Error") else "Chart sent to the chat. Add one or two sentences at most; don't repeat the numbers."

    return TOOL, send


# -- a portfolio grouped by sector, industry, holding...: the bot adds it up, not the model ---------------
GROUPS = {"sector": "sector", "industry": "industry", "holding": "code", "market": "market", "type": "type"}
PORTFOLIO_TOOL = ToolDef(
    "portfolio_chart",
    "Draw a chart of one Sharesight portfolio's current holdings grouped by sector, industry, holding, market or "
    "type, and post it in this chat. The bot fetches and adds up the holdings itself, so use this (not "
    "send_chart) for 'my portfolio by sector' and the like. Sector and industry are different Sharesight "
    "fields (Technology Services is a sector, Packaged Software an industry): pass exactly the one asked for. "
    "Call it once per portfolio asked about; portfolio_id comes from list_portfolios.",
    {"type": "object", "required": ["portfolio_id", "by"], "properties": {
        "portfolio_id": {"type": "number"},
        "by": {"type": "string", "enum": list(GROUPS)},
        "kind": {"type": "string", "enum": ["pie", "bar"], "description": "Default pie"}}})


def group_holdings(holdings: list[dict], by: str) -> list[tuple[str, float]]:
    """Value per group (largest first), from slimmed Sharesight holdings; a missing field is "Unclassified"."""
    field, totals = GROUPS[by], {}
    for h in holdings:
        value = h.get("value")
        if not isinstance(value, (int, float)) or value <= 0:
            continue
        name = str(h.get(field) or "Unclassified")
        totals[name] = totals.get(name, 0.0) + float(value)
    return sorted(totals.items(), key=lambda kv: -kv[1])


def portfolio_tool(ctx: Ctx, msg: Message):
    """(definition, function) of portfolio_chart, posting as a reply to `msg`."""

    async def send(args: dict) -> str:
        server = next((s for s in ctx.registry.up() if "get_performance_report" in s.fn_names.values()), None)
        if server is None:
            return "Error: the portfolio data source isn't connected."
        by, kind = str(args.get("by", "")).lower(), str(args.get("kind") or "pie").lower()
        if by not in GROUPS:
            return f"Error: by must be one of {', '.join(GROUPS)}."
        try:
            pid = int(args.get("portfolio_id"))
        except (TypeError, ValueError):
            return "Error: give the portfolio_id (call list_portfolios first)."
        today = datetime.now(ctx.st.tz).date().isoformat()
        try:
            report = json.loads(await server.call("get_performance_report", {
                "portfolio_id": pid, "start_date": today, "end_date": today}))["report"]
            names = {p.get("id"): p.get("name") for p in
                     json.loads(await server.call("list_portfolios", {})).get("portfolios", []) if isinstance(p, dict)}
        except (ValueError, KeyError, TypeError, AttributeError):
            return "Error: couldn't read that portfolio's holdings."
        groups = group_holdings(table_records(report.get("holdings")), by)
        if not groups:
            return "Error: that portfolio has no holdings with a value."
        currency, total = report.get("currency") or "", sum(v for _, v in groups)
        cash = sum(c.get("value") or 0 for c in report.get("cash_accounts") or [] if isinstance(c, dict))
        subtitle = f"{currency} {total:,.0f} in holdings" + (f", {cash:,.0f} cash" if cash > 0 else "")
        title = f"{names.get(pid) or f'Portfolio {pid}'} by {by}"
        sent = await _send(ctx, msg, kind, title, [{"label": k, "value": v} for k, v in groups], subtitle, "")
        if sent.startswith("Error"):
            return sent
        top = ", ".join(f"{k} {v / total * 100:.1f}% ({render.short(v)})" for k, v in groups[:8])
        return f"Chart sent to the chat ({by}: {top}). Add one or two sentences at most; don't repeat the numbers."

    return PORTFOLIO_TOOL, send
