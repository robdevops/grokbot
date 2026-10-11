"""period_returns: a Sharesight portfolio's return per day, week, month, quarter or year, fetched by the bot
in one tool call instead of one model-driven report per period (64 calls and 290k tokens for "best month ever")."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, timedelta

from ..context import Ctx
from ..mcp.schema import ToolDef

log = logging.getLogger("bot")

PERIODS = ("day", "week", "month", "quarter", "year")
MAX_PERIODS = 120  # reports one call may fetch (about 2.3 s each)
MAX_WEEKS = 26
MAX_DAYS = 90
TOOL = ToolDef(
    "period_returns",
    "Total return of a Sharesight portfolio for each day, week, month, quarter or year, with the best and "
    "worst one. Use it for best/worst day, week, month, quarter or year, or any period-by-period question, "
    "instead of fetching one get_performance_report per period. Sold holdings are included. Month, quarter "
    "and year can cover the whole history (omit start for 'since inception'); day and week only recent "
    f"windows (last {MAX_DAYS} days, {MAX_WEEKS} weeks): if the tool refuses a longer range, tell the user the limit.",
    {"type": "object", "required": ["portfolio_id"], "properties": {
        "portfolio_id": {"type": "number", "description": "From list_portfolios"},
        "period": {"type": "string", "enum": list(PERIODS), "description": "Default month"},
        "start": {"type": "string", "description": "First day: YYYY-MM-DD, YYYY-MM or YYYY (default: inception)"},
        "end": {"type": "string", "description": "Last day, same formats (default: today)"}}})


def _date(text: str, *, end: bool = False) -> date | None:
    """A date from YYYY-MM-DD, YYYY-MM or YYYY; a partial one means its first day, or its last for `end`."""
    text = str(text).strip()
    for fmt, size in (("%Y-%m-%d", 10), ("%Y-%m", 7), ("%Y", 4)):
        try:
            d = datetime.strptime(text, fmt).date()
        except ValueError:
            continue
        if end and size < 10:
            d = (date(d.year + 1, 1, 1) if size == 4 else date(d.year + (d.month == 12), d.month % 12 + 1, 1)) \
                - timedelta(days=1)
        return d
    return None


def _month_add(d: date, months: int) -> date:
    n = d.year * 12 + d.month - 1 + months
    return date(n // 12, n % 12 + 1, 1)


def period_windows(period: str, first: date, last: date) -> list[tuple[str, date, date]]:
    """(label, first day, last day) of each period between `first` and `last`; the ends are clipped to them."""
    out: list[tuple[str, date, date]] = []
    if period == "day":
        d = first
        while d <= last:
            if d.weekday() < 5:  # no trading at weekends
                out.append((d.isoformat(), d, d))
            d += timedelta(days=1)
    elif period == "week":
        monday = first - timedelta(days=first.weekday())
        while monday <= last:
            iso = monday.isocalendar()
            out.append((f"{iso[0]}-W{iso[1]:02d}", max(monday, first), min(monday + timedelta(days=6), last)))
            monday += timedelta(days=7)
    else:
        step = {"month": 1, "quarter": 3, "year": 12}[period]
        cur = date(first.year, ((first.month - 1) // step) * step + 1, 1)
        while cur <= last:
            nxt = _month_add(cur, step)
            label = {"month": f"{cur.year}-{cur.month:02d}", "quarter": f"{cur.year}-Q{(cur.month - 1) // 3 + 1}",
                     "year": str(cur.year)}[period]
            out.append((label, max(cur, first), min(nxt - timedelta(days=1), last)))
            cur = nxt
    return out


def _report(text: str) -> dict | None:
    try:
        report = json.loads(text).get("report")
    except (ValueError, AttributeError):
        return None
    return report if isinstance(report, dict) and report.get("total_gain_percent") is not None else None


async def _inception(server, portfolio_id: int) -> date | None:
    try:
        portfolios = json.loads(await server.call("list_portfolios", {})).get("portfolios") or []
    except (ValueError, AttributeError):
        return None
    for p in portfolios:
        if isinstance(p, dict) and p.get("id") == portfolio_id:
            return _date(p.get("inception_date") or "")
    return None


def _too_long(period: str, first: date, last: date, count: int) -> str | None:
    """Why this range is refused (with the earliest start that is allowed), or None."""
    if period == "day" and (last - first).days + 1 > MAX_DAYS:
        return (f"daily returns are limited to the last {MAX_DAYS} days; "
                f"ask again with start={last - timedelta(days=MAX_DAYS - 1)} or later")
    if period == "week" and count > MAX_WEEKS:
        monday = last - timedelta(days=last.weekday())
        return (f"weekly returns are limited to {MAX_WEEKS} weeks; "
                f"ask again with start={monday - timedelta(weeks=MAX_WEEKS - 1)} or later")
    if count > MAX_PERIODS:
        return f"that is {count} {period}s; the limit is {MAX_PERIODS}. Use a shorter range or a longer period"
    return None


def tool(ctx: Ctx):
    """(definition, function) of period_returns, backed by the Sharesight server (the one with a
    get_performance_report tool)."""

    async def periods(args: dict) -> str:
        server = next((s for s in ctx.registry.up() if "get_performance_report" in s.fn_names.values()), None)
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
        last = min(_date(args["end"], end=True) or today, today) if args.get("end") else today
        if args.get("start"):
            first = _date(args["start"])
        elif period == "day":
            first = last - timedelta(days=MAX_DAYS - 1)
        elif period == "week":
            first = last - timedelta(days=last.weekday()) - timedelta(weeks=MAX_WEEKS - 1)
        else:
            first = await _inception(server, pid)
        if first is None:
            return "Error: couldn't tell when to start; pass start as YYYY-MM-DD."
        if first > last:
            return "Error: start is after end."
        windows = period_windows(period, first, last)
        if refused := _too_long(period, first, last, len(windows)):
            return "Error: " + refused
        if not windows:
            return "Error: no trading days in that range."
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
            return "Error: no reports came back."
        best, worst = max(rows, key=lambda r: r[1]), min(rows, key=lambda r: r[1])
        lines = [f"Total return per {period} ({rows[0][3]}), {rows[0][0]} to {rows[-1][0]}, sold holdings included:"]
        lines += [f"{label} {pct:+.2f}% {gain:+,.0f}" for label, pct, gain, _ in rows]
        lines.append(f"Best: {best[0]} {best[1]:+.2f}%. Worst: {worst[0]} {worst[1]:+.2f}%."
                     + (f" {missing} {period}s had no data." if missing else ""))
        log.info("period_returns: %d %ss for portfolio %s", len(rows), period, pid)
        return "\n".join(lines)

    return TOOL, periods
