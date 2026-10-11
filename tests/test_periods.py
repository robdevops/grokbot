import json
from datetime import date, datetime

import pytest

from lib import config
from lib.context import Ctx
from lib.features import periods
from lib.features.periods import windows
from lib.store import PeriodRow

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
        rows = [["AAA", pct * 150, 40.0], ["BBB", -pct * 50, -10.0], ["CCC", 0, 0]]  # a sold holding could be among them
        return json.dumps({"report": {"total_gain_percent": pct, "total_gain": pct * 100, "currency": "AUD",
                                      "holdings": {"columns": ["code", "total_gain", "total_gain_percent"], "rows": rows}}})


class Always(Sharesight):
    async def call(self, tool, args):
        self.calls.append((tool, args))
        return json.dumps({"report": {"total_gain_percent": 1.0, "total_gain": 10.0, "currency": "AUD"}})


def make(env, store, ss=None):
    ss = ss or Sharesight("sharesight", tools=("list_portfolios", "get_performance_report"))
    return Ctx(config.load(env), store, ScriptedBackend([]), registry(ss), FakeBot()), ss


def reports(ss):
    return [a for t, a in ss.calls if t == "get_performance_report"]


async def test_months_run_from_inception_with_sold_holdings_and_best_worst(env, store):
    ctx, ss = make(env, store)
    defn, fn = periods.tool(ctx)
    out = await fn({"portfolio_id": 7, "end": "2026-03"})
    assert defn.name == "period_returns" and "Total return per month (AUD)" in out
    assert "2025-12 -2.00% -200" in out and "2026-02 +5.00% +500" in out
    assert "Best by %: 2026-02 +5.00% (+500). Worst by %: 2025-12 -2.00% (-200)." in out
    assert "Best by money: 2026-02 +5.00% (+500). Worst by money: 2025-12 -2.00% (-200). 1 months had no data." in out  # March: no data
    first = reports(ss)[0]
    assert (first["start_date"], first["end_date"]) == ("2025-12-20", "2025-12-31")  # clipped to inception
    assert all(r["_keep_sales"] is True for r in reports(ss))


async def test_best_and_worst_periods_say_which_holdings_drove_them(env, store):
    ctx, ss = make(env, store)
    _, fn = periods.tool(ctx)
    out = await fn({"portfolio_id": 7, "end": "2026-03"})
    assert "Holdings behind 2026-02: up: AAA +750 (+40.0%); down: BBB -250 (-10.0%)" in out
    assert "Holdings behind 2025-12: up: BBB +100 (-10.0%); down: AAA -300 (+40.0%)" in out  # worst month
    assert "Holdings behind 2026-01" not in out and "CCC" not in out  # only the extremes, only movers


async def test_finished_periods_are_cached_forever_and_served_without_sharesight(env, store):
    ctx, ss = make(env, store)
    _, fn = periods.tool(ctx)
    first = await fn({"portfolio_id": 7, "start": "2025-12", "end": "2026-02"})
    n = len(reports(ss))
    assert n == 3 and store.period_count(7, "2000-01-01", "2100-01-01") == 3
    assert await fn({"portfolio_id": 7, "start": "2025-12", "end": "2026-02"}) == first  # same text, from the cache
    assert len(reports(ss)) == n
    assert store.period_flush(7) == 3 and store.period_flush() == 0
    await fn({"portfolio_id": 7, "start": "2025-12", "end": "2026-02"})
    assert len(reports(ss)) == 2 * n  # fetched again after the flush


async def test_the_unfinished_period_is_never_cached(env, store):
    ctx, ss = make(env, store, Always("sharesight", tools=("list_portfolios", "get_performance_report")))
    _, fn = periods.tool(ctx)
    today = datetime.now(ctx.st.tz).date()
    await fn({"portfolio_id": 7, "start": today.strftime("%Y-%m")})  # this month, ends today
    assert len(reports(ss)) == 1 and store.period_count(7, "2000-01-01", "2100-01-01") == 0


