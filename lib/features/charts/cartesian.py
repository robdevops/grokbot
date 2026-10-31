"""The layout of a line, bar or scatter chart: up to two y axes, either upside down, and a legend or names at the
line ends."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from .raster import measure
from .scene import PT, Dot, H, Line, Rect, Scene, Text, W
from .ticks import EPOCH, date_ticks, days, nice_ticks, number_label, tick_label

LEGEND_COLUMNS = 6
RULE_WIDTH = 0.8  # points: every horizontal rule, the one at zero included
END_LABEL_GAP = 0.065  # least distance between two end-of-line labels, as a fraction of the plot's height
OUTER = 12  # pixels of edge around everything
TEXT = 10 * PT  # axis, legend and subtitle type
TITLE = 14 * PT
TICK_LEN, TICK_PAD = 5 * PT, 6 * PT
LEGEND_ROW = 19 * PT
LINE_WIDTH, MARKER, POINT = 1.6 * PT, 2 * PT, 2.35 * PT


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


def legend_columns(labels: list[str]) -> int:
    """As many legend columns as fit across the image: each takes the line sample, gaps and the widest label
    (about 0.65 em a character at the legend's 10 pt)."""
    em = TEXT
    entry = (3.6 + 0.65 * max(len(label) for label in labels)) * em
    return max(1, min(len(labels), LEGEND_COLUMNS, int((W - 2 * em) // entry)))


def axis_title(label: str, inverted: bool) -> str:
    return f"{label} (inverted)" if inverted and label else ("inverted" if inverted else label)


@dataclass
class Axis:
    lo: float
    hi: float
    ticks: list[float]
    inverted: bool
    pct: bool

    def at(self, v: float, top: float, bottom: float) -> float:
        f = (v - self.lo) / (self.hi - self.lo)
        return top + f * (bottom - top) if self.inverted else bottom - f * (bottom - top)


def _y_axis(chart, side: str) -> Axis | None:
    series = [s for s in chart.series if s.axis == side]
    if not series:
        return None
    values = [v for s in series for v in s.y if not math.isnan(v)]
    bars = any(s.kind == "bar" for s in series)
    lo, hi = (min(values + [0.0] * bars), max(values + [0.0] * bars)) if values else (0.0, 1.0)
    if lo == hi:
        lo, hi = lo - (abs(lo) * 0.05 or 1), hi + (abs(hi) * 0.05 or 1)
    margin = (hi - lo) * 0.05
    lo2, hi2 = lo - margin, hi + margin
    if bars and lo >= 0:  # bars stand on zero: no gap under them
        lo2 = 0.0
    if bars and hi <= 0:
        hi2 = 0.0
    return Axis(lo2, hi2, nice_ticks(lo2, hi2), getattr(chart, f"invert_{side}"), getattr(chart, f"{side}_percent"))


def _positions(chart, s) -> list[float]:
    if chart.labels:
        return [float(chart.labels.index(v)) for v in s.x]
    if s.x and isinstance(s.x[0], datetime):
        return [days(v) for v in s.x]
    return [float(v) for v in s.x]


def _bar_width(xs: list[list[float]]) -> float:
    gaps = [b - a for pos in xs for a, b in zip(sorted(pos), sorted(pos)[1:], strict=False) if b > a]
    return 0.8 * (min(gaps) if gaps else 1.0)


def _x_ticks(chart, lo: float, hi: float) -> list[tuple[float, str]]:
    if chart.labels:
        return [(float(i), label) for i, label in enumerate(chart.labels)]
    if chart.series and chart.series[0].x and isinstance(chart.series[0].x[0], datetime):
        start, end = EPOCH + timedelta(days=lo), EPOCH + timedelta(days=hi)
        return [(days(t), label) for t, label in date_ticks(start, end)]
    integer = all(float(v).is_integer() for s in chart.series for v in s.x)
    return [(t, number_label(t)) for t in nice_ticks(lo, hi, integer=integer)]


@dataclass
class Frame:
    """Where the plot sits, and the axes' texts measured to put it there."""
    x0: float
    x1: float
    y0: float
    y1: float
    titles: dict[str, str]
    labels: dict[str, list[str]]
    text_h: float
    subtitle_y: float  # top of the subtitle, under the title
    legend_top: float  # top of the legend's first row, under the subtitle


def _frame(chart, axes: dict[str, Axis], xticks, tilt: bool, rows: int, ends: list[str]) -> Frame:
    """Margins from what sits around the plot: tick labels, axis titles, end labels, title, subtitle and legend."""
    both = len(axes) == 2
    titles = {side: axis_title(getattr(chart, f"{side}_label"), a.inverted) if both else ("inverted" if a.inverted else "")
              for side, a in axes.items()}
    labels = {side: [tick_label(t, a.ticks, a.pct) for t in a.ticks] for side, a in axes.items()}
    text_h = measure("Hg", TEXT)[1]

    def side_room(side: str) -> float:
        widest = max((measure(t, TEXT)[0] for t in labels.get(side, [])), default=0.0)
        return OUTER + (text_h + TICK_PAD if titles.get(side) else 0) + widest + TICK_PAD

    xlabel_w = [measure(label, TEXT)[0] for _, label in xticks] or [0.0]
    left = max(side_room("left"), OUTER + xlabel_w[0] / 2)
    if "right" in axes:
        right = side_room("right")
    elif ends:
        right = OUTER + max(measure(e, TEXT)[0] for e in ends) + 0.02 * W + 6
    else:
        right = OUTER + (0 if tilt else xlabel_w[-1] / 2)
    subtitle_y = OUTER + measure("Hg", TITLE, bold=True)[1] + 0.5 * TEXT
    legend_top = subtitle_y + (text_h + 0.5 * TEXT if chart.subtitle else 0)
    top = legend_top + rows * LEGEND_ROW + 0.6 * TEXT
    xlabel_h = (max(xlabel_w) + text_h) * math.sin(math.radians(45)) if tilt else text_h
    bottom = OUTER + TICK_LEN + TICK_PAD + xlabel_h
    return Frame(left, W - right, top, H - bottom, titles, labels, text_h, subtitle_y, legend_top)


def _rules(scene: Scene, chart, axes: dict[str, Axis], f: Frame, th) -> None:
    """Horizontal rules only, from the first axis, plus one at zero where the data crosses it or bars stand on it."""
    first = axes.get("left") or axes.get("right")
    for t in first.ticks:
        y = first.at(t, f.y0, f.y1)
        scene.add(Line(role="rule", points=[(f.x0, y), (f.x1, y)], color=th.rule, width=RULE_WIDTH * PT))
    for side, a in axes.items():
        values = [v for s in chart.series if s.axis == side for v in s.y if not math.isnan(v)]
        if any(s.kind == "bar" for s in chart.series if s.axis == side) or (values and min(values) < 0 < max(values)):
            y = a.at(0, f.y0, f.y1)
            scene.add(Line(role="zero", points=[(f.x0, y), (f.x1, y)], color=th.rule, width=RULE_WIDTH * PT))


def _series(scene: Scene, chart, xs, X, axes: dict[str, Axis], f: Frame, group: float, labelled: bool, th) -> None:
    bars = [i for i, s in enumerate(chart.series) if s.kind == "bar"]
    width = group / max(len(bars), 1)
    for i, (s, pos) in enumerate(zip(chart.series, xs, strict=True)):
        a, color = axes[s.axis], th.palette[i % len(th.palette)]
        points = [(p, v) for p, v in zip(pos, s.y, strict=True) if not math.isnan(v)]
        if s.kind == "bar":
            shift = (bars.index(i) - (len(bars) - 1) / 2) * width
            for p, v in points:
                if v:  # a bar of zero has no height to draw
                    scene.add(Rect(role="bar", color=color, box=(X(p + shift - width / 2), a.at(0, f.y0, f.y1),
                                                                 X(p + shift + width / 2), a.at(v, f.y0, f.y1))))
        elif s.kind == "scatter":
            for p, v in points:
                scene.add(Dot(role="point", x=X(p), y=a.at(v, f.y0, f.y1), r=POINT, color=color))
        else:
            run: list[tuple[float, float]] = []
            for p, v in [*zip(pos, s.y, strict=True), (0.0, math.nan)]:  # a gap (NaN) ends a run
                if math.isnan(v):
                    if run:
                        scene.add(Line(role="line", points=run, color=color, width=LINE_WIDTH))
                    run = []
                else:
                    run.append((X(p), a.at(v, f.y0, f.y1)))
            if len(pos) <= 12 and not labelled:
                for p, v in points:
                    scene.add(Dot(role="marker", x=X(p), y=a.at(v, f.y0, f.y1), r=MARKER, color=color))


def _axis_marks(scene: Scene, xticks, X, axes: dict[str, Axis], f: Frame, tilt: bool, th) -> None:
    """Short strokes under the plot with the x labels under them; y labels beside it; the axis titles."""
    for p, label in xticks:
        x = X(p)
        if f.x0 - 1 <= x <= f.x1 + 1:
            scene.add(Line(role="x-tick", points=[(x, f.y1), (x, f.y1 + TICK_LEN)], color=th.tick, width=1 * PT))
            scene.add(Text(role="x-label", x=x, y=f.y1 + TICK_LEN + TICK_PAD, text=label, size=TEXT, color=th.text,
                           anchor="ra" if tilt else "ma", angle=45 if tilt else 0))
            scene.x_labels.append(label)
    for side, a in axes.items():
        for t, label in zip(a.ticks, f.labels[side], strict=True):
            x, anchor = (f.x0 - TICK_PAD, "rm") if side == "left" else (f.x1 + TICK_PAD, "lm")
            scene.add(Text(role="y-label", x=x, y=a.at(t, f.y0, f.y1), text=label, size=TEXT, color=th.text,
                           anchor=anchor))
        scene.axis_titles[side], scene.inverted[side] = f.titles[side], a.inverted
        if f.titles[side]:
            x = OUTER + f.text_h / 2 if side == "left" else W - OUTER - f.text_h / 2
            scene.add(Text(role="axis-title", x=x, y=(f.y0 + f.y1) / 2, text=f.titles[side], size=TEXT,
                           color=th.text, anchor="mm", angle=90))


def layout(chart, th) -> Scene:
    scene = Scene(bg=th.bg)
    axes = {side: a for side in ("left", "right") if (a := _y_axis(chart, side))}
    both = len(axes) == 2
    labelled = bool(chart.series) and not both and all(s.kind == "line" for s in chart.series)
    xs = [_positions(chart, s) for s in chart.series]
    group = _bar_width([x for x, s in zip(xs, chart.series, strict=True) if s.kind == "bar"]) \
        if any(s.kind == "bar" for s in chart.series) else 0.0
    every = [x for pos in xs for x in pos] or [0.0, 1.0]
    xlo, xhi = min(every) - group / 2, max(every) + group / 2
    if xlo == xhi:
        xlo, xhi = xlo - 1, xhi + 1
    xlo, xhi = xlo - (xhi - xlo) * 0.01, xhi + (xhi - xlo) * 0.01
    xticks = _x_ticks(chart, xlo, xhi)
    tilt = bool(chart.labels) and len(chart.labels) > 8
    entries = [] if labelled else [
        (" ".join(filter(None, (s.name, s.total))) + (" (right)" if both and s.axis == "right" else ""), i)
        for side in ("left", "right") for i, s in enumerate(chart.series) if s.axis == side]
    columns = legend_columns([e for e, _ in entries]) if entries else 1
    rows = math.ceil(len(entries) / columns) if len(entries) > 1 or any(s.total for s in chart.series) else 0
    ends = [" ".join(filter(None, (s.name, s.total))) for s in chart.series] if labelled else []
    f = _frame(chart, axes, xticks, tilt, rows, ends)

    def X(v: float) -> float:
        return f.x0 + (v - xlo) / (xhi - xlo) * (f.x1 - f.x0)

    _rules(scene, chart, axes, f, th)
    _series(scene, chart, xs, X, axes, f, group, labelled, th)
    _axis_marks(scene, xticks, X, axes, f, tilt, th)
    if labelled:
        _end_labels(scene, chart, xs, X, axes, (f.x0, f.x1, f.y0, f.y1), th)
    else:
        _legend(scene, chart, entries, columns, (f.x0, f.legend_top), th)
    scene.add(Text(role="title", x=f.x0, y=OUTER, text=chart.title, size=TITLE, color=th.ink, bold=True))
    if chart.subtitle:
        scene.add(Text(role="subtitle", x=f.x0, y=f.subtitle_y, text=chart.subtitle, size=TEXT, color=th.text))
    return scene


def _end_labels(scene: Scene, chart, xs, X, axes, box, th) -> None:
    """A square at the end of each line and a small "name  total" label beside it, at the right edge of the plot."""
    x0, x1, y0, y1 = box
    ends = []
    for i, (s, pos) in enumerate(zip(chart.series, xs, strict=True)):
        points = [(p, v) for p, v in zip(pos, s.y, strict=True) if not math.isnan(v)]
        if points:
            p, v = points[-1]
            ends.append((i, X(p), axes[s.axis].at(v, y0, y1)))
    wanted = [(y1 - y) / (y1 - y0) for _, _, y in ends]  # measured up the picture, whichever way the axis runs
    for (i, x, y), frac in zip(ends, spread(wanted), strict=True):
        s, color = chart.series[i], th.palette[i % len(th.palette)]
        text = "  ".join(filter(None, (s.name, s.total)))
        tx, ty = x1 + 0.02 * (x1 - x0), y1 - frac * (y1 - y0)
        scene.add(Line(role="leader", points=[(x + 3 * PT, y), (tx - 2, ty)], color=color, width=0.9 * PT))
        scene.add(Dot(role="end", x=x, y=y, r=2.5 * PT, color=color, square=True))
        scene.add(Text(role="end-label", x=tx, y=ty, text=text, size=TEXT, color=th.ink, anchor="lm"))
        scene.end_labels.append(text)


def _legend(scene: Scene, chart, entries, columns: int, at: tuple[float, float], th) -> None:
    """Above the plot, under the title, without a frame; read left to right, row by row."""
    if not entries or (len(entries) == 1 and not any(s.total for s in chart.series)):
        return
    x0, top = at
    sample, gap = 1.4 * TEXT, 0.8 * TEXT
    widths = [max(measure(label, TEXT)[0] for label, _ in entries[c::columns]) + sample + gap + 1.4 * TEXT
              for c in range(columns)]
    starts = [x0 + sum(widths[:c]) for c in range(columns)]
    for n, (label, i) in enumerate(entries):
        s, color = chart.series[i], th.palette[i % len(th.palette)]
        x, y = starts[n % columns], top + (n // columns + 0.5) * LEGEND_ROW
        if s.kind == "bar":
            scene.add(Rect(role="swatch", box=(x, y - 0.35 * TEXT, x + sample, y + 0.35 * TEXT), color=color))
        elif s.kind == "scatter":
            scene.add(Dot(role="swatch", x=x + sample / 2, y=y, r=POINT, color=color))
        else:
            scene.add(Line(role="swatch", points=[(x, y), (x + sample, y)], color=color, width=LINE_WIDTH))
        scene.add(Text(role="legend", x=x + sample + gap, y=y, text=label, size=TEXT, color=th.ink, anchor="lm"))
        scene.legend.append(label)
