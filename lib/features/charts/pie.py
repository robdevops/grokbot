"""The layout of a pie chart: a donut, its total in the middle, and a legend of each slice's share beside it."""

from __future__ import annotations

import textwrap

from .raster import measure, point_on
from .scene import PT, Dot, H, Line, Rect, Scene, Text, W, Wedge

CENTRE = (0.27 * W, 0.58 * H)
RADIUS = 0.30 * H
HOLE = 0.58  # the hole's radius as a share of the donut's
GAP = 2.5 * PT  # the background-coloured line between two slices
WRAP = 26  # characters of a slice's name per legend line


def layout(chart, th, slices: list[tuple[str, float]], share) -> Scene:
    scene = Scene(bg=th.bg)
    whole = sum(y for _, y in slices) or 1.0
    colors = [th.text if name == "Other" else th.palette[i % len(th.palette)] for i, (name, _) in enumerate(slices)]
    cx, cy = CENTRE
    angle, bounds = 0.0, []
    for (_, y), color in zip(slices, colors, strict=True):
        sweep = 360 * y / whole
        scene.add(Wedge(role="wedge", cx=cx, cy=cy, r=RADIUS, start=angle, end=angle + sweep, color=color))
        bounds.append(angle)
        angle += sweep
    if len(slices) > 1:
        for a in bounds:
            scene.add(Line(role="gap", points=[(cx, cy), point_on(cx, cy, RADIUS + 2, a)], color=th.bg, width=GAP))
    scene.add(Dot(role="hole", x=cx, y=cy, r=RADIUS * HOLE, color=th.bg))
    total = chart.series[0].total
    if total:
        scene.add(Text(role="total", x=cx, y=cy, text=total, size=17 * PT, color=th.ink, bold=True, anchor="mm"))
    _legend(scene, th, slices, colors, whole, share)
    scene.add(Text(role="title", x=0.03 * W, y=0.05 * H, text=chart.title, size=14 * PT, color=th.ink, bold=True))
    if chart.subtitle:
        scene.add(Text(role="subtitle", x=0.03 * W, y=0.125 * H, text=chart.subtitle, size=10 * PT, color=th.text))
    return scene


def _legend(scene: Scene, th, slices, colors, whole: float, share) -> None:
    """A square and "name  share" per slice, the names wrapped, the whole block centred beside the donut."""
    px = 11 * PT
    line_h = measure("Hg", px)[1] * 1.25
    gap = 0.9 * px
    entries = [(textwrap.fill(name, WRAP) + f"  {share(y, whole)}").split("\n") for name, y in slices]
    height = sum(len(lines) * line_h for lines in entries) + gap * (len(entries) - 1)
    x, y = 0.56 * W, 0.58 * H - height / 2
    side = 0.75 * px
    for lines, color in zip(entries, colors, strict=True):
        block = len(lines) * line_h
        middle = y + block / 2
        scene.add(Rect(role="swatch", box=(x, middle - side / 2, x + side, middle + side / 2), color=color))
        for i, text in enumerate(lines):
            scene.add(Text(role="legend", x=x + side + px * 0.8, y=y + (i + 0.5) * line_h, text=text, size=px,
                           color=th.ink, anchor="lm"))
        scene.legend.append("\n".join(lines))
        y += block + gap
