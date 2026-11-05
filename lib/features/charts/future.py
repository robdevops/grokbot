"""/future TICKER: analysts' low, average and high revenue and EPS for this and next fiscal year, drawn without the model."""

from __future__ import annotations

import html
import json
import logging
import math
import re

from ...context import Ctx
from ...mcp.results import table_records
from ...tickers import yahoo_symbol
from . import Charts, tool

log = logging.getLogger("bot")

ESTIMATES = "get_analyst_estimates"  # Yahoo tools, found by name on whichever MCP server has them
STATEMENTS = "get_financial_statements"
SOURCE = "Yahoo Finance consensus"
SYMBOLS = {"USD": "$", "AUD": "A$", "NZD": "NZ$", "CAD": "C$", "HKD": "HK$", "EUR": "€", "GBP": "£", "JPY": "¥"}
STEPS = ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K"))
DATE_RE = re.compile(r"\b(\d{4})-\d{2}-\d{2}\b")
LINES = (("Low", "low"), ("Average", "avg"), ("High", "high"))


def symbol_of(text: str) -> str:
    """"nvda", "$NXT.AX", "ASX:NXT" -> NVDA, NXT.AX, NXT.AX."""
    text = text.strip().lstrip("$").upper()
    market, colon, code = text.partition(":")
    return (yahoo_symbol(code.strip(), market) or code.strip()) if colon else text


def _num(x) -> float | None:
    return float(x) if isinstance(x, (int, float)) and not math.isnan(x) else None


def rows(payload: dict, key: str) -> dict[str, dict]:
    """The estimate rows of one kind ("earningsEstimate", "revenueEstimate") by period ("0y", "+1y")."""
    return {str(r.get("period")): r for r in table_records(payload.get(key))}


def fiscal_years(statements: str) -> list[str]:
    """Labels for last, this and next fiscal year: from the latest annual statement's date (2025-12-31 -> FY2025 is
    the last reported), else plain words."""
    head = statements.split("## Balance Sheet")[0]
    years = [int(y) for y in DATE_RE.findall(head)]
    if not years:
        return ["Last FY", "This FY", "Next FY"]
    last = max(years)
    return [f"FY{last}", f"FY{last + 1}", f"FY{last + 2}"]


def scale(values: list[float]) -> tuple[float, str]:
    top = max(abs(v) for v in values)
    return next(((div, s) for div, s in STEPS if top >= div), (1.0, ""))


def money(value: float, sym: str, div: float, suffix: str) -> str:
    v = value / div
    return f"{'-' if v < 0 else ''}{sym}{abs(v):,.{0 if abs(v) >= 100 else 1 if abs(v) >= 10 else 2}f}{suffix}"


def metric(est: dict[str, dict], actual_key: str) -> tuple[float, list[dict]] | None:
    """Last year's actual and this and next year's estimate rows, if all are there."""
    this, nxt = est.get("0y"), est.get("+1y")
    actual = _num((this or {}).get(actual_key))
    if not (this and nxt) or actual is None:
        return None
    if any(_num(r.get(k)) is None for r in (this, nxt) for _, k in LINES):
        return None
    return actual, [this, nxt]


def chart_args(title: str, years: list[str], actual: float, est: list[dict], div: float, show) -> dict:
    """Three lines (low, average, high) from last year's actual through this and next year's estimates."""
    series = []
    for name, key in LINES:
        ys = [actual] + [_num(r[key]) for r in est]
        series.append({"name": name, "x": years, "y": [round(y / div, 4) for y in ys], "total": show(ys[-1])})
    return {"title": title, "source": SOURCE, "series": series}


async def fast(ctx: Ctx, text: str, charts: Charts) -> str | None:
    """The caption for /future TICKER, its charts added to `charts`; None if the estimates are missing or unreadable."""
    server = next((s for s in ctx.registry.up() if ESTIMATES in s.fn_names.values()), None)
    ticker = symbol_of(text)
    if not ticker or not server:
        return None
    out = await server.call(ESTIMATES, {"ticker": ticker, "response_format": "json"})
    try:
        payload = json.loads(out)
    except ValueError:
        log.warning("/future %s: couldn't read the %s result (%s)", ticker, ESTIMATES, " ".join(out.split())[:300])
        return None
    revenue = metric(rows(payload, "revenueEstimate"), "yearAgoRevenue")
    eps = metric(rows(payload, "earningsEstimate"), "yearAgoEps")
    if not (revenue or eps):
        return None
    statements = await server.call(STATEMENTS, {"ticker": ticker}) if STATEMENTS in server.fn_names.values() else ""
    years = fiscal_years(statements)
    currency = str(((revenue or eps)[1][0]).get("currency") or "USD")
    sym = SYMBOLS.get(currency, f"{currency} ")
    _, make = tool(ctx, charts)
    lines = []
    if revenue:
        actual, est = revenue
        div, suffix = scale([actual] + [_num(r[k]) for r in est for _, k in LINES])
        args = chart_args(f"{ticker} revenue forecast ({sym}{suffix})", years, actual, est, div,
                          lambda v: money(v, sym, div, suffix))
        if (await make(args)).startswith("Chart "):
            lines.append(_line("Revenue", years, actual, est, lambda v: money(v, sym, div, suffix)))
    if eps:
        actual, est = eps
        args = chart_args(f"{ticker} EPS forecast ({sym.strip()})", years, actual, est, 1.0,
                          lambda v: money(v, sym, 1.0, ""))
        if (await make(args)).startswith("Chart "):
            lines.append(_line("EPS", years, actual, est, lambda v: money(v, sym, 1.0, "")))
    if not lines:
        return None
    log.info("/future %s: %d charts without the model", ticker, len(lines))
    return (f"<b>{html.escape(ticker)}</b> analyst consensus, low / average / high\n" + "\n".join(lines) +
            f"\n<i>{html.escape(years[0])} is actual (EPS as analysts count it, usually adjusted). Source: {SOURCE}; "
            "Yahoo has nothing past next fiscal year.</i>")


def _line(name: str, years: list[str], actual: float, est: list[dict], show) -> str:
    parts = [f"{years[0]} {show(actual)}"]
    for label, r in zip(years[1:], est, strict=True):
        n = _num(r.get("numberOfAnalysts"))
        who = f", {int(n)} analysts" if n else ""
        parts.append(f"{label} {show(_num(r['low']))} / {show(_num(r['avg']))} / {show(_num(r['high']))}{who}")
    return f"{name}: " + "; ".join(parts)
