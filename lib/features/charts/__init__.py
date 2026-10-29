"""make_chart: the model draws a chart (two y axes, either inverted) that is sent as a photo with its reply.
Series are values the model passes, or a ticker whose price history the bot fetches from the Yahoo server."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from datetime import date, datetime

from ...context import Ctx
from ...mcp.schema import ToolDef
from . import render
from .render import KINDS, Chart, Series

log = logging.getLogger("bot")

CHART_WORDS = re.compile(r"\b(charts?|charting|graphs?|plot|plots|plotting|visuali[sz]e)\b", re.I)
PERIODS = ("1d", "5d", "1mo", "3mo", "6mo", "ytd", "1y", "2y", "5y", "10y", "max")
# Yahoo's server returns at most ~90 rows in one call, so each period gets an interval that fits.
DEFAULT_INTERVAL = {"1d": "5m", "5d": "30m", "1mo": "1d", "3mo": "1d", "6mo": "1wk", "1y": "1wk",
                    "2y": "1mo", "5y": "1mo", "10y": "3mo", "max": "3mo"}
MAX_CHARTS, MAX_SERIES, MAX_POINTS = 3, 8, 2000
TOTAL_MAX = 24  # characters of a legend total
CAPTION_HINT = 900  # Telegram captions hold 1024 characters; a longer reply continues in a text message
HISTORY_TOOL = "get_historical_prices"

TOOL = ToolDef(
    "make_chart",
    "Draw a chart, sent as a photo with your reply; use it when asked for a chart, graph or plot. Series go on the "
    "left or right y axis (different scales on different axes); invert an axis when asked or to show an inverse "
    "relationship. A series is a ticker (any Yahoo symbol: NVDA, CBA.AX, ^GSPC, AUDUSD=X, GC=F, BTC-USD), whose "
    "price history is fetched for you, or x/y values from tool results or the chat, never invented. Then comment "
    "on what it shows; don't describe the chart.",
    {"type": "object", "required": ["title", "series"], "properties": {
        "title": {"type": "string"},
        "series": {"type": "array", "items": {"type": "object", "required": ["name"], "properties": {
            "name": {"type": "string"},
            "ticker": {"type": "string"},
            "period": {"type": "string", "enum": list(PERIODS), "description": "For a ticker; default 1y"},
            "x": {"type": "array", "items": {"type": ["string", "number"]}, "description": "Dates, numbers or labels"},
            "y": {"type": "array", "items": {"type": ["number", "null"]}},
            "axis": {"type": "string", "enum": ["left", "right"]},
            "kind": {"type": "string", "enum": list(KINDS)},
            "percent_change": {"type": "boolean", "description": "Plot % change since the first point"},
            "total": {"type": "string", "description": "For the legend, e.g. the total return from the data"}}}},
        "left_label": {"type": "string"},
        "right_label": {"type": "string"},
        "invert_left": {"type": "boolean"},
        "invert_right": {"type": "boolean"}}})


class ChartError(ValueError):
    """A problem with the request, reported to the model so it can fix the call."""


def wants_chart(text: str) -> bool:
    """Does this message ask for a chart? Only then is the tool offered."""
    return bool(CHART_WORDS.search(text))


def default_interval(period: str, today: date) -> str:
    if period == "ytd":  # daily while the year is short enough to fit
        return "1d" if (today - date(today.year, 1, 1)).days <= 120 else "1wk"
    return DEFAULT_INTERVAL.get(period, "1d")


def as_date(v) -> datetime | None:
    if not isinstance(v, str):
        return None
    s = v.strip()
    if re.fullmatch(r"\d{4}-\d{2}", s):
        s += "-01"
    try:
        return datetime.fromisoformat(s).replace(tzinfo=None)
    except ValueError:
        return None


def as_number(v) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(v) if isinstance(v, str) and v.strip() else None
    except ValueError:
        return None


def as_y(values) -> list[float]:
    out = []
    for v in values:
        n = None if v is None else as_number(v)
        if v is not None and n is None:
            raise ChartError(f"y values must be numbers, got {v!r}")
        out.append(math.nan if n is None or not math.isfinite(n) else n)
    return out


def unify_x(series: list[Series]) -> list[str]:
    """Turns every series' x into datetimes or numbers if all of them parse that way, else into labels; returns the
    label order (empty unless labels)."""
    raw = [v for s in series for v in s.x]
    dated = [as_date(v) is not None for v in raw]
    if any(dated) and not all(dated):
        raise ChartError("x values must be all dates, all numbers or all labels")
    if all(dated):
        for s in series:
            s.x = [as_date(v) for v in s.x]
        return []
    if all(as_number(v) is not None for v in raw):
        for s in series:
            s.x = [as_number(v) for v in s.x]
        return []
    labels: list[str] = []
    for s in series:
        s.x = [str(v) for v in s.x]
        labels += [v for v in s.x if v not in labels]
    return labels


def total(s: Series, spec: dict) -> str:
    """The figure after a series' name in the legend: the model's own, else the change over the chart for a % change
    or a ticker's prices; other values have no total to assume."""
    given = str(spec.get("total") or "").strip()
    values = [v for v in s.y if not math.isnan(v)]
    if given or not values:
        return given if len(given) <= TOTAL_MAX else (given[:TOTAL_MAX].rsplit(" ", 1)[0] or given[:TOTAL_MAX])
    if spec.get("percent_change"):
        return f"{values[-1]:+.1f}%"
    if spec.get("ticker") and values[0]:
        return f"{(values[-1] / values[0] - 1) * 100:+.1f}%"
    return ""


