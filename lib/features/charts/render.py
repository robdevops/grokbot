"""Drawing a chart as a PNG with matplotlib (imported on the first chart): two y axes, either one inverted."""

from __future__ import annotations

import io
import math
import os
import textwrap
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

from ... import config

KINDS = ("line", "bar", "scatter", "pie")
PIE_SLICES = 8  # more than this and the smallest are added up as "Other"
SIZE_IN, DPI = (7.2, 4.2), 170  # 1224 x 714 px, with type big enough to read on a phone
LEGEND_COLUMNS = 6
RULE_WIDTH = 0.8  # every horizontal rule, the one at zero included
SUBTITLE_PT = 17  # height the subtitle adds above the plot
END_LABEL_GAP = 0.065  # least distance between two end-of-line labels, as a fraction of the plot's height
_lock = threading.Lock()  # matplotlib's global state (rcParams, font cache) isn't thread-safe


@dataclass(frozen=True)
class Theme:
    bg: str
    ink: str  # title and labels
    text: str  # axis text: lighter than the ink
    rule: str  # the horizontal rules
    tick: str  # the strokes above the x labels
    palette: tuple[str, ...]


THEMES = {
    "light": Theme("#ffffff", "#111827", "#8b93a1", "#e6e9ee", "#c3c8d0",
                   ("#2563eb", "#f97316", "#16a34a", "#dc2626", "#9333ea", "#0891b2", "#ca8a04", "#db2777",
                    "#4b5563", "#65a30d")),
    "dark": Theme("#141a22", "#f3f4f6", "#8e97a6", "#2a323e", "#4b5563",
                  ("#3b86e0", "#e0622f", "#3fb950", "#e3b341", "#a371f7", "#39c5cf", "#f778ba", "#9ca3af",
                   "#7ee787", "#ff7b72")),
}


@dataclass
class Series:
    name: str
    x: list  # all datetimes, all numbers, or all labels (across the whole chart)
    y: list[float]  # NaN = a gap
    axis: str = "left"
    kind: str = "line"
    total: str = ""  # shown after the name in the legend, e.g. "+20.4%"


@dataclass
class Chart:
    title: str
    series: list[Series]
    left_label: str = ""
    right_label: str = ""
    invert_left: bool = False
    invert_right: bool = False
    labels: list[str] = field(default_factory=list)  # category order when x holds labels
    left_percent: bool = False  # tick labels end in %
    right_percent: bool = False
    dark: bool = False  # the dark theme (the bot draws it at night)
    subtitle: str = ""  # under the title, in quiet type


def _matplotlib():
    # Keep the font cache with the bot's data (a service's home is often read-only), unless set already.
    data_dir = Path(config.database_path(os.environ)).parent
    if data_dir != Path("."):
        os.environ.setdefault("MPLCONFIGDIR", str(data_dir / "matplotlib"))
    from matplotlib import dates
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from matplotlib.ticker import Formatter, MaxNLocator
    return dates, FigureCanvasAgg, Figure, Formatter, MaxNLocator


def _axis_label(label: str, inverted: bool) -> str:
    return f"{label} (inverted)" if inverted and label else ("inverted" if inverted else label)


def tick_label(v: float, ticks, pct: bool) -> str:
    """3,400 / 0.60 / 150M / 1.5B (or with %), with the decimals the step between `ticks` needs, so every label
    on an axis has the same number of them."""
    ticks = sorted(float(t) for t in ticks)
    biggest = max((abs(t) for t in ticks), default=abs(v))
    div, suffix = next(((d, x) for d, x in ((1e12, "T"), (1e9, "B"), (1e6, "M")) if biggest >= d), (1.0, ""))
    steps = [b - a for a, b in zip(ticks, ticks[1:], strict=False) if b > a]
    step = (min(steps) if steps else 1.0) / div
    decimals = next((d for d in range(7) if abs(round(step, d) - step) <= step * 1e-6), 6)
    if abs(v) < (min(steps) if steps else 1.0) * 1e-6:
        v = 0.0  # not "-0"
    text = f"{v / div:,.{decimals}f}"
    return text + suffix + ("%" if pct else "")


