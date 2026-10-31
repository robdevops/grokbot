"""Painting a Scene with Pillow: drawn at twice the size and scaled down, which smooths the lines (Pillow's own shapes
aren't)."""

from __future__ import annotations

import io
import math
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

from .scene import Dot, H, Line, Rect, Scene, Text, W, Wedge

SCALE = 2  # 3 looks no better and costs 1.7x the time and 20 MB more memory


@lru_cache(maxsize=64)
def font(px: int) -> ImageFont.FreeTypeFont:
    """Pillow's built-in font at a pixel size (needs FreeType, which Pillow's wheels have)."""
    return ImageFont.load_default(size=max(1, px))


def _stroke(px: float, bold: bool) -> int:
    """Pillow's font has no bold: an outline in the text's own colour stands in for it."""
    return max(1, round(px / 40)) if bold else 0


def measure(text: str, px: float, bold: bool = False) -> tuple[float, float]:
    """Width and height of a line of text at `px` pixels, in the picture's pixels."""
    size = round(px * SCALE)
    left, top, right, bottom = font(size).getbbox(text or " ", stroke_width=_stroke(size, bold))
    return (right - left) / SCALE, (bottom - top) / SCALE


def _text(img: Image.Image, draw: ImageDraw.ImageDraw, op: Text) -> None:
    size = round(op.size * SCALE)
    kw = {"font": font(size), "fill": op.color, "stroke_width": _stroke(size, op.bold), "stroke_fill": op.color}
    x, y = op.x * SCALE, op.y * SCALE
    if not op.angle:
        draw.text((x, y), op.text, anchor=op.anchor, **kw)
        return
    # Rotated: drawn on its own transparent tile, turned, then placed by the anchor's corner of the turned tile.
    left, top, right, bottom = font(size).getbbox(op.text, stroke_width=kw["stroke_width"])
    tile = Image.new("RGBA", (right - left + 4, bottom - top + 4), (0, 0, 0, 0))
    ImageDraw.Draw(tile).text((2 - left, 2 - top), op.text, **kw)
    tile = tile.rotate(op.angle, expand=True, resample=Image.BICUBIC)
    across, down = op.anchor[0], op.anchor[1]
    dx = {"l": 0, "m": tile.width / 2, "r": tile.width}[across]
    dy = {"a": 0, "t": 0, "m": tile.height / 2, "s": tile.height, "b": tile.height, "d": tile.height}[down]
    img.paste(tile, (round(x - dx), round(y - dy)), tile)


def _line(draw: ImageDraw.ImageDraw, op: Line) -> None:
    points = [(x * SCALE, y * SCALE) for x, y in op.points]
    width = max(1, round(op.width * SCALE))
    if len(points) > 1:
        draw.line(points, fill=op.color, width=width, joint="curve")
    r = width / 2
    for x, y in (points[0], points[-1]):  # round ends
        draw.ellipse((x - r, y - r, x + r, y + r), fill=op.color)


def png(scene: Scene) -> bytes:
    img = Image.new("RGB", (W * SCALE, H * SCALE), scene.bg)
    draw = ImageDraw.Draw(img)
    for op in scene.ops:
        if isinstance(op, Text):
            _text(img, draw, op)
        elif isinstance(op, Line):
            _line(draw, op)
        elif isinstance(op, Rect):
            x0, y0, x1, y1 = (v * SCALE for v in op.box)
            draw.rectangle((min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)), fill=op.color)
        elif isinstance(op, Dot):
            x, y, r = op.x * SCALE, op.y * SCALE, op.r * SCALE
            (draw.rectangle if op.square else draw.ellipse)((x - r, y - r, x + r, y + r), fill=op.color)
        elif isinstance(op, Wedge):
            x, y, r = op.cx * SCALE, op.cy * SCALE, op.r * SCALE
            if op.end - op.start >= 360 - 1e-9:
                draw.ellipse((x - r, y - r, x + r, y + r), fill=op.color)
            else:  # Pillow counts degrees clockwise from 3 o'clock
                draw.pieslice((x - r, y - r, x + r, y + r), op.start - 90, op.end - 90, fill=op.color)
    out = io.BytesIO()
    img.resize((W, H), Image.LANCZOS, reducing_gap=2.0).save(out, format="PNG")
    return out.getvalue()


def point_on(cx: float, cy: float, r: float, angle: float) -> tuple[float, float]:
    """The point at `angle` degrees clockwise from 12 o'clock on a circle."""
    a = math.radians(angle)
    return cx + r * math.sin(a), cy - r * math.cos(a)
