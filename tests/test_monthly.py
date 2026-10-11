import json
from datetime import date

from lib import config
from lib.context import Ctx
from lib.features import monthly

from .conftest import FakeBot
from .fakes import FakeMcp, ScriptedBackend, registry


class Sharesight(FakeMcp):
    """Returns a report per month: +1% in 2026-01, +5% in 2026-02, nothing before 2026."""

    async def call(self, tool, args):
        self.calls.append((tool, args))
        if tool == "list_portfolios":
            return json.dumps({"portfolios": [{"id": 7, "inception_date": "2025-12-20"}]})
        start = args["start_date"]
        pct = {"2025-12": -2.0, "2026-01": 1.0, "2026-02": 5.0}.get(start[:7])
        if pct is None:
            return "Tool error: no data"
        return json.dumps({"report": {"total_gain_percent": pct, "total_gain": pct * 100, "currency": "AUD"}})


def make(env, ss):
    return Ctx(config.load(env), None, ScriptedBackend([]), registry(ss), FakeBot())


async def test_months_run_from_inception_with_sold_holdings_and_best_worst(env):
    ss = Sharesight("sharesight", tools=("list_portfolios", "get_performance_report"))
    defn, fn = monthly.tool(make(env, ss))
    out = await fn({"portfolio_id": 7, "end": "2026-03"})
    assert defn.name == "monthly_returns"
    assert "2025-12 -2.00% -200" in out and "2026-02 +5.00% +500" in out
    assert "Best: 2026-02 +5.00%. Worst: 2025-12 -2.00%. 1 months had no data." in out  # March: no data
    reports = [a for t, a in ss.calls if t == "get_performance_report"]
    assert [r["start_date"] for r in reports][:2] == ["2025-12-01", "2026-01-01"]
    assert reports[0]["end_date"] == "2025-12-31" and all(r["_keep_sales"] is True for r in reports)


async def test_start_given_skips_the_inception_lookup_and_bad_input_is_reported(env):
    ss = Sharesight("sharesight", tools=("list_portfolios", "get_performance_report"))
    _, fn = monthly.tool(make(env, ss))
    out = await fn({"portfolio_id": 7, "start": "2026-01", "end": "2026-02"})
    assert out.splitlines()[1].startswith("2026-01 +1.00%") and not any(t == "list_portfolios" for t, _ in ss.calls)
    assert "portfolio_id" in await fn({})
    assert "start as YYYY-MM" in await fn({"portfolio_id": 99})  # unknown portfolio: no inception date
    _, none = monthly.tool(Ctx(config.load(env), None, ScriptedBackend([]), registry(), FakeBot()))
    assert "isn't connected" in await none({"portfolio_id": 7})


def test_month_windows_end_today_in_the_current_month():
    w = monthly.month_windows((2025, 11), (2026, 2), date(2026, 2, 10))
    assert [x[0] for x in w] == ["2025-11", "2025-12", "2026-01", "2026-02"]
    assert w[0][2] == date(2025, 11, 30) and w[-1][2] == date(2026, 2, 10)