def percent_change(y: list[float]) -> list[float]:
    base = next((v for v in y if not math.isnan(v)), math.nan)
    if math.isnan(base) or base == 0:
        raise ChartError("percent_change needs a non-zero first value")
    return [(v / base - 1) * 100 for v in y]


async def price_history(ctx: Ctx, ticker: str, period: str, interval: str) -> tuple[list[str], list[float], str]:
    """Closing prices of a Yahoo symbol from the MCP server that has get_historical_prices; a note if cut short."""
    server = next((s for s in ctx.registry.up() if HISTORY_TOOL in s.fn_names.values()), None)
    if server is None:
        raise ChartError("price history isn't available right now; pass x and y values instead")
    try:
        out = await server.call(HISTORY_TOOL, {"ticker": ticker, "period": period, "interval": interval,
                                               "response_format": "json"})
    except TimeoutError:
        raise ChartError(f"{ticker}: the price history timed out") from None
    try:
        data = json.loads(out)
        rows = [r for r in data["data"] if isinstance(r, dict) and r.get("Close") is not None]
    except (ValueError, KeyError, TypeError):
        raise ChartError(f"{ticker}: {' '.join(out.split())[:200]}") from None
    if not rows:
        raise ChartError(f"{ticker}: no price history for period {period}, interval {interval}")
    note = f" (newest {data.get('returnedRecords')} of {data.get('totalRecords')})" if data.get("truncated") else ""
    return [r.get("Date") or r.get("Datetime") or "" for r in rows], [r["Close"] for r in rows], note


