"""Chart images (pie and horizontal bar) drawn with Pillow: no network, no config or cache directories."""

from __future__ import annotations

import io
import math

WIDTH = 1000
SCALE = 2  # drawn at 2x and shrunk, which smooths the edges
PALETTE = ["#4E79A7", "#F28E2B", "#59A14F", "#E15759", "#76B7B2", "#EDC948", "#B07AA1", "#FF9DA7", "#9C755F", "#86BCB6"]
OTHER_COLOUR, INK, MUTED, BACKGROUND, NEGATIVE = "#A0A0A0", "#222222", "#666666", "#FFFFFF", "#C8553D"
MAX_ITEMS = 30
MAX_SLICES = 10  # named slices before the rest become "Other"
OTHER_BELOW = 0.02  # slices under this share merge into "Other"
LABEL_CHARS = 28


class ChartError(ValueError):
    """The data can't be drawn as asked."""


def available() -> bool:
    try:
        import PIL  # noqa: F401
    except ImportError:
        return False
    return True


def short(value: float) -> str:
    """12345 -> 12.3k, 2500000 -> 2.5M, 3.2 -> 3.2."""
    mag = abs(value)
    if mag >= 1e6:
        return f"{value / 1e6:.1f}M"
    if mag >= 1e3:
        return f"{value / 1e3:.1f}k"
    return f"{value:.1f}" if mag < 10 and value != int(value) else f"{value:.0f}"


def clean(kind: str, items: object) -> list[tuple[str, float]]:
    """Validated (label, value) pairs; a pie keeps only positive values."""
    if kind not in ("pie", "bar"):
        raise ChartError("kind must be 'pie' or 'bar'")
    if not isinstance(items, list) or not items:
        raise ChartError("give a list of items with a label and a value each")
    out = []
    for item in items:
        try:
            label, value = str(item["label"]).strip(), float(item["value"])
        except (TypeError, KeyError, ValueError):
            raise ChartError("every item needs a label and a numeric value") from None
        if not math.isfinite(value):
            raise ChartError(f"the value for {label!r} isn't a number")
        out.append((label if len(label) <= LABEL_CHARS else label[:LABEL_CHARS - 1] + "…", value))
    if kind == "pie":
        out = [(label, value) for label, value in out if value > 0]
        if not out:
            raise ChartError("a pie chart needs at least one positive value")
    if len(out) > (200 if kind == "pie" else MAX_ITEMS):
        raise ChartError(f"too many items ({len(out)}); group them into at most {MAX_ITEMS}")
    return out


def pie_slices(items: list[tuple[str, float]]) -> list[tuple[str, float, float, bool]]:
    """(label, value, share, is_other) largest first; small slices and everything past MAX_SLICES merge."""
    total = sum(value for _, value in items)
    ranked = sorted(items, key=lambda i: -i[1])
    named = [i for i in ranked[:MAX_SLICES] if i[1] / total >= OTHER_BELOW]
    rest = sum(value for _, value in ranked[len(named):])
    slices = [(label, value, value / total, False) for label, value in named]
    if rest > 0:
        slices.append(("Other", rest, rest / total, True))
    return slices


def _font(size: int):
    from PIL import ImageFont
    try:
        return ImageFont.load_default(size=size * SCALE)
    except TypeError:  # Pillow before 10.1 has only the small bitmap font
        return ImageFont.load_default()


def _fit(draw, text: str, font, width: float) -> str:
    """Text cut with an ellipsis to fit `width` (1x pixels)."""
    if draw.textlength(text, font=font) <= width * SCALE:
        return text
    while text and draw.textlength(text + "…", font=font) > width * SCALE:
        text = text[:-1]
    return text.rstrip() + "…"


def draw(kind: str, title: str, items: list, subtitle: str = "") -> bytes:
    """A PNG of the chart. Raises ChartError for data that can't be drawn."""
    pairs = clean(kind, items)
    from PIL import Image, ImageDraw

    height = _pie_height(pairs) if kind == "pie" else 130 + 38 * len(pairs)
    img = Image.new("RGB", (WIDTH * SCALE, height * SCALE), BACKGROUND)
    d = ImageDraw.Draw(img)
    s = SCALE
    d.text((40 * s, 28 * s), _fit(d, title.strip() or "Chart", _font(36), WIDTH - 80), fill=INK, font=_font(36))
    if subtitle:
        d.text((40 * s, 76 * s), _fit(d, subtitle, _font(24), WIDTH - 80), fill=MUTED, font=_font(24))
    (_draw_pie if kind == "pie" else _draw_bar)(d, pairs, height)
    img = img.resize((WIDTH, height), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue()


def _pie_height(pairs: list[tuple[str, float]]) -> int:
    return max(600, 150 + 62 * len(pie_slices(pairs)))


def _draw_pie(d, pairs: list[tuple[str, float]], height: int) -> None:
    s = SCALE
    slices = pie_slices(pairs)
    left, top, size = 40, 130, 420
    box = [left * s, top * s, (left + size) * s, (top + size) * s]
    angle = -90.0
    for i, (_, _, share, is_other) in enumerate(slices):
        colour = OTHER_COLOUR if is_other else PALETTE[i % len(PALETTE)]
        end = angle + 360 * share
        if share > 0.9999:
            d.ellipse(box, fill=colour)
        else:
            d.pieslice(box, angle, end, fill=colour, outline=BACKGROUND, width=2 * s)
        angle = end
    label_font, detail_font = _font(26), _font(22)
    for i, (label, value, share, is_other) in enumerate(slices):
        y = 135 + 62 * i
        colour = OTHER_COLOUR if is_other else PALETTE[i % len(PALETTE)]
        d.rectangle([500 * s, (y + 4) * s, 524 * s, (y + 28) * s], fill=colour)
        d.text((540 * s, y * s), _fit(d, label, label_font, 420), fill=INK, font=label_font)
        d.text((540 * s, (y + 31) * s), f"{share * 100:.1f}%   {short(value)}", fill=MUTED, font=detail_font)


def _draw_bar(d, pairs: list[tuple[str, float]], height: int) -> None:
    s = SCALE
    x_left, x_right = 300, 860
    top_max = max(0.0, max(v for _, v in pairs))
    low_max = max(0.0, -min(v for _, v in pairs))
    span = (top_max + low_max) or 1.0
    zero = x_left + (x_right - x_left) * low_max / span
    label_font, value_font = _font(24), _font(22)
    for i, (label, value) in enumerate(pairs):
        y = 115 + 38 * i
        d.text((40 * s, (y + 2) * s), _fit(d, label, label_font, 245), fill=INK, font=label_font)
        length = (x_right - x_left) * abs(value) / span
        x0, x1 = (zero, zero + length) if value >= 0 else (zero - length, zero)
        d.rectangle([x0 * s, y * s, max(x1, x0 + 1) * s, (y + 28) * s], fill=PALETTE[0] if value >= 0 else NEGATIVE)
        text = short(value)
        if value >= 0:
            d.text(((x1 + 8) * s, (y + 2) * s), text, fill=MUTED, font=value_font)
        else:
            d.text((x0 * s - 8 * s - d.textlength(text, font=value_font), (y + 2) * s), text, fill=MUTED, font=value_font)
    d.line([zero * s, 108 * s, zero * s, (115 + 38 * len(pairs)) * s], fill=MUTED, width=s)