async def test_quarters_and_years_are_asked_for_whole_windows(env, store):
    ctx, ss = make(env, store)
    _, fn = periods.tool(ctx)
    out = await fn({"portfolio_id": 7, "period": "quarter", "start": "2025-10", "end": "2026-06"})
    assert [(r["start_date"], r["end_date"]) for r in reports(ss)] == [
        ("2025-10-01", "2025-12-31"), ("2026-01-01", "2026-03-31"), ("2026-04-01", "2026-06-30")]
    assert "2026-Q1 +1.00%" in out and "2 quarters had no data" in out
    ctx, ss = make(env, store)
    _, fn = periods.tool(ctx)
    await fn({"portfolio_id": 7, "period": "year", "start": "2024", "end": "2025-06"})
    assert [(r["start_date"], r["end_date"]) for r in reports(ss)] == [
        ("2024-01-01", "2024-12-31"), ("2025-01-01", "2025-06-30")]


async def test_start_given_skips_the_inception_lookup_and_bad_input_is_reported(env, store):
    ctx, ss = make(env, store)
    _, fn = periods.tool(ctx)
    out = await fn({"portfolio_id": 7, "start": "2026-01", "end": "2026-02"})
    assert out.splitlines()[1].startswith("2026-01 +1.00%") and not any(t == "list_portfolios" for t, _ in ss.calls)
    assert "portfolio_id" in await fn({})
    assert "period must be" in await fn({"portfolio_id": 7, "period": "decade"})
    assert "start as YYYY-MM-DD" in await fn({"portfolio_id": 99})  # unknown portfolio: no inception date
    assert "after end" in await fn({"portfolio_id": 7, "start": "2026-03", "end": "2026-01"})
    none_ctx = Ctx(config.load(env), store, ScriptedBackend([]), registry(), FakeBot())
    assert "isn't connected" in await periods.tool(none_ctx)[1]({"portfolio_id": 7})


async def test_only_uncached_windows_count_towards_the_live_limit(env, store):
    ctx, ss = make(env, store)
    _, fn = periods.tool(ctx)
    refused = await fn({"portfolio_id": 7, "period": "day", "start": "2020-01-01"})
    assert "0 of " in refused and "days are cached" in refused and "start=" in refused and reports(ss) == []
    # Cache every weekday from 2025-01-01 to 2026-03-31 except the last ten: the rest fits the live budget.
    days = windows.period_windows("day", date(2025, 1, 1), date(2026, 3, 31))
    store.period_put([PeriodRow(7, a.isoformat(), b.isoformat(), 0.5, 5.0, "AUD", []) for _, a, b in days[:-10]])
    out = await fn({"portfolio_id": 7, "period": "day", "start": "2025-01-01", "end": "2026-03-31"})
    assert not out.startswith("Error") and len(reports(ss)) == 10  # only the ten uncached days were fetched


@pytest.mark.parametrize("period, first, last, labels", [
    ("month", date(2025, 11, 15), date(2026, 2, 10), ["2025-11", "2025-12", "2026-01", "2026-02"]),
    ("quarter", date(2025, 8, 1), date(2026, 2, 10), ["2025-Q3", "2025-Q4", "2026-Q1"]),
    ("year", date(2024, 6, 1), date(2026, 2, 10), ["2024", "2025", "2026"]),
    ("week", date(2026, 3, 4), date(2026, 3, 17), ["2026-W10", "2026-W11", "2026-W12"]),
    ("day", date(2026, 3, 6), date(2026, 3, 10), ["2026-03-06", "2026-03-09", "2026-03-10"]),  # Fri, Mon, Tue
])
def test_period_windows_labels_and_weekends(period, first, last, labels):
    w = windows.period_windows(period, first, last)
    assert [x[0] for x in w] == labels
    assert w[0][1] == first and w[-1][2] == last  # clipped to the requested range


