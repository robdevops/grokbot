"""What a chart draws, as plain data in pixels: the layout writes a Scene, the rasteriser paints it, tests read it."""

from __future__ import annotations

from dataclasses import dataclass, field

W, H = 1224, 714  # the picture, in pixels
PT = 170 / 72  # pixels per point (the old matplotlib figure was 7.2 x 4.2 in at 170 dpi; sizes are kept in points)


@dataclass(kw_only=True)
class Text:
    role: str
    x: float
    y: float
    text: str
    size: float  # pixels
    color: str
    bold: bool = False
    anchor: str = "la"  # Pillow's anchors: l/m/r across, a/m/s/d (ascender, middle, baseline, descender) down
    angle: float = 0  # degrees, counter-clockwise


@dataclass(kw_only=True)
class Line:
    role: str
    points: list[tuple[float, float]]
    color: str
    width: float


@dataclass(kw_only=True)
class Rect:
    role: str
    box: tuple[float, float, float, float]  # x0, y0, x1, y1
    color: str


@dataclass(kw_only=True)
class Dot:
    role: str
    x: float
    y: float
    r: float
    color: str
    square: bool = False


@dataclass(kw_only=True)
class Wedge:
    role: str
    cx: float
    cy: float
    r: float
    start: float  # degrees, clockwise from 12 o'clock
    end: float
    color: str


@dataclass
class Scene:
    bg: str
    ops: list = field(default_factory=list)
    legend: list[str] = field(default_factory=list)
    end_labels: list[str] = field(default_factory=list)
    x_labels: list[str] = field(default_factory=list)
    axis_titles: dict[str, str] = field(default_factory=dict)  # side -> title ("" when none)
    inverted: dict[str, bool] = field(default_factory=dict)  # side -> drawn upside down

    def add(self, op) -> None:
        self.ops.append(op)

    def find(self, role: str) -> list:
        return [op for op in self.ops if op.role == role]

    def texts(self, role: str) -> list[str]:
        return [op.text for op in self.find(role)]
