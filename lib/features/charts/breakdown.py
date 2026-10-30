"""Fast path for "TSLA revenue breakdown": one get_segments call, one pie per grouping, no model."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re

from ...context import Ctx
from ...tickers import is_ticker
from . import MAX_CHARTS, Charts, subtotal_in, tool

log = logging.getLogger("bot")

STATEMENTS = "get_statements"
MIN_PIES = 3  # fewer than this from get_segments and the income statement is searched too
TOOL = "get_segments"  # the TickerScout tool, found by name on whichever MCP server has it
ASKS = re.compile(r"\b(?:breakdowns?|segments?|break\s+down)\b", re.IGNORECASE)
# What may surround the ticker in a plain request; anything else is a question for the model.
FILLER = {"revenue", "revenues", "breakdown", "breakdowns", "break", "down", "segment", "segments", "by", "of", "the",
          "for", "show", "me", "a", "pie", "pies", "chart", "charts", "donut", "donuts", "please", "latest", "its",
          "s", "and", "give", "get", "what", "is", "are"}
TICKER = re.compile(r"^[A-Z]{1,5}(?:[.-][A-Z]{1,3})?$")
CURRENCIES = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥"}
UNITS = (("actual dollars", 1.0), ("thousand", 1e3), ("million", 1e6), ("billion", 1e9))  # of the "units" text
STEPS = ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K"))


def requested_ticker(text: str) -> str | None:
    """The ticker of a plain "NVDA revenue breakdown" / "segments for NVDA" message, else None: a message with
    other words in it (a period, a comparison, a question) needs the model."""
    if not ASKS.search(text):
        return None
    words = re.sub(r"@\w+", " ", text).replace("$", " ").replace("'", " ")
    found = [w for w in re.findall(r"[A-Za-z][A-Za-z.-]*", words) if w.lower() not in FILLER]
    if len(found) != 1:
        return None
    ticker = found[0].upper().strip(".-")
    return ticker if TICKER.match(ticker) and is_ticker(ticker) else None


REVENUE_KEY = re.compile(r"^(?:net_?)?(?:revenues?|sales)(?:_usd)?$", re.IGNORECASE)
NAME_KEYS = ("name", "segment", "label", "category", "product", "region", "geography")
WANTED = ("revenue", "segment", "geograph", "product", "region", "customer")  # in the path of a grouping that is one
UNWANTED = ("income", "profit", "margin", "asset", "cost", "expense", "capex", "ebit", "depreciation", "growth", "percent")


def revenue_of(value) -> float | None:
    """A slice's revenue: a bare number, or the revenue field of a {revenue: ..., operating_income: ...} object."""
    if isinstance(value, dict):
        value = next((v for k, v in value.items() if REVENUE_KEY.match(k)), None)
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return float(value)
    return None


def pretty(name: str) -> str:
    """A snake_case key as a slice name: automotive_segment is "Automotive", energy_generation_and_storage_segment
    is "Energy generation and storage". Names that are already words are left alone."""
    if "_" not in name or " " in name:
        return name
    words = name.replace("_", " ").strip()
    words = re.sub(r"\s+(?:segment|revenues?)$", "", words, flags=re.IGNORECASE) or words
    return words[:1].upper() + words[1:]


def slices_of(group) -> list[tuple[str, float]]:
    """(name, value) of a grouping: {name: value}, {name: {"revenue": value, ...}} or [{"name": ..., "revenue": ...}].
    Where the entries are objects, a bare number beside them (a total operating income) is no slice."""
    if isinstance(group, list):
        items = [(next((str(i[k]) for k in NAME_KEYS if isinstance(i.get(k), str)), ""), revenue_of(i))
                 for i in group if isinstance(i, dict)]
    else:
        nested = any(isinstance(v, dict) for v in group.values())
        items = [(str(n), revenue_of(v)) for n, v in group.items() if isinstance(v, dict) or not nested]
    out = [(pretty(n), v) for n, v in items if n and v and not re.search(r"\btotal\b", n.replace("_", " "), re.I)]
    while (overlap := subtotal_in(out)):  # a subtotal beside its parts would be counted twice
        out = [(n, v) for n, v in out if n != overlap[0]]
    return out


def label_of(path: list[str]) -> str:
    """What a grouping is split by, from where it was found: revenue_by_market_platform is "market platform"."""
    last = path[-1].lower()
    if last.startswith("revenue_by_"):
        return last[len("revenue_by_"):].replace("_", " ")
    return "segment" if "segment" in last else last.replace("_", " ")


def groupings(period: dict, factor: float = 1.0) -> list[tuple[str, list[tuple[str, float]]]]:
    """The pies of one period (or of a whole result), as (label, slices): every group of two or more revenues found at
    any depth whose path says it is about revenue, segments, products or regions, and whose own name is not about
    income, profit or the like."""
    found, seen = [], []

    def walk(node, path: list[str]) -> None:
        if isinstance(node, (dict, list)) and path:
            joined = "/".join(path).lower()
            slices = slices_of(node)
            if len(slices) >= 2 and any(w in joined for w in WANTED) and not any(w in path[-1].lower() for w in UNWANTED):
                if slices not in seen:
                    seen.append(slices)
                    found.append((label_of(path), [(n, v * factor) for n, v in slices]))
                return
        children = node.items() if isinstance(node, dict) else enumerate(node) if isinstance(node, list) else ()
        for key, child in children:
            if isinstance(child, (dict, list)):
                walk(child, path + [str(key)] if isinstance(node, dict) else path)

    walk(period, [])
    return found[:MAX_CHARTS]


