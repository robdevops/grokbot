"""Period windows for period_returns: parsing dates, cutting a date range into days/weeks/months/quarters/years,
and the limits on how many uncached ones one request may fetch."""

from __future__ import annotations

from datetime import date, datetime, timedelta

PERIODS = ("day", "week", "month", "quarter", "year")
# Uncached reports one call may fetch (about 2.4 s each); cached ones are free.
LIVE_LIMIT = {"day": 64, "week": 26, "month": 120, "quarter": 120, "year": 120}

Window = tuple[str, date, date]  # label, first day, last day


def parse_date(text: str, *, end: bool = False) -> date | None:
    """A date from YYYY-MM-DD, YYYY-MM or YYYY; a partial one means its first day, or its last for `end`."""
    text = str(text).strip()
    for fmt, size in (("%Y-%m-%d", 10), ("%Y-%m", 7), ("%Y", 4)):
        try:
            d = datetime.strptime(text, fmt).date()
        except ValueError:
            continue
        if end and size < 10:
            d = (date(d.year + 1, 1, 1) if size == 4 else month_add(d, 1)) - timedelta(days=1)
        return d
    return None


def month_add(d: date, months: int) -> date:
    n = d.year * 12 + d.month - 1 + months
    return date(n // 12, n % 12 + 1, 1)


def period_windows(period: str, first: date, last: date) -> list[Window]:
    """(label, first day, last day) of each period between `first` and `last`; the ends are clipped to them."""
    out: list[Window] = []
    if period == "day":
        d = first
        while d <= last:
            if d.weekday() < 5:  # no trading at weekends
                out.append((d.isoformat(), d, d))
            d += timedelta(days=1)
    elif period == "week":
        monday = first - timedelta(days=first.weekday())
        while monday <= last:
            iso = monday.isocalendar()
            out.append((f"{iso[0]}-W{iso[1]:02d}", max(monday, first), min(monday + timedelta(days=6), last)))
            monday += timedelta(days=7)
    else:
        step = {"month": 1, "quarter": 3, "year": 12}[period]
        cur = date(first.year, ((first.month - 1) // step) * step + 1, 1)
        while cur <= last:
            nxt = month_add(cur, step)
            label = {"month": f"{cur.year}-{cur.month:02d}", "quarter": f"{cur.year}-Q{(cur.month - 1) // 3 + 1}",
                     "year": str(cur.year)}[period]
            out.append((label, max(cur, first), min(nxt - timedelta(days=1), last)))
            cur = nxt
    return out


def key(w: Window) -> tuple[str, str]:
    return w[1].isoformat(), w[2].isoformat()


def refusal(period: str, windows: list[Window], cached: set[tuple[str, str]]) -> str | None:
    """Why a request needs too many uncached reports (with the earliest start that fits), or None."""
    limit = LIVE_LIMIT[period]
    missing = [w for w in windows if key(w) not in cached]
    if len(missing) <= limit:
        return None
    seen, start = 0, windows[-1][1]
    for w in reversed(windows):  # walk back from the end until the budget of uncached reports is spent
        if key(w) not in cached:
            seen += 1
            if seen > limit:
                break
        start = w[1]
    return (f"{len(windows) - len(missing)} of {len(windows)} {period}s are cached and the other {len(missing)} "
            f"would take about {len(missing) * 2.4 / 60:.0f} minutes to fetch (the background fill may still be "
            f"running or off). Ask again with start={start} or later, or try again later")
