"""Drawing a chart as a PNG with our own layout (`cartesian`, `pie`) painted by Pillow (`raster`): two y axes,
either one inverted, a legend or names at the line ends, or a donut."""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field, replace

from . import cartesian, pie, raster
from .cartesian import LEGEND_COLUMNS, RULE_WIDTH, legend_columns, spread  # noqa: F401  (part of this module's API)
from .scene import Scene
from .ticks import tick_label  # noqa: F401

KINDS = ("line", "bar", "scatter", "pie")
PIE_SLICES = 8  # more than this and the smallest are added up as "Other"
_lock = threading.Lock()  # Pillow's font objects are shared between the threads that draw


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


def _single_axis(chart: Chart) -> Chart:
    """A chart whose series are all on the right axis is drawn on the left one, with the right axis's settings."""
    if not chart.series or any(s.axis == "left" for s in chart.series):
        return chart
    return replace(chart, series=[replace(s, axis="left") for s in chart.series], left_label=chart.right_label,
                   invert_left=chart.invert_right, left_percent=chart.right_percent)


def scene(chart: Chart) -> Scene:
    """What the chart draws (tests look at this)."""
    chart = _single_axis(chart)
    th = THEMES["dark" if chart.dark else "light"]
    if any(s.kind == "pie" for s in chart.series):
        return pie.layout(chart, th, pie_slices(chart.series[0]), share)
    return cartesian.layout(chart, th)


def png(chart: Chart) -> bytes:
    with _lock:
        return raster.png(scene(chart))
