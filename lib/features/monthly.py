"""monthly_returns: a Sharesight portfolio's return per month, fetched by the bot in one tool call
instead of one model-driven report per month (64 calls and 290k tokens for "best month ever")."""

from __future__ import annotations

import asyncio
import calendar
import json
import logging
from datetime import date, datetime

from ..context import Ctx
from ..mcp.schema import ToolDef

log = logging.getLogger("bot")

MAX_MONTHS = 120
TOOL = ToolDef(
    "monthly_returns",
    "Total return of a Sharesight portfolio for each month, with the best and worst month. Use it for "
    "best/worst month, month-by-month or year-by-year questions, instead of fetching one "
    "get_performance_report per period. Sold holdings are included. Omit start for 'since inception'.",
    {"type": "object", "required": ["portfolio_id"], "properties": {
        "portfolio_id": {"type": "number", "description": "From list_portfolios"},
        "start": {"type": "string", "description": "First month, YYYY-MM (default: the portfolio's inception)"},
        "end": {"type": "string", "description": "Last month, YYYY-MM (default: this month)"}}})


def _month(text: str) -> tuple[int, int] | None:
    try:
        d = datetime.strptime(str(text).strip()[:7], "%Y-%m")
    except ValueError:
        return None
    return d.year, d.month


def month_windows(first: tuple[int, int], last: tuple[int, int], today: date) -> list[tuple[str, date, date]]:
    """(label, first day, last day) per month from `first` to `last`; the current month ends today."""
    out, (y, m) = [], first
    while (y, m) <= last and len(out) < MAX_MONTHS:
        end = min(date(y, m, calendar.monthrange(y, m)[1]), today)
        out.append((f"{y}-{m:02d}", date(y, m, 1), end))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _report(text: str) -> dict | None:
    try:
        report = json.loads(text).get("report")
    except (ValueError, AttributeError):
        return None
    return report if isinstance(report, dict) and report.get("total_gain_percent") is not None else None


async def _inception(server, portfolio_id: int) -> tuple[int, int] | None:
    try:
        portfolios = json.loads(await server.call("list_portfolios", {})).get("portfolios") or []
    except (ValueError, AttributeError):
        return None
    for p in portfolios:
        if isinstance(p, dict) and p.get("id") == portfolio_id:
            return _month(p.get("inception_date") or "")
    return None


def tool(ctx: Ctx):
    """(definition, function) of monthly_returns, backed by the Sharesight server (the one with a
    get_performance_report tool)."""

    async def monthly(args: dict) -> str:
        server = next((s for s in ctx.registry.up() if "get_performance_report" in s.fn_names.values()), None)
        if server is None:
            return "Error: the portfolio data source isn't connected."
        try:
            pid = int(args.get("portfolio_id"))
        except (TypeError, ValueError):
            return "Error: give the portfolio_id (call list_portfolios first)."
        today = datetime.now(ctx.st.tz).date()
        first = _month(args["start"]) if args.get("start") else await _inception(server, pid)
        last = (_month(args["end"]) if args.get("end") else None) or (today.year, today.month)
        if first is None:
            return "Error: couldn't tell when the portfolio started; pass start as YYYY-MM."
        windows = month_windows(first, last, today)
        if not windows:
            return "Error: no months in that range."
        results = await asyncio.gather(*(server.call("get_performance_report", {
            "portfolio_id": pid, "start_date": a.isoformat(), "end_date": b.isoformat(),
            "_keep_sales": True}) for _, a, b in windows))
        rows, missing = [], 0
        for (label, _, _), text in zip(windows, results, strict=True):
            report = _report(text)
            if report is None:
                missing += 1
                continue
            rows.append((label, float(report["total_gain_percent"]), float(report.get("total_gain") or 0),
                         report.get("currency") or ""))
        if not rows:
            return "Error: no monthly reports came back."
        best, worst = max(rows, key=lambda r: r[1]), min(rows, key=lambda r: r[1])
        lines = [f"Total return per month ({rows[0][3]}), {rows[0][0]} to {rows[-1][0]}, sold holdings included:"]
        lines += [f"{label} {pct:+.2f}% {gain:+,.0f}" for label, pct, gain, _ in rows]
        lines.append(f"Best: {best[0]} {best[1]:+.2f}%. Worst: {worst[0]} {worst[1]:+.2f}%."
                     + (f" {missing} months had no data." if missing else ""))
        log.info("monthly_returns: %d months for portfolio %s", len(rows), pid)
        return "\n".join(lines)

    return TOOL, monthly
