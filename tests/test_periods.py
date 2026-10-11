import json
from datetime import date, datetime

import pytest

from lib import config
from lib.context import Ctx
from lib.features import periods

from .conftest import FakeBot
from .fakes import FakeMcp, ScriptedBackend, registry


class Sharesight(FakeMcp):
    """Reports by start date: +1% in 2026-01, +5% in 2026-02, -2% in 2025-12; no data otherwise."""

    async def call(self, tool, args):
        self.calls.append((tool, args))
        if tool == "list_portfolios":
            return json.dumps({"portfolios": [{"id": 7, "inception_date": "2025-12-20"}]})
        pct = {"2025-12": -2.0, "2026-01": 1.0, "2026-02": 5.0}.get(args["start_date"][:7])
        if pct is None:
            return "Tool error: no data"
        return json.dumps({"report": {"total_gain_percent": pct, "total_gain": pct * 100, "currency": "AUD"}})


def make(env, ss=None):
    ss = ss or Sharesight("sharesight", tools=("list_portfolios", "get_performance_report"))
    return Ctx(config.load(env), None, ScriptedBackend([]), registry(ss), FakeBot()), ss


def reports(ss):
    return [a for t, a in ss.calls if t == "get_performance_report"]


async def test_months_run_from_inception_with_sold_holdings_and_best_worst(env):
    ctx, ss = make(env)
    defn, fn = periods.tool(ctx)
    out = await fn({"portfolio_id": 7, "end": "2026-03"})
    assert defn.name == "period_returns" and "Total return per month (AUD)" in out
    assert "2025-12 -2.00% -200" in out and "2026-02 +5.00% +500" in out
    assert "Best: 2026-02 +5.00%. Worst: 2025-12 -2.00%. 1 months had no data." in out  # March: no data
    first = reports(ss)[0]
    assert (first["start_date"], first["end_date"]) == ("2025-12-20", "2025-12-31")  # clipped to inception
    assert all(r["_keep_sales"] is True for r in reports(ss))


async def test_quarters_and_years_are_asked_for_whole_windows(env):
    ctx, ss = make(env)
    _, fn = periods.tool(ctx)
    out = await fn({"portfolio_id": 7, "period": "quarter", "start": "2025-10", "end": "2026-06"})
    assert [(r["start_date"], r["end_date"]) for r in reports(ss)] == [
        ("2025-10-01", "2025-12-31"), ("2026-01-01", "2026-03-31"), ("2026-04-01", "2026-06-30")]
    assert "2026-Q1 +1.00%" in out and "2 quarters had no data" in out
    ctx, ss = make(env)
    _, fn = periods.tool(ctx)
    await fn({"portfolio_id": 7, "period": "year", "start": "2024", "end": "2025-06"})
    assert [(r["start_date"], r["end_date"]) for r in reports(ss)] == [
        ("2024-01-01", "2024-12-31"), ("2025-01-01", "2025-06-30")]


async def test_start_given_skips_the_inception_lookup_and_bad_input_is_reported(env):
    ctx, ss = make(env)
    _, fn = periods.tool(ctx)
    out = await fn({"portfolio_id": 7, "start": "2026-01", "end": "2026-02"})
    assert out.splitlines()[1].startswith("2026-01 +1.00%") and not any(t == "list_portfolios" for t, _ in ss.calls)
    assert "portfolio_id" in await fn({})
    assert "period must be" in await fn({"portfolio_id": 7, "period": "decade"})
    assert "start as YYYY-MM-DD" in await fn({"portfolio_id": 99})  # unknown portfolio: no inception date
    assert "after end" in await fn({"portfolio_id": 7, "start": "2026-03", "end": "2026-01"})
    none_ctx = Ctx(config.load(env), None, ScriptedBackend([]), registry(), FakeBot())
    assert "isn't connected" in await periods.tool(none_ctx)[1]({"portfolio_id": 7})


async def test_day_and_week_are_limited_to_recent_windows_and_refuse_without_calling(env):
    ctx, ss = make(env)
    _, fn = periods.tool(ctx)
    day = await fn({"portfolio_id": 7, "period": "day", "start": "2020-01-01"})
    week = await fn({"portfolio_id": 7, "period": "week", "start": "2020-01-01"})
    assert "limited to the last 90 days" in day and "start=" in day
    assert "limited to 26 weeks" in week and "start=" in week
    assert "limit is 120" in await fn({"portfolio_id": 7, "period": "month", "start": "2000-01"})
    assert reports(ss) == []


async def test_day_and_week_default_to_the_allowed_window(env):
    ctx, ss = make(env)
    _, fn = periods.tool(ctx)
    today = datetime.now(ctx.st.tz).date()
    await fn({"portfolio_id": 7, "period": "day"})
    days = [r["start_date"] for r in reports(ss)]
    assert days and all(date.fromisoformat(d).weekday() < 5 for d in days) and date.fromisoformat(days[-1]) <= today
    ss.calls.clear()
    await fn({"portfolio_id": 7, "period": "week"})
    assert len(reports(ss)) == periods.MAX_WEEKS


@pytest.mark.parametrize("period, first, last, labels", [
    ("month", date(2025, 11, 15), date(2026, 2, 10), ["2025-11", "2025-12", "2026-01", "2026-02"]),
    ("quarter", date(2025, 8, 1), date(2026, 2, 10), ["2025-Q3", "2025-Q4", "2026-Q1"]),
    ("year", date(2024, 6, 1), date(2026, 2, 10), ["2024", "2025", "2026"]),
    ("week", date(2026, 3, 4), date(2026, 3, 17), ["2026-W10", "2026-W11", "2026-W12"]),
    ("day", date(2026, 3, 6), date(2026, 3, 10), ["2026-03-06", "2026-03-09", "2026-03-10"]),  # Fri, Mon, Tue
])
def test_period_windows_labels_and_weekends(period, first, last, labels):
    w = periods.period_windows(period, first, last)
    assert [x[0] for x in w] == labels
    assert w[0][1] == first and w[-1][2] == last  # clipped to the requested range


def test_dates_accept_day_month_or_year_and_end_means_the_period_end():
    assert periods._date("2026-02-03") == date(2026, 2, 3)
    assert periods._date("2026-02") == date(2026, 2, 1) and periods._date("2026-02", end=True) == date(2026, 2, 28)
    assert periods._date("2025-12", end=True) == date(2025, 12, 31) and periods._date("2025", end=True) == date(2025, 12, 31)
    assert periods._date("nope") is None
