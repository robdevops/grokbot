"""period_returns: a Sharesight portfolio's return per day, week, month, quarter or year, in one tool call
instead of one model-driven report per period. Finished periods are cached in SQLite (see backfill.py)."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime

from ...context import Ctx
from ...mcp.schema import ToolDef
from .rows import drivers, fetch_row
from .windows import PERIODS, key, parse_date, period_windows, refusal

log = logging.getLogger("bot")

TOOL = ToolDef(
    "period_returns",
    "Total return of a Sharesight portfolio for each day, week, month, quarter or year, with the best and "
    "worst one and the holdings behind them. Use it for best/worst day, week, month, quarter or year, or any "
    "period-by-period question, instead of fetching one get_performance_report per period. Sold holdings are "
    "included. Omit start for 'since inception'. History that isn't cached yet is fetched live, which is slow "
    "for days and weeks: if the tool refuses a range, tell the user why and offer the start it suggests.",
    {"type": "object", "required": ["portfolio_id"], "properties": {
        "portfolio_id": {"type": "number", "description": "From list_portfolios"},
        "period": {"type": "string", "enum": list(PERIODS), "description": "Default month"},
        "start": {"type": "string", "description": "First day: YYYY-MM-DD, YYYY-MM or YYYY (default: inception)"},
        "end": {"type": "string", "description": "Last day, same formats (default: today)"}}})


async def inception(server, portfolio_id: int) -> date | None:
    try:
        portfolios = json.loads(await server.call("list_portfolios", {})).get("portfolios") or []
    except (ValueError, AttributeError):
        return None
    for p in portfolios:
        if isinstance(p, dict) and p.get("id") == portfolio_id:
            return parse_date(p.get("inception_date") or "")
    return None


def sharesight(ctx: Ctx):
    """The MCP server that has a get_performance_report tool, if it is up."""
    return next((s for s in ctx.registry.up() if "get_performance_report" in s.fn_names.values()), None)


def tool(ctx: Ctx):
    """(definition, function) of period_returns."""

    async def periods(args: dict) -> str:
        server = sharesight(ctx)
        if server is None:
            return "Error: the portfolio data source isn't connected."
        try:
            pid = int(args.get("portfolio_id"))
        except (TypeError, ValueError):
            return "Error: give the portfolio_id (call list_portfolios first)."
        period = str(args.get("period") or "month").lower()
        if period not in PERIODS:
            return f"Error: period must be one of {', '.join(PERIODS)}."
        today = datetime.now(ctx.st.tz).date()
        last = min(parse_date(args["end"], end=True) or today, today) if args.get("end") else today
        first = parse_date(args["start"]) if args.get("start") else await inception(server, pid)
        if first is None:
            return "Error: couldn't tell when to start; pass start as YYYY-MM-DD."
        if first > last:
            return "Error: start is after end."
        windows = period_windows(period, first, last)
        if not windows:
            return "Error: no trading days in that range."
        cached = await asyncio.to_thread(ctx.store.period_get, pid, first.isoformat(), last.isoformat())
        have = {key(w): cached[key(w)] for w in windows if key(w) in cached}
        if refused := refusal(period, windows, set(have)):
            return "Error: " + refused
        todo = [w for w in windows if key(w) not in have]
        fetched = await asyncio.gather(*(fetch_row(server, pid, a, b) for _, a, b in todo))
        fresh = [r for r in fetched if r is not None]
        have.update({(r.start, r.end): r for r in fresh})
        if finished := [r for r in fresh if date.fromisoformat(r.end) < today]:  # never the unfinished period
            await asyncio.to_thread(ctx.store.period_put, finished)
        rows = [(label, have[key((label, a, b))]) for label, a, b in windows if key((label, a, b)) in have]
        missing = len(windows) - len(rows)
        if not rows:
            return "Error: no reports came back."
        best = max(rows, key=lambda r: r[1].pct)
        worst = min(rows, key=lambda r: r[1].pct)
        lines = [f"Total return per {period} ({rows[0][1].currency}), {rows[0][0]} to {rows[-1][0]}, "
                 "sold holdings included:"]
        lines += [f"{label} {r.pct:+.2f}% {r.gain:+,.0f}" for label, r in rows]
        lines.append(f"Best: {best[0]} {best[1].pct:+.2f}%. Worst: {worst[0]} {worst[1].pct:+.2f}%."
                     + (f" {missing} {period}s had no data." if missing else ""))
        # What moved the extremes (best/worst by percent and by money), so the answer can say why.
        extremes = dict.fromkeys(label for label, _ in (best, worst, max(rows, key=lambda r: r[1].gain),
                                                        min(rows, key=lambda r: r[1].gain)))
        for label, r in rows:
            if label in extremes and (text := drivers(r.movers)):
                lines.append(f"Holdings behind {label}: {text}")
        log.info("period_returns: %d %ss for portfolio %s (%d fetched)", len(rows), period, pid, len(todo))
        return "\n".join(lines)

    return TOOL, periods
