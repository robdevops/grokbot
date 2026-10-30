"""make_chart: the model draws a chart (two y axes, either inverted) that is sent as a photo with its reply.
Series are values the model passes, or a ticker whose price history the bot fetches from the Yahoo server."""

from __future__ import annotations

import asyncio
import itertools
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

CHART_WORDS = re.compile(r"\b(charts?|charting|graphs?|plot|plots|plotting|pies?|donuts?|visuali[sz]e|breakdowns?|break down|segments?)\b", re.I)
PERIODS = ("1d", "5d", "1mo", "3mo", "6mo", "ytd", "1y", "2y", "5y", "10y", "max")
# Yahoo's server returns at most ~90 rows in one call, so each period gets an interval that fits.
DEFAULT_INTERVAL = {"1d": "5m", "5d": "30m", "1mo": "1d", "3mo": "1d", "6mo": "1wk", "1y": "1wk",
                    "2y": "1mo", "5y": "1mo", "10y": "3mo", "max": "3mo"}
MAX_CHARTS, MAX_SERIES, MAX_POINTS = 3, 8, 2000
NIGHT_FROM, NIGHT_TO = 21, 7  # the dark theme from 9 pm to 7 am (BOT_TZ), when the chart is drawn
TOTAL_MAX = 24  # characters of a legend total
CAPTION_HINT = 900  # Telegram captions hold 1024 characters; a longer reply continues in a text message
HISTORY_TOOL = "get_historical_prices"

