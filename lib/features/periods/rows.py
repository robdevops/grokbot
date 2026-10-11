"""Turning Sharesight performance reports into cached period rows, and the "what drove it" text."""

from __future__ import annotations

import json
from datetime import date

from ...mcp.results import table_records
from ...store import PeriodRow

MAX_MOVERS = 12  # holdings kept per period
Mover = tuple[str, float, object]  # code, total gain, gain %


def parse_report(text: str) -> dict | None:
    try:
        report = json.loads(text).get("report")
    except (ValueError, AttributeError):
        return None
    return report if isinstance(report, dict) and report.get("total_gain_percent") is not None else None


def movers(report: dict) -> list[list]:
    """The holdings with the largest gain or loss in a report, sold ones included: [[code, gain, gain %], ...]."""
    rows = [[h["code"], float(h["total_gain"]), h.get("total_gain_percent")]
            for h in table_records(report.get("holdings"))
            if h.get("code") and isinstance(h.get("total_gain"), (int, float)) and h["total_gain"]]
    return sorted(rows, key=lambda m: -abs(m[1]))[:MAX_MOVERS]


def drivers(rows: list, n: int = 3) -> str:
    """The holdings that added most and lost most: "up: CRWD +9,975 (+64.5%); down: ..."."""
    def show(m):
        return f"{m[0]} {m[1]:+,.0f}" + (f" ({m[2]:+.1f}%)" if isinstance(m[2], (int, float)) else "")
    ups = sorted((m for m in rows if m[1] > 0), key=lambda m: -m[1])[:n]
    downs = sorted((m for m in rows if m[1] < 0), key=lambda m: m[1])[:n]
    return "; ".join(f"{name}: {', '.join(map(show, group))}" for name, group in (("up", ups), ("down", downs)) if group)


async def fetch_row(server, portfolio_id: int, first: date, last: date) -> PeriodRow | None:
    """One window's return from Sharesight (sold holdings included), or None if no report came back."""
    text = await server.call("get_performance_report", {
        "portfolio_id": portfolio_id, "start_date": first.isoformat(), "end_date": last.isoformat(),
        "_keep_sales": True})
    report = parse_report(text)
    if report is None:
        return None
    return PeriodRow(portfolio_id, first.isoformat(), last.isoformat(), float(report["total_gain_percent"]),
                     float(report.get("total_gain") or 0), report.get("currency") or "", movers(report))
