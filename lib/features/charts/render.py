"""Drawing a chart as a PNG with matplotlib (imported on the first chart): two y axes, either one inverted."""

from __future__ import annotations

import io
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ... import config

KINDS = ("line", "bar", "scatter")
SIZE_IN, DPI = (10, 5.6), 120  # 1200 x 672 px: Telegram shows photos up to 1280 px
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


def _matplotlib():
    # Keep the font cache with the bot's data (a service's home is often read-only), unless set already.
    data_dir = Path(config.database_path(os.environ)).parent
    if data_dir != Path("."):
        os.environ.setdefault("MPLCONFIGDIR", str(data_dir / "matplotlib"))
    from matplotlib import dates
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from matplotlib.ticker import MaxNLocator
    return dates, FigureCanvasAgg, Figure, MaxNLocator


def _axis_label(label: str, inverted: bool) -> str:
    return f"{label} (inverted)" if inverted and label else ("inverted" if inverted else label)


def _positions(chart: Chart, s: Series, dates) -> list[float]:
    if chart.labels:
        return [float(chart.labels.index(v)) for v in s.x]
    if s.x and isinstance(s.x[0], datetime):
        return [float(v) for v in dates.date2num(s.x)]
    return [float(v) for v in s.x]


def _bar_width(xs: list[list[float]]) -> float:
    gaps = [b - a for pos in xs for a, b in zip(sorted(pos), sorted(pos)[1:], strict=False) if b > a]
    return 0.8 * (min(gaps) if gaps else 1.0)


def figure(chart: Chart):
    """The matplotlib Figure for a chart (tests look at its axes)."""
    dates, canvas, Figure, MaxNLocator = _matplotlib()
    fig = Figure(figsize=SIZE_IN, dpi=DPI, layout="constrained")
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
        color = f"C{i % 10}"
        if s.kind == "bar":
            shift = (bars.index(i) - (len(bars) - 1) / 2) * width
            ax.bar([p + shift for p in pos], s.y, width=width, label=label, color=color, alpha=0.8)
        elif s.kind == "scatter":
            ax.scatter(pos, s.y, label=label, color=color, s=18)
        else:
            ax.plot(pos, s.y, label=label, color=color, linewidth=1.8, marker="o" if len(pos) <= 12 else None)
    for ax, label, inverted, side in ((left, chart.left_label, chart.invert_left, "left"),
                                      (right, chart.right_label, chart.invert_right, "right")):
        if ax is None:
            continue
        if any(s.kind == "bar" for s in chart.series if s.axis == side):
            ax.axhline(0, color="0.6", linewidth=0.8, zorder=0)
        ax.set_ylabel(_axis_label(label, inverted))
        if inverted:
            ax.invert_yaxis()
    if chart.labels:
        left.set_xticks(range(len(chart.labels)), chart.labels,
                        rotation=45 if len(chart.labels) > 8 else 0, ha="right" if len(chart.labels) > 8 else "center")
    elif chart.series and chart.series[0].x and isinstance(chart.series[0].x[0], datetime):
        locator = dates.AutoDateLocator()
        left.xaxis.set_major_locator(locator)
        left.xaxis.set_major_formatter(dates.ConciseDateFormatter(locator))
    elif all(float(v).is_integer() for s in chart.series for v in s.x):
        left.xaxis.set_major_locator(MaxNLocator(integer=True))
    left.set_title(chart.title, fontsize=13)
    left.grid(True, alpha=0.3)
    handles = [h for ax in (left, right) if ax for h in ax.get_legend_handles_labels()[0]]
    if len(handles) > 1:
        left.legend(handles=handles, loc="best", fontsize=9, framealpha=0.85)
    return fig


def png(chart: Chart) -> bytes:
    with _lock:
        fig = figure(chart)
        buf = io.BytesIO()
        fig.savefig(buf, format="png")
        fig.clear()
    return buf.getvalue()
