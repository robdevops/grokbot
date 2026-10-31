"""Where an axis's ticks go and what they say: round numbers, and dates labelled briefly."""

from __future__ import annotations

import math
from datetime import datetime, timedelta

EPOCH = datetime(1970, 1, 1)
STEPS = (1, 2, 2.5, 5, 10)  # a tick step is one of these times a power of ten
MAX_TICKS = 7  # dates
NUMBER_TICKS = 9  # numbers, as matplotlib's MaxNLocator picks for a chart this size


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


def number_label(v: float) -> str:
    """A plain x value: 2024, 2.5 (no thousands separators: these are often years)."""
    return str(int(round(v))) if float(v).is_integer() else f"{v:g}"


def nice_ticks(lo: float, hi: float, integer: bool = False, most: int = NUMBER_TICKS) -> list[float]:
    """Round ticks inside [lo, hi]: the smallest step of 1, 2, 2.5 or 5 x 10^n that gives at most `most`."""
    if not (math.isfinite(lo) and math.isfinite(hi)) or hi <= lo:
        return [lo] if math.isfinite(lo) else []
    raw = (hi - lo) / (most - 1)
    mag = 10 ** math.floor(math.log10(raw))
    if integer:
        mag = max(mag, 1)  # whole numbers (years, counts) step by at least 1
    for m in STEPS:
        step = m * mag
        if integer and m == 2.5:
            continue
        first = math.ceil(lo / step - 1e-9)
        count = math.floor(hi / step + 1e-9) - first + 1
        if count <= most:
            return [round((first + i) * step, 10) for i in range(count)]
    return [lo, hi]


def days(dt: datetime) -> float:
    """A date as a number for the x scale: days since 1970 (wall-clock; the bot's datetimes carry no zone)."""
    return (dt.replace(tzinfo=None) - EPOCH).total_seconds() / 86400


def _months(dt: datetime, n: int) -> datetime:
    month = dt.month - 1 + n
    return dt.replace(year=dt.year + month // 12, month=month % 12 + 1, day=1)


# (unit, step): tried smallest first, until the span holds at most MAX_TICKS
DATE_STEPS = ([("minute", n) for n in (5, 15, 30)] + [("hour", n) for n in (1, 2, 3, 6, 12)]
              + [("day", n) for n in (1, 2, 7, 14)] + [("month", n) for n in (1, 2, 3, 6)]
              + [("year", n) for n in (1, 2, 5, 10, 20, 50, 100)])


def _date_ticks(start: datetime, end: datetime, unit: str, n: int) -> list[datetime]:
    if unit in ("minute", "hour"):
        size = timedelta(minutes=n) if unit == "minute" else timedelta(hours=n)
        t = start.replace(second=0, microsecond=0, minute=0 if unit == "hour" else start.minute - start.minute % n)
        if unit == "hour":
            t = t.replace(hour=t.hour - t.hour % n)
        out = []
        while t <= end:
            if t >= start:
                out.append(t)
            t += size
        return out
    if unit == "day":
        t = start.replace(hour=0, minute=0, second=0, microsecond=0)
        out = []
        while t <= end:
            if t >= start and ((t.day - 1) % n == 0 if n < 7 else (t.day - 1) % n == 0 and t.day <= 28):
                out.append(t)
            t += timedelta(days=1)
        return out
    if unit == "month":
        t = start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        out = []
        while t <= end:
            if t >= start and (t.month - 1) % n == 0:
                out.append(t)
            t = _months(t, 1)
        return out
    first = start.year + (-start.year) % n
    years = [datetime(y, 1, 1) for y in range(first, end.year + 1, n)]
    return [t for t in years if start <= t <= end]


def date_label(t: datetime, unit: str) -> str:
    """Brief, with the bigger unit at its boundaries: 2026 / Feb / Mar, or Mar / 2 / 3, or 5 Mar / 06:00 / 12:00."""
    if unit == "year" or (t.month == 1 and t.day == 1 and t.hour == 0 and t.minute == 0):
        return str(t.year)
    if unit == "month" or (t.day == 1 and t.hour == 0 and t.minute == 0):
        return t.strftime("%b")
    if unit == "day":
        return str(t.day)
    if t.hour == 0 and t.minute == 0:
        return f"{t.day} {t:%b}"
    return t.strftime("%H:%M")


def date_ticks(start: datetime, end: datetime) -> list[tuple[datetime, str]]:
    """Ticks for a span of dates and their labels: the finest step that keeps them to MAX_TICKS (and at least two)."""
    if end <= start:
        return [(start, date_label(start, "day" if start.hour == start.minute == 0 else "hour"))]
    for unit, n in DATE_STEPS:
        found = _date_ticks(start, end, unit, n)
        if 2 <= len(found) <= MAX_TICKS:
            return [(t, date_label(t, unit)) for t in found]
    unit = "year" if end - start > timedelta(days=3650) else "minute"
    return [(start, date_label(start, unit)), (end, date_label(end, unit))]