def _positions(chart: Chart, s: Series, dates) -> list[float]:
    if chart.labels:
        return [float(chart.labels.index(v)) for v in s.x]
    if s.x and isinstance(s.x[0], datetime):
        return [float(v) for v in dates.date2num(s.x)]
    return [float(v) for v in s.x]


def _bar_width(xs: list[list[float]]) -> float:
    gaps = [b - a for pos in xs for a, b in zip(sorted(pos), sorted(pos)[1:], strict=False) if b > a]
    return 0.8 * (min(gaps) if gaps else 1.0)


def _formatter(Formatter, pct: bool):
    """Tick labels that see all of the axis's ticks (set before every draw), to share one number of decimals."""

    class Ticks(Formatter):
        ticks: list = []

        def set_locs(self, locs):
            super().set_locs(locs)
            self.ticks = list(locs)

        def __call__(self, v, pos=None):
            return tick_label(v, self.ticks, pct)

    return Ticks()


def legend_columns(labels: list[str]) -> int:
    """As many legend columns as fit across the image: each takes the line sample, gaps and the widest label
    (about 0.65 em a character at the legend's 10 pt)."""
    em = 10 / 72 * DPI
    entry = (3.6 + 0.65 * max(len(label) for label in labels)) * em
    return max(1, min(len(labels), LEGEND_COLUMNS, int((SIZE_IN[0] * DPI - 2 * em) // entry)))


def legend_order(handles: list, columns: int) -> list:
    """Matplotlib fills legend columns top to bottom; this order makes it read left to right, row by row."""
    rows = math.ceil(len(handles) / columns)
    return [handles[r * columns + c] for c in range(columns) for r in range(rows) if r * columns + c < len(handles)]


def _plain(ax, th: Theme) -> None:
    """No frame; the horizontal rules carry the scale, so no y ticks. The x axis gets short strokes above its labels."""
    ax.set_facecolor(th.bg)
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.tick_params(axis="y", length=0, labelsize=10, labelcolor=th.text, pad=6)
    ax.tick_params(axis="x", length=5, width=1, color=th.tick, labelsize=10, labelcolor=th.text, pad=6)


def spread(fracs: list[float], gap: float = END_LABEL_GAP) -> list[float]:
    """Positions for labels wanted at `fracs` (0 = bottom, 1 = top of the plot), pushed apart to at least `gap`
    while keeping their order and staying as near their wish as that allows."""
    order = sorted(range(len(fracs)), key=lambda i: fracs[i])
    pos = [fracs[i] for i in order]
    for i in range(1, len(pos)):
        pos[i] = max(pos[i], pos[i - 1] + gap)
    if pos and pos[-1] > 1:
        pos[-1] = 1.0
        for i in range(len(pos) - 2, -1, -1):
            pos[i] = min(pos[i], pos[i + 1] - gap)
    out = [0.0] * len(fracs)
    for rank, i in enumerate(order):
        out[i] = pos[rank]
    return out


def pie_slices(s: Series) -> list[tuple[str, float]]:
    """The slices of a pie, biggest first: gaps and values of zero or below dropped, and past PIE_SLICES the
    smallest are added up as "Other"."""
    slices = sorted(((str(x), y) for x, y in zip(s.x, s.y, strict=True) if not math.isnan(y) and y > 0),
                    key=lambda item: -item[1])
    if len(slices) > PIE_SLICES:
        slices = slices[:PIE_SLICES - 1] + [("Other", sum(y for _, y in slices[PIE_SLICES - 1:]))]
    return slices


def share(part: float, whole: float) -> str:
    pct = part / whole * 100
    return "<0.1%" if 0 < pct < 0.05 else f"{pct:.1f}%"


def pie_figure(chart: Chart, th: Theme, canvas, Figure):
    """A donut for the one series of a pie chart, a legend with each slice's share beside it, the total in the
    middle if the series has one."""
    fig = Figure(figsize=SIZE_IN, dpi=DPI, facecolor=th.bg)
    canvas(fig)
    slices = pie_slices(chart.series[0])
    whole = sum(y for _, y in slices) or 1.0
    colors = [th.text if name == "Other" else th.palette[i % len(th.palette)] for i, (name, _) in enumerate(slices)]
    ax = fig.add_axes((0.03, 0.04, 0.48, 0.76))
    wedges, _ = ax.pie([y for _, y in slices], colors=colors, startangle=90, counterclock=False,
                       wedgeprops={"width": 0.42, "edgecolor": th.bg, "linewidth": 2.5})
    if chart.series[0].total:
        ax.text(0, 0, chart.series[0].total, ha="center", va="center", fontsize=17, fontweight="bold", color=th.ink)
    labels = [textwrap.fill(name, 26) + f"  {share(y, whole)}" for name, y in slices]
    fig.legend(wedges, labels, loc="center left", bbox_to_anchor=(0.54, 0.42), frameon=False, fontsize=11,
               labelspacing=0.9, handlelength=1.0, handleheight=1.0, labelcolor=th.ink)
    fig.text(0.03, 0.95, chart.title, ha="left", va="top", fontsize=14, fontweight="bold", color=th.ink)
    if chart.subtitle:
        fig.text(0.03, 0.875, chart.subtitle, ha="left", va="top", fontsize=10, color=th.text)
    return fig


def _single_axis(chart: Chart) -> Chart:
    """A chart whose series are all on the right axis is drawn on the left one, with the right axis's settings."""
    if not chart.series or any(s.axis == "left" for s in chart.series):
        return chart
    return replace(chart, series=[replace(s, axis="left") for s in chart.series], left_label=chart.right_label,
                   invert_left=chart.invert_right, left_percent=chart.right_percent)


def end_labels(ax, chart: Chart, xs: list[list[float]], th: Theme) -> None:
    """A square at the end of each line and a small "name  total" label beside it, at the right edge of the plot."""
    ends = []
    for i, (s, pos) in enumerate(zip(chart.series, xs, strict=True)):
        points = [(x, y) for x, y in zip(pos, s.y, strict=True) if not math.isnan(y)]
        if points:
            ends.append((i, *points[-1]))
    if not ends:
        return
    lo, hi = ax.get_ylim()  # after any inversion: the fraction below is still measured up the picture
    wanted = [(y - lo) / (hi - lo) if hi != lo else 0.5 for _, _, y in ends]
    for (i, x, y), frac in zip(ends, spread(wanted), strict=True):
        s, color = chart.series[i], th.palette[i % len(th.palette)]
        ax.plot([x], [y], marker="s", markersize=5, color=color, linestyle="none", zorder=4, clip_on=False)
        ax.annotate("  ".join(filter(None, (s.name, s.total))), xy=(x, y), xycoords="data", xytext=(1.02, frac),
                    textcoords="axes fraction", ha="left", va="center", fontsize=10, color=th.ink,
                    annotation_clip=False,
                    arrowprops={"arrowstyle": "-", "color": color, "linewidth": 0.9, "shrinkA": 0, "shrinkB": 3})


def figure(chart: Chart):
    """The matplotlib Figure for a chart (tests look at its axes)."""
    chart = _single_axis(chart)
    th = THEMES["dark" if chart.dark else "light"]
    dates, canvas, Figure, Formatter, MaxNLocator = _matplotlib()
    if any(s.kind == "pie" for s in chart.series):
        return pie_figure(chart, th, canvas, Figure)
    fig = Figure(figsize=SIZE_IN, dpi=DPI, layout="constrained", facecolor=th.bg)
    canvas(fig)
    left = fig.subplots()
    right = left.twinx() if any(s.axis == "right" for s in chart.series) else None
    labelled = chart.series and right is None and all(s.kind == "line" for s in chart.series)  # names at the line ends
    xs = [_positions(chart, s, dates) for s in chart.series]
    bars = [i for i, s in enumerate(chart.series) if s.kind == "bar"]
    width = _bar_width([xs[i] for i in bars]) / max(len(bars), 1)
    both = right is not None
    for i, (s, pos) in enumerate(zip(chart.series, xs, strict=True)):
        ax = right if s.axis == "right" else left
        label = " ".join(filter(None, (s.name, s.total))) + (" (right)" if both and s.axis == "right" else "")
        color = th.palette[i % len(th.palette)]
        if s.kind == "bar":
            shift = (bars.index(i) - (len(bars) - 1) / 2) * width
            ax.bar([p + shift for p in pos], s.y, width=width, label=label, color=color, linewidth=0, zorder=2)
        elif s.kind == "scatter":
            ax.scatter(pos, s.y, label=label, color=color, s=22, linewidths=0, zorder=3)
        else:
            ax.plot(pos, s.y, label=label, color=color, linewidth=1.6, solid_capstyle="round",
                    solid_joinstyle="round", marker="o" if len(pos) <= 12 and not labelled else None, markersize=4,
                    zorder=3)
    for ax, side in ((left, "left"), (right, "right")):
        if ax is None:
            continue
        _plain(ax, th)
        values = [v for s in chart.series if s.axis == side for v in s.y if not math.isnan(v)]
        if any(s.kind == "bar" for s in chart.series if s.axis == side) or (values and min(values) < 0 < max(values)):
            ax.axhline(0, color=th.rule, linewidth=RULE_WIDTH, zorder=1)  # as light as the rest of the rules
        ax.yaxis.set_major_formatter(_formatter(Formatter, getattr(chart, f"{side}_percent")))
        inverted = getattr(chart, f"invert_{side}")
        # One y axis needs no title (the chart's title says what it shows), but an inverted one says so.
        label = _axis_label(getattr(chart, f"{side}_label"), inverted) if both else ("inverted" if inverted else "")
        if label:
            ax.set_ylabel(label, color=th.text, fontsize=10)
        if inverted:
            ax.invert_yaxis()
    left.yaxis.grid(True, color=th.rule, linewidth=RULE_WIDTH)  # horizontal rules only, from the left axis
    left.set_axisbelow(True)
    left.margins(x=0.01)
    if chart.labels:
        tilt = len(chart.labels) > 8
        left.set_xticks(range(len(chart.labels)), chart.labels, rotation=45 if tilt else 0,
                        ha="right" if tilt else "center")
    elif chart.series and chart.series[0].x and isinstance(chart.series[0].x[0], datetime):
        locator = dates.AutoDateLocator()
        left.xaxis.set_major_locator(locator)
        left.xaxis.set_major_formatter(dates.ConciseDateFormatter(
            locator, offset_formats=["", "%Y", "%b %Y", "%d %b %Y", "%d %b %Y", "%d %b %Y %H:%M"]))
        left.xaxis.get_offset_text().set_visible(False)  # the subtitle gives the dates
    elif all(float(v).is_integer() for s in chart.series for v in s.x):
        left.xaxis.set_major_locator(MaxNLocator(integer=True))
    rows = 0
    if labelled:
        end_labels(left, chart, xs, th)
    else:  # a legend above the plot, under the title, without a frame
        handles = [h for ax in (left, right) if ax for h in ax.get_legend_handles_labels()[0]]
        columns = legend_columns([h.get_label() for h in handles]) if handles else 1
        rows = math.ceil(len(handles) / columns) if len(handles) > 1 or any(s.total for s in chart.series) else 0
        if rows:
            left.legend(handles=legend_order(handles, columns), loc="lower left", bbox_to_anchor=(0, 1.01),
                        ncol=columns, frameon=False, fontsize=10, handlelength=1.4, columnspacing=1.4,
                        borderaxespad=0, labelcolor=th.ink)
    left.set_title(chart.title, loc="left", fontsize=14, fontweight="bold", color=th.ink,
                   pad=10 + rows * 19 + (SUBTITLE_PT if chart.subtitle else 0))
    if chart.subtitle:  # above the legend, under the title
        left.annotate(chart.subtitle, xy=(0, 1), xycoords="axes fraction", xytext=(0, 6 + rows * 19),
                      textcoords="offset points", va="bottom", ha="left", fontsize=10, color=th.text,
                      annotation_clip=False)
    return fig


def png(chart: Chart) -> bytes:
    with _lock:
        fig = figure(chart)
        buf = io.BytesIO()
        fig.savefig(buf, format="png")
        fig.clear()
    return buf.getvalue()