def scaled(slices: list[tuple[str, float]]) -> tuple[list[tuple[str, float]], str]:
    """The values in the unit that suits the biggest one, and that unit's letter (B for billions)."""
    biggest = max(v for _, v in slices)
    divisor, suffix = next(((d, s) for d, s in STEPS if biggest >= d), (1.0, ""))
    return [(n, v / divisor) for n, v in slices], suffix


def caption(ticker: str, period: str, label: str, slices: list[tuple[str, float]], symbol: str) -> str:
    """One line: the period's total and the biggest slice of the first pie."""
    values, suffix = scaled(slices)
    whole = sum(v for _, v in values)
    top_name, top = max(values, key=lambda s: s[1])
    return (f"<b>{html.escape(ticker)}</b> {html.escape(period)} revenue {symbol}{whole:,.1f}{suffix}: "
            f"{html.escape(top_name)} is {top / whole * 100:.0f}% of the {label} split.")


def read_units(payload: dict, default: float) -> float:
    """The multiplier that turns a result's figures into dollars, from its "units" text."""
    return next((f for key, f in UNITS if key in str(payload.get("units", "")).lower()), default)


def new_only(found: list, have: list) -> list:
    """The groupings of `found` that no pie of `have` (or earlier in `found`) already shows."""
    out: list = []
    for label, slices in found:
        if all(slices != other for _, other in have + out):
            out.append((label, slices))
    return out


def parsed(ticker: str, key: str, out) -> dict | None:
    """A result as JSON, or None (a warning says why) when it isn't, or when the company's record lacks the block."""
    try:
        data = json.loads(out)
    except (TypeError, ValueError):
        log.warning("Fast breakdown for %s: couldn't read %s: %s", ticker, key, " ".join(str(out).split())[:200])
        return None
    if not isinstance(data, dict) or data.get("available") is False:
        log.debug("Fast breakdown for %s: %s isn't available: %s", ticker, key, isinstance(data, dict) and data.get("message"))
        return None
    return data


async def more_groupings(server, ticker: str, name: str, payload: dict, have: list, factor: float) -> list:
    """What "all" left out. Breakdowns that `showing` lists (revenue_by_product and the like) but gave no pie are
    asked for, all at once; a company's record may not carry them ("available": false). With fewer than three pies
    then, the income statement is searched for revenue lines split by product or region."""
    labels = {label for label, _ in have}
    missing = [k for k in payload.get("showing") or [] if isinstance(k, str) and k.startswith("revenue_by_")
               and label_of([k]) not in labels]
    outs = await asyncio.gather(*(server.call(TOOL, {"ticker": ticker, "breakdown": k, "period": name}) for k in missing),
                                return_exceptions=True)
    extra: list = []
    for key, out in zip(missing, outs, strict=True):
        if data := parsed(ticker, key, out):
            extra += new_only(groupings(data, read_units(data, factor)), have + extra)
    if len(have) + len(extra) < MIN_PIES and STATEMENTS in server.fn_names.values():
        out = await server.call(STATEMENTS, {"ticker": ticker, "statement": "income_statement", "period": name})
        if data := parsed(ticker, "income_statement", out):
            extra += new_only(groupings(data, read_units(data, factor)), have + extra)
    return extra


async def fast(ctx: Ctx, text: str, charts: Charts) -> str | None:
    """The caption for a plain revenue-breakdown request, its pies added to `charts`; None when this isn't one,
    the data source is missing or down, or nothing could be drawn (the model then answers as usual)."""
    ticker = requested_ticker(text)
    server = next((s for s in ctx.registry.up() if TOOL in s.fn_names.values()), None)
    if not ticker or not server:
        return None
    out = await server.call(TOOL, {"ticker": ticker, "breakdown": "all", "period": "latest quarter"})
    try:
        payload = json.loads(out)
        period = payload["periods"][0]
        symbol = CURRENCIES.get(str(payload.get("reporting_currency")))
        factor = read_units(payload, 0.0)
    except (ValueError, KeyError, IndexError, TypeError):
        log.warning("Fast breakdown for %s: couldn't read the %s result (%s)", ticker, TOOL, " ".join(out.split())[:200])
        return None
    if not symbol or not factor:
        log.warning("Fast breakdown for %s: currency %r or units %r not understood", ticker,
                    payload.get("reporting_currency"), payload.get("units"))
        return None
    name = str(period.get("period") or "latest quarter")
    found = groupings(period, factor)
    found += await more_groupings(server, ticker, name, payload, found, factor)
    _, make = tool(ctx, charts)
    first = None
    for label, slices in found[:MAX_CHARTS]:
        values, suffix = scaled(slices)
        args = {"title": f"{ticker} {name} revenue by {label} ({symbol}{suffix})",
                "series": [{"name": f"Revenue by {label}", "kind": "pie",
                            "x": [n for n, _ in values], "y": [round(v, 3) for _, v in values]}]}
        if (await make(args)).startswith("Chart ") and first is None:
            first = (label, slices)
    if first is None:
        log.warning("Fast breakdown for %s: no pie could be drawn from the period: %s", ticker,
                    json.dumps(period, separators=(",", ":"))[:600])
        return None
    log.info("Fast path: %d revenue pies for %s without the model", len(charts.photos), ticker)
    return caption(ticker, name, *first, symbol)