def test_dates_accept_day_month_or_year_and_end_means_the_period_end():
    d = windows.parse_date
    assert d("2026-02-03") == date(2026, 2, 3)
    assert d("2026-02") == date(2026, 2, 1) and d("2026-02", end=True) == date(2026, 2, 28)
    assert d("2025-12", end=True) == date(2025, 12, 31) and d("2025", end=True) == date(2025, 12, 31)
    assert d("nope") is None


def test_refusal_suggests_the_earliest_start_that_fits_and_ignores_cached_windows():
    w = windows.period_windows("week", date(2026, 1, 5), date(2027, 1, 3))  # 52 weeks
    msg = windows.refusal("week", w, set())
    assert "0 of 52 weeks are cached" in msg and f"start={w[-26][1]}" in msg
    assert windows.refusal("week", w, {windows.key(x) for x in w[:30]}) is None  # 22 uncached: fits


def test_flush_scope_understands_portfolio_year_month_day_and_all():
    f = windows.flush_scope
    assert f([]) == (None, None, None) and f(["all"]) == (None, None, None)
    assert f(["684141"]) == (684141, None, None)
    assert f(["2024"]) == (None, date(2024, 1, 1), date(2024, 12, 31))
    assert f(["2024-05", "684141"]) == (684141, date(2024, 5, 1), date(2024, 5, 31))
    assert f(["2024-05-17"]) == (None, date(2024, 5, 17), date(2024, 5, 17))
    assert f(["nope"]) is None and f(["684141", "702349"]) is None and f(["2024", "2025"]) is None
    assert f(["1234"]) is None  # 4 digits that aren't a year in range


async def test_best_by_money_can_differ_from_best_by_percent(env, store):
    class Mixed(Sharesight):
        async def call(self, tool, args):
            self.calls.append((tool, args))
            pct, gain = {"2026-01": (10.0, 100.0), "2026-02": (2.0, 900.0)}[args["start_date"][:7]]
            return json.dumps({"report": {"total_gain_percent": pct, "total_gain": gain, "currency": "AUD"}})

    ctx, _ = make(env, store, Mixed("sharesight", tools=("list_portfolios", "get_performance_report")))
    _, fn = periods.tool(ctx)
    out = await fn({"portfolio_id": 7, "start": "2026-01", "end": "2026-02"})
    assert "Best by %: 2026-01 +10.00% (+100)" in out and "Best by money: 2026-02 +2.00% (+900)" in out
    assert "Worst by money: 2026-01 +10.00% (+100)" in out


async def test_long_results_list_only_the_extremes_not_every_row(env, store):
    class Spread(Sharesight):
        async def call(self, tool, args):
            self.calls.append((tool, args))
            day = int(args["start_date"][-2:]) + 31 * int(args["start_date"][5:7])  # a different number every day
            return json.dumps({"report": {"total_gain_percent": day / 10, "total_gain": -day * 10.0, "currency": "AUD"}})

    ctx, _ = make(env, store, Spread("sharesight", tools=("list_portfolios", "get_performance_report")))
    _, fn = periods.tool(ctx)
    out = await fn({"portfolio_id": 7, "period": "day", "start": "2026-01-01", "end": "2026-03-31"})
    lines = out.splitlines()
    assert len(lines) < 15 and "full list is left out" in lines[0] and "64 days" in lines[0]
    assert lines[1].startswith("Top 5 by %: 2026-03-31 ") and lines[2].startswith("Bottom 5 by %: 2026-01-01 ")
    assert lines[3].startswith("Top 5 by money: 2026-01-01 ") and lines[4].startswith("Bottom 5 by money: 2026-03-31 ")
    assert "Best by %:" in out
    short = await fn({"portfolio_id": 7, "period": "day", "start": "2026-03-02", "end": "2026-03-13"})
    assert "Top 5" not in short and "2026-03-02 " in short  # a short range still lists every row
