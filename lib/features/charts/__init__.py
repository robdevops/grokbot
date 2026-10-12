"""send_chart / portfolio_chart: draw a pie or bar chart; the picture goes out with the answer as its caption."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime

from ...context import Ctx
from ...mcp.results import table_records
from ...mcp.schema import ToolDef
from . import render

log = logging.getLogger("bot")

READY = ("Chart ready: it will be attached to your reply, and your reply becomes its caption. Write that reply now "
         "in a few sentences at most, without repeating the numbers.")
TOOL = ToolDef(
    "send_chart",
    "Draw a pie chart (shares of a whole, e.g. sectors) or a horizontal bar chart (values per category or per "
    "period, negatives allowed). The picture is posted together with your reply, which becomes its caption. Use it "
    "whenever someone asks for a pie chart, bar chart, chart or graph. Every value must come from tool results, "
    "never invented; group categories yourself if you want different slices. Call it once per chart, then write "
    "one or two sentences, not the numbers again.",
    {"type": "object", "required": ["kind", "title", "items"], "properties": {
        "kind": {"type": "string", "enum": ["pie", "bar"]},
        "title": {"type": "string", "description": "Short title shown on the picture"},
        "items": {"type": "array", "maxItems": 200, "items": {"type": "object", "required": ["label", "value"], "properties": {
            "label": {"type": "string"}, "value": {"type": "number"}}}},
        "subtitle": {"type": "string", "description": "Optional line under the title, e.g. the total"}}})


def available() -> bool:
    return render.available()


async def _draw(images: list[bytes], kind: str, title: str, items: object, subtitle: str) -> str:
    """Draw the chart and queue it for the reply; "Error: ..." text if that fails, else the READY note."""
    try:
        png = await asyncio.to_thread(render.draw, kind, title, items, subtitle)
    except render.ChartError as e:
        return f"Error: {e}."
    except Exception as e:  # a drawing bug must not sink the answer: the model falls back to text
        log.exception("Chart drawing failed")
        return f"Error: couldn't draw the chart ({type(e).__name__}); answer in text instead."
    images.append(png)
    log.info("Drew a %s chart (%d bytes): %s", kind, len(png), title[:60])
    return READY


def tool(ctx: Ctx, images: list[bytes]):
    """(definition, function) of send_chart; the picture is appended to `images` for the reply to carry."""

    async def send(args: dict) -> str:
        return await _draw(images, str(args.get("kind", "")).lower(), str(args.get("title", "")).strip(),
                           args.get("items"), str(args.get("subtitle") or ""))

    return TOOL, send


# -- a portfolio grouped by sector, industry, holding...: the bot adds it up, not the model ---------------
GROUPS = {"sector": "sector", "industry": "industry", "holding": "code", "market": "market", "type": "type"}
PORTFOLIO_TOOL = ToolDef(
    "portfolio_chart",
    "Draw a chart of one Sharesight portfolio's current holdings grouped by sector, industry, holding, market or "
    "type. The picture is posted together with your reply, which becomes its caption. The bot fetches and adds up "
    "the holdings itself, so use this (not send_chart) for 'my portfolio by sector' and the like. Sector and "
    "industry are different Sharesight fields (Technology Services is a sector, Packaged Software an industry): "
    "pass exactly the one asked for. Call it once per portfolio asked about; portfolio_id comes from "
    "list_portfolios.",
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


def portfolio_tool(ctx: Ctx, images: list[bytes]):
    """(definition, function) of portfolio_chart; the picture is appended to `images` for the reply to carry."""

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
        out = await _draw(images, kind, title, [{"label": k, "value": v} for k, v in groups], subtitle)
        if out != READY:
            return out
        top = ", ".join(f"{k} {v / total * 100:.1f}% ({render.short(v)})" for k, v in groups[:8])
        return f"{out} ({by}: {top})"

    return PORTFOLIO_TOOL, send