async def build(ctx: Ctx, args: dict) -> tuple[Chart, list[str]]:
    """The chart a make_chart call asks for, and a line per series for the model."""
    title = str(args.get("title") or "").strip()
    specs = args.get("series")
    if not title or not isinstance(specs, list) or not specs:
        raise ChartError("give a title and at least one series")
    if len(specs) > MAX_SERIES:
        raise ChartError(f"at most {MAX_SERIES} series per chart")
    today = datetime.now(ctx.st.tz).date()
    fetches, series, notes = [], [], [""] * len(specs)
    for i, spec in enumerate(specs):
        if not isinstance(spec, dict) or not str(spec.get("name") or "").strip():
            raise ChartError(f"series {i + 1} needs a name")
        axis, kind = spec.get("axis") or "left", spec.get("kind") or "line"
        if axis not in ("left", "right") or kind not in KINDS:
            raise ChartError(f"{spec['name']}: axis is left or right, kind is one of {', '.join(KINDS)}")
        series.append(Series(str(spec["name"]).strip(), [], [], axis, kind))
        if spec.get("ticker"):
            period = spec.get("period") or "1y"
            if period not in PERIODS:
                raise ChartError(f"{spec['name']}: period is one of {', '.join(PERIODS)}")
            fetches.append((i, price_history(ctx, str(spec["ticker"]).strip(), period, default_interval(period, today))))
        else:
            x, y = spec.get("x"), spec.get("y")
            if not isinstance(x, list) or not isinstance(y, list) or not x or len(x) != len(y):
                raise ChartError(f"{spec['name']}: give a ticker, or x and y lists of the same length")
            if len(x) > MAX_POINTS:
                raise ChartError(f"{spec['name']}: at most {MAX_POINTS} points")
            series[i].x, series[i].y = list(x), as_y(y)
    for (i, _), got in zip(fetches, await asyncio.gather(*(f for _, f in fetches)), strict=True):
        series[i].x, series[i].y, notes[i] = got[0], as_y(got[1]), got[2]
    labels = unify_x(series)
    for s, spec in zip(series, specs, strict=True):
        if spec.get("percent_change"):
            s.y = percent_change(s.y)
        s.total = total(s, spec)
    chart = Chart(title, series, str(args.get("left_label") or ""), str(args.get("right_label") or ""),
                  bool(args.get("invert_left")), bool(args.get("invert_right")), labels)
    for side in ("left", "right"):  # an axis of nothing but % changes labels its ticks with %
        on_side = [spec for s, spec in zip(series, specs, strict=True) if s.axis == side]
        setattr(chart, f"{side}_percent", bool(on_side) and all(spec.get("percent_change") for spec in on_side))
    return chart, [summary(s, specs[i].get("percent_change"), notes[i]) for i, s in enumerate(series)]


def _fmt(v: float) -> str:
    return f"{v:,.4g}" if abs(v) < 1000 else f"{v:,.0f}"


def _x(v) -> str:
    return v.strftime("%Y-%m-%d %H:%M").removesuffix(" 00:00") if isinstance(v, datetime) else str(v)


def summary(s: Series, pct: bool, note: str) -> str:
    """First and last point, change, low and high: the model hasn't seen fetched prices."""
    points = [(x, y) for x, y in zip(s.x, s.y, strict=True) if not math.isnan(y)]
    if not points:
        return f"{s.name}: no values"
    (x0, y0), (x1, y1) = points[0], points[-1]
    unit = "%" if pct else ""
    change = "" if pct or y0 == 0 else f" ({(y1 / y0 - 1) * 100:+.1f}%)"
    low, high = min(y for _, y in points), max(y for _, y in points)
    return (f"{s.name} ({s.axis} axis, {len(points)} points): {_x(x0)} {_fmt(y0)}{unit} -> {_x(x1)} {_fmt(y1)}{unit}"
            f"{change}, low {_fmt(low)}{unit}, high {_fmt(high)}{unit}{note}")


class Charts:
    """The photos made while answering one message; the handler sends them in one reply, the text as caption."""

    def __init__(self):
        self.photos: list[tuple[bytes, str]] = []
        self.calls: list[dict] = []  # the make_chart arguments behind each photo, to redraw it from


def tool(ctx: Ctx, charts: Charts):
    """(definition, function) of make_chart, collecting the PNGs into `charts`."""

    async def make(args: dict) -> str:
        if len(charts.photos) >= MAX_CHARTS:
            return f"Error: at most {MAX_CHARTS} charts per reply."
        began = time.monotonic()
        try:
            chart, lines = await build(ctx, args)
            image = await asyncio.to_thread(render.png, chart)
        except ChartError as e:
            return f"Error: {e}"
        charts.photos.append((image, chart.title))
        charts.calls.append(args)
        log.debug("Chart %r: %d series in %.1f s", chart.title, len(chart.series), time.monotonic() - began)
        return (f"Chart {len(charts.photos)} is ready. Your reply becomes its caption, so keep the reply under "
                f"{CAPTION_HINT} characters, and don't describe the chart or say you made one: say what it shows.\n"
                + "\n".join(lines))

    return TOOL, make