TOOL = ToolDef(
    "make_chart",
    "Draw a chart, sent as a photo with your reply; use it when asked for a chart, graph, plot, breakdown or segments. Series go on the "
    "left or right y axis (different scales on different axes); invert an axis when asked or to show an inverse "
    "relationship. A series is a ticker (any Yahoo symbol: NVDA, CBA.AX, ^GSPC, AUDUSD=X, GC=F, BTC-USD), whose "
    "price history is fetched for you, or x/y values from tool results or the chat, never invented. For a share-of-"
    "total breakdown (revenue by segment or region; also whenever a breakdown or the segments are asked for) use kind pie: one series per chart, x the slice names and y their "
    "values, parts of one whole only: never a subtotal beside its parts, and where a line is split into sub-lines "
    "(Data Center into Hyperscale and AI Clouds) use the sub-lines, the finest split, not the subtotal. A source that returns several groupings "
    "(segments, products or platforms, regions) gets one pie for every grouping, not just the first: ask it for all "
    "of them, call this once per grouping, and the charts go out as one image with a button for each. Then comment on what it shows; don't describe the chart.",
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


def now(tz) -> datetime:
    return datetime.now(tz)


def is_night(hour: int) -> bool:
    return hour >= NIGHT_FROM or hour < NIGHT_TO


def date_range(first: datetime, last: datetime) -> str:
    """30 Sep - 28 Oct 2026 (the year once when it is the same), or a single date for one day."""
    def day(d: datetime, year: bool) -> str:
        return f"{d.day} {d:%b}" + (f" {d.year}" if year else "")
    if first.date() == last.date():
        return day(last, True)
    return f"{day(first, first.year != last.year)} - {day(last, True)}"


PERIOD_WORDS = {"1d": "the past day", "5d": "the past 5 days", "1mo": "the past month", "3mo": "the past 3 months",
                "6mo": "the past 6 months", "ytd": "the year to date", "1y": "the past year", "2y": "the past 2 years",
                "5y": "the past 5 years", "10y": "the past 10 years", "max": "all time"}


def subtitle_for(specs: list[dict], series: list[Series], pct: bool) -> str:
    """What the chart shows and when, like "% change over the past month, 30 Sep - 28 Oct 2026"."""
    periods = {spec.get("period") or "1y" for spec in specs if spec.get("ticker")}
    head = ""
    if len(periods) == 1 and all(spec.get("ticker") for spec in specs):
        head = ("% change" if pct else "Closing price") + f" over {PERIOD_WORDS[periods.pop()]}"
    elif pct:
        head = "% change"
    times = [x for s in series for x in s.x if isinstance(x, datetime)]
    return ", ".join(filter(None, (head, date_range(min(times), max(times)) if times else "")))


PIE_WORDS = re.compile(r"\b(pies?|donuts?|breakdowns?|break down|segments?)\b", re.I)


def asks_pie(text: str) -> bool:
    return bool(PIE_WORDS.search(text))


ALIASES = {"x_values": "x", "labels": "x", "names": "x", "categories": "x",
           "y_values": "y", "values": "y", "data": "y"}  # what models call a series' x and y when they forget


def unquote_series(args: dict) -> dict:
    """Weaker models send `series` as a string holding the JSON, or as one object instead of a list, and
    name x and y something else: read all of those."""
    specs = args.get("series")
    if isinstance(specs, str):
        try:
            specs = json.loads(specs)
        except ValueError:
            return args
    if isinstance(specs, dict):
        specs = [specs]
    if not isinstance(specs, list):
        return args
    fixed = []
    for spec in specs:
        if isinstance(spec, dict):
            spec = {ALIASES.get(k, k): v for k, v in spec.items() if v is not None}
        fixed.append(spec)
    return {**args, "series": fixed}


def pie_default(args: dict, prefer_pie: bool) -> dict:
    """A breakdown was asked for, and the model drew one series of names and values as the default line chart: that
    is a pie. A kind it chose itself (bar, scatter, pie) stays."""
    specs = args.get("series")
    if not (prefer_pie and isinstance(specs, list) and specs and all(isinstance(s, dict) for s in specs)):
        return args
    if len(specs) > 1:
        return one_slice_per_series(args, specs)
    spec = specs[0]
    x = spec.get("x")
    if (spec.get("kind") in (None, "", "line") and not spec.get("ticker") and isinstance(x, list) and x
            and all(as_date(v) is None and as_number(v) is None for v in x)):
        return {**args, "series": [{**spec, "kind": "pie"}]}
    return args


def one_slice_per_series(args: dict, specs: list[dict]) -> dict:
    """The model drew each slice as a series of its own, one point each (a line chart of dots, named at their ends):
    several series that each hold one value, with no kind but line or pie, are the slices of one pie."""
    points = []
    for spec in specs:
        x, y = spec.get("x"), spec.get("y")
        if (spec.get("kind") not in (None, "", "line", "pie") or spec.get("ticker") or not isinstance(x, list)
                or not isinstance(y, list) or len(x) != 1 or len(y) != 1 or as_number(y[0]) is None):
            return args
        points.append((str(spec.get("name") or x[0]).strip(), y[0]))
    return {**args, "series": [{"name": "Share", "kind": "pie", "x": [n for n, _ in points],
                                "y": [v for _, v in points]}]}


def chart_hint(text: str) -> str:
    """A line for the end of the prompt: offering make_chart isn't enough, some models answer in text anyway."""
    if PIE_WORDS.search(text):
        return ("\n\nThey want this drawn, not described: fetch the data (every grouping the source has: segments, "
                "products or platforms, regions), then call make_chart with kind pie once for each grouping, using the "
                "finest split with no overlaps (a line's sub-lines, not their subtotal). Each pie is one series whose x are "
                "the slice names and y their values, not a series per slice.")
    return "\n\nThey want a chart: call make_chart rather than describing the numbers."


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


UNIT_RE = re.compile(r"\(\s*(A\$|US\$|[$€£¥])?\s*(trillion|billion|million|thousand|[KMBT]n?|bn|mn)?\s*\)", re.IGNORECASE)


def pie_total(s: Series, title: str) -> str:
    """The sum of a pie's slices for its middle, in the unit the title gives in brackets ("($B)", "(€ million)");
    nothing if the title names none, as a bare number could be anything."""
    found = UNIT_RE.search(title)
    if not found or not (found.group(1) or found.group(2)):
        return ""
    whole = sum(y for _, y in render.pie_slices(s))
    short = {"trillion": "T", "billion": "B", "million": "M", "thousand": "K", "bn": "B", "mn": "M"}
    unit = found.group(2) or ""
    unit = short.get(unit.lower(), unit)
    digits = f"{whole:,.0f}" if whole >= 1000 else f"{whole:,.1f}"
    return f"{found.group(1) or ''}{digits}{unit}"


def total(s: Series, spec: dict) -> str:
    """The figure next to a series' name: the change over the chart when the bot knows the values (a % change, or a
    ticker's prices), else the model's own, if it is a figure (it has a digit: models sometimes repeat the name);
    other values have no total to assume."""
    values = [v for v in s.y if not math.isnan(v)]
    if values and spec.get("percent_change"):
        return f"{values[-1]:+.1f}%"
    if values and values[0] and spec.get("ticker"):
        return f"{(values[-1] / values[0] - 1) * 100:+.1f}%"
    given = str(spec.get("total") or "").strip()
    if not any(c.isdigit() for c in given):
        return ""
    return given if len(given) <= TOTAL_MAX else (given[:TOTAL_MAX].rsplit(" ", 1)[0] or given[:TOTAL_MAX])


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


def check_shape(title: str, specs) -> None:
    if not title:
        raise ChartError("give the chart a title")
    if not isinstance(specs, list) or not specs or not all(isinstance(sp, dict) for sp in specs):
        raise ChartError("series must be a list of objects like {\"name\": ..., \"x\": [...], \"y\": [...]} "
                         "(or {\"name\", \"ticker\"}); send valid JSON and don't repeat the call as it was")


async def build(ctx: Ctx, args: dict) -> tuple[Chart, list[str]]:
    """The chart a make_chart call asks for, and a line per series for the model."""
    title = str(args.get("title") or "").strip()
    specs = args.get("series")
    check_shape(title, specs)
    if len(specs) > MAX_SERIES:
        raise ChartError(f"at most {MAX_SERIES} series per chart")
    if any(isinstance(sp, dict) and sp.get("kind") == "pie" for sp in specs) and (
            len(specs) != 1 or specs[0].get("ticker")):
        raise ChartError("a pie chart is one series of x (slice names) and y (values), not a ticker: "
                         "make one chart for each breakdown")
    moment = now(ctx.st.tz)
    today = moment.date()
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
    pie = any(s.kind == "pie" for s in series)
    for (i, _), got in zip(fetches, await asyncio.gather(*(f for _, f in fetches)), strict=True):
        series[i].x, series[i].y, notes[i] = got[0], as_y(got[1]), got[2]
    if pie:
        series[0].x = [str(v) for v in series[0].x]  # names, even when they look like years
        if not render.pie_slices(series[0]):
            raise ChartError("a pie chart needs at least one value above zero")
        overlap = subtotal_in(render.pie_slices(series[0]))
        if overlap:
            parent, parts = overlap
            raise ChartError(f"{parent} is a subtotal of {' + '.join(parts)} (it equals their sum), so a pie with all of "
                             f"them counts it twice. Draw the sub-lines without {parent}, or {parent} without them.")
    labels = [] if pie else unify_x(series)
    for s, spec in zip(series, specs, strict=True):
        if spec.get("percent_change"):
            s.y = percent_change(s.y)
        s.total = total(s, spec)
    if pie and not series[0].total:
        series[0].total = pie_total(series[0], title)
    chart = Chart(title, series, str(args.get("left_label") or ""), str(args.get("right_label") or ""),
                  bool(args.get("invert_left")), bool(args.get("invert_right")), labels,
                  dark=is_night(moment.hour))
    for side in ("left", "right"):  # an axis of nothing but % changes labels its ticks with %
        on_side = [spec for s, spec in zip(series, specs, strict=True) if s.axis == side]
        setattr(chart, f"{side}_percent", bool(on_side) and all(spec.get("percent_change") for spec in on_side))
    chart.subtitle = "Share of total" if pie else subtitle_for(
        specs, series, chart.left_percent and not any(s.axis == "right" for s in series))
    return chart, [pie_summary(s) if pie else summary(s, specs[i].get("percent_change"), notes[i])
                   for i, s in enumerate(series)]


def _fmt(v: float) -> str:
    return f"{v:,.4g}" if abs(v) < 1000 else f"{v:,.0f}"


def _x(v) -> str:
    return v.strftime("%Y-%m-%d %H:%M").removesuffix(" 00:00") if isinstance(v, datetime) else str(v)


SUBTOTAL_TOLERANCE = 0.001  # relative: how close a sum must be to count as equal
SUBTOTAL_MAX_SLICES = 12  # more than this isn't searched (2^11 subsets of the others)


def subtotal_in(slices: list[tuple[str, float]]) -> tuple[str, list[str]] | None:
    """A slice that is the sum of two or more of the others and whose name is part of theirs (Data Center next to Data
    Center - Hyperscale and Data Center - AI Clouds): a subtotal drawn beside its parts, which counts it twice. The
    name test keeps coincidences (a region of 20 beside two of 10) from being refused."""
    if not 3 <= len(slices) <= SUBTOTAL_MAX_SLICES:
        return None
    for name, value in slices:
        others = [(n, v) for n, v in slices if n != name]
        for size in range(2, len(others) + 1):
            for combo in itertools.combinations(others, size):
                if abs(sum(v for _, v in combo) - value) <= value * SUBTOTAL_TOLERANCE and any(
                        name.lower() in n.lower() for n, _ in combo):
                    return name, [n for n, _ in combo]
    return None


def pie_summary(s: Series) -> str:
    """Each slice's share, for the model to comment on."""
    slices = render.pie_slices(s)
    whole = sum(y for _, y in slices)
    return f"{s.name} (pie, {len(slices)} slices, total {_fmt(whole)}): " + ", ".join(
        f"{name} {render.share(y, whole)}" for name, y in slices)


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
    """The charts made while answering one message; the handler sends them in one reply, the text as caption. The
    model's calls run at the same time, so each takes its place when it starts: the order is the order asked for,
    not the order the drawings finish in."""

    def __init__(self):
        self.slots: list[dict] = []  # state: pending | done | failed
        self.prefer_pie = False  # the message asked for a breakdown: one series of names and values is a pie

    @property
    def photos(self) -> list[tuple[bytes, str]]:
        return [(s["image"], s["title"]) for s in self.slots if s["state"] == "done"]

    @property
    def calls(self) -> list[dict]:  # the make_chart arguments behind each photo, to redraw it from
        return [s["args"] for s in self.slots if s["state"] == "done"]

    def active(self) -> int:
        return sum(1 for s in self.slots if s["state"] != "failed")


def tool(ctx: Ctx, charts: Charts):
    """(definition, function) of make_chart, collecting the PNGs into `charts`."""

    async def make(args: dict) -> str:
        if charts.active() >= MAX_CHARTS:
            return f"Error: at most {MAX_CHARTS} charts per reply."
        args = pie_default(unquote_series(args), charts.prefer_pie)
        slot = {"state": "pending"}  # taken before the first await, so in the order the calls were made
        charts.slots.append(slot)
        began = time.monotonic()
        try:
            chart, lines = await build(ctx, args)
            image = await asyncio.to_thread(render.png, chart)
        except ChartError as e:
            slot["state"] = "failed"
            return f"Error: {e}"
        slot.update(image=image, title=chart.title, args=args, state="done")
        log.debug("Chart %r: %d series in %.1f s", chart.title, len(chart.series), time.monotonic() - began)
        position = next(i for i, other in enumerate(charts.slots) if other is slot)  # not index(): equal charts match
        number = sum(1 for other in charts.slots[:position + 1] if other["state"] != "failed")
        return (f"Chart {number} is ready. Your reply becomes its caption, so keep the reply under "
                f"{CAPTION_HINT} characters, and don't describe the chart or say you made one: say what it shows.\n"
                + "\n".join(lines))

    return TOOL, make
