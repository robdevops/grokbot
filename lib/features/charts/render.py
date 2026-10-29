"""Drawing a chart as a PNG with matplotlib (imported on the first chart): two y axes, either one inverted."""

from __future__ import annotations

import io
import math
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ... import config

KINDS = ("line", "bar", "scatter")
SIZE_IN, DPI = (7.2, 4.2), 170  # 1224 x 714 px, with type big enough to read on a phone
PALETTE = ("#2563eb", "#f97316", "#16a34a", "#dc2626", "#9333ea", "#0891b2", "#ca8a04", "#db2777", "#4b5563",
           "#65a30d")
INK, MUTED, RULE, ZERO = "#111827", "#6b7280", "#e5e7eb", "#9ca3af"
LEGEND_COLUMNS = 6
_lock = threading.Lock()  # matplotlib's global state (rcParams, font cache) isn't thread-safe


@dataclass
class Series:
    name: str
    x: list  # all datetimes, all numbers, or all labels (across the whole chart)
    y: list[float]  # NaN = a gap
    axis: str = "left"
    kind: str = "line"


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


def legend_order(handles: list, columns: int) -> list:
    """Matplotlib fills legend columns top to bottom; this order makes it read left to right, row by row."""
    rows = math.ceil(len(handles) / columns)
    return [handles[r * columns + c] for c in range(columns) for r in range(rows) if r * columns + c < len(handles)]


def _plain(ax) -> None:
    """No frame and no tick marks: the horizontal rules carry the scale."""
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.tick_params(length=0, labelsize=10, labelcolor=MUTED, pad=6)


def figure(chart: Chart):
    """The matplotlib Figure for a chart (tests look at its axes)."""
    dates, canvas, Figure, Formatter, MaxNLocator = _matplotlib()
    fig = Figure(figsize=SIZE_IN, dpi=DPI, layout="constrained", facecolor="white")
    canvas(fig)
    left = fig.subplots()
    right = left.twinx() if any(s.axis == "right" for s in chart.series) else None
    xs = [_positions(chart, s, dates) for s in chart.series]
    bars = [i for i, s in enumerate(chart.series) if s.kind == "bar"]
    width = _bar_width([xs[i] for i in bars]) / max(len(bars), 1)
    both = right is not None and any(s.axis == "left" for s in chart.series)
    for i, (s, pos) in enumerate(zip(chart.series, xs, strict=True)):
        ax = right if s.axis == "right" else left
        label = s.name + (" (right)" if both and s.axis == "right" else "")
        color = PALETTE[i % len(PALETTE)]
        if s.kind == "bar":
            shift = (bars.index(i) - (len(bars) - 1) / 2) * width
            ax.bar([p + shift for p in pos], s.y, width=width, label=label, color=color, linewidth=0, zorder=2)
        elif s.kind == "scatter":
            ax.scatter(pos, s.y, label=label, color=color, s=22, linewidths=0, zorder=3)
        else:
            ax.plot(pos, s.y, label=label, color=color, linewidth=2.2, solid_capstyle="round",
                    solid_joinstyle="round", marker="o" if len(pos) <= 12 else None, markersize=4, zorder=3)
    for ax, side in ((left, "left"), (right, "right")):
        if ax is None:
            continue
        _plain(ax)
        values = [v for s in chart.series if s.axis == side for v in s.y if not math.isnan(v)]
        if any(s.kind == "bar" for s in chart.series if s.axis == side) or (values and min(values) < 0 < max(values)):
            ax.axhline(0, color=ZERO, linewidth=1, zorder=1)
        ax.yaxis.set_major_formatter(_formatter(Formatter, getattr(chart, f"{side}_percent")))
        inverted = getattr(chart, f"invert_{side}")
        ax.set_ylabel(_axis_label(getattr(chart, f"{side}_label"), inverted), color=MUTED, fontsize=10)
        if inverted:
            ax.invert_yaxis()
    left.yaxis.grid(True, color=RULE, linewidth=0.8)  # horizontal rules only, from the left axis
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
        left.xaxis.get_offset_text().set(color=MUTED, fontsize=9)
    elif all(float(v).is_integer() for s in chart.series for v in s.x):
        left.xaxis.set_major_locator(MaxNLocator(integer=True))
    handles = [h for ax in (left, right) if ax for h in ax.get_legend_handles_labels()[0]]
    columns = min(len(handles), LEGEND_COLUMNS)
    rows = math.ceil(len(handles) / columns) if len(handles) > 1 else 0
    if rows:  # above the plot, under the title, without a frame
        left.legend(handles=legend_order(handles, columns), loc="lower left", bbox_to_anchor=(0, 1.01), ncol=columns,
                    frameon=False, fontsize=10, handlelength=1.4, columnspacing=1.4, borderaxespad=0, labelcolor=INK)
    left.set_title(chart.title, loc="left", fontsize=14, fontweight="bold", color=INK, pad=10 + rows * 19)
    return fig


def png(chart: Chart) -> bytes:
    with _lock:
        fig = figure(chart)
        buf = io.BytesIO()
        fig.savefig(buf, format="png")
        fig.clear()
    return buf.getvalue()
