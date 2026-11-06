import json
from types import SimpleNamespace as N

import pytest

from lib.features import charts
from lib.features.charts import future

from .conftest import user_msg
from .fakes import FakeMcp
from .test_handlers import make_ctx, update_for


def est(avg, low, high, ago, n, currency, kind):
    """One estimate dict as earningsTrend has it."""
    if kind == "eps":
        return {"avg": avg, "low": low, "high": high, "yearAgoEps": ago, "numberOfAnalysts": n,
                "earningsCurrency": currency}
    return {"avg": avg, "low": low, "high": high, "yearAgoRevenue": ago, "numberOfAnalysts": n,
            "revenueCurrency": currency}


def trend(*rows):
    """A quoteSummary earningsTrend result: rows of (period, endDate, eps estimate, revenue estimate)."""
    return {"result": {"earningsTrend": {"trend": [
        {"period": p, "endDate": end, "earningsEstimate": e, "revenueEstimate": r} for p, end, e, r in rows]}}}


AMD = trend(("0q", "2026-12-31T00:00:00.000Z", est(2.95, 2.92, 2.99, 1.53, 7, "USD", "eps"),
             est(16.4e9, 13.3e9, 17.5e9, 10.27e9, 33, "USD", "rev")),
            ("0y", "2026-12-31T00:00:00.000Z", est(8.064, 8.018, 8.13, 4.17, 8, "USD", "eps"),
             est(51.2e9, 48.34e9, 52.51e9, 34.64e9, 45, "USD", "rev")),
            ("+1y", "2027-12-31T00:00:00.000Z", est(16.27, 9.6, 23.66, 8.064, 49, "USD", "eps"),
             est(90.87e9, 61.26e9, 120.27e9, 51.2e9, 48, "USD", "rev")))
# An ASX name: half-yearly reporting, so the quarter rows are empty (null), and the yearly ones are what counts; its
# fiscal year ends in June.
NXT = trend(("0q", None, est(None, None, None, None, None, None, "eps"), est(0, 0, 0, None, 0, None, "rev")),
            ("0y", "2027-06-30T00:00:00.000Z", est(-0.353, -0.454, -0.225, -0.0332, 10, "AUD", "eps"),
             est(601.97e6, 580.0e6, 618.5e6, 496.5e6, 15, "AUD", "rev")),
            ("+1y", "2028-06-30T00:00:00.000Z", est(-0.313, -0.839, -0.02, -0.353, 12, "AUD", "eps"),
             est(1039.7e6, 889.4e6, 1201e6, 601.97e6, 15, "AUD", "rev")))


class Yahoo(FakeMcp):
    def __init__(self, estimates):
        super().__init__("yahoo", tools=(future.ESTIMATES,))
        self.estimates = estimates

    async def call(self, tool, args):
        self.calls.append((tool, args))
        return json.dumps(self.estimates)


@pytest.mark.parametrize("text, symbol", [("nvda", "NVDA"), ("$NXT.AX", "NXT.AX"), ("ASX:NXT", "NXT.AX"),
                                          ("nasdaq:nvda", "NVDA")])
def test_the_ticker_can_be_written_any_usual_way(text, symbol):
    assert future.symbol_of(text) == symbol


async def test_low_average_and_high_fan_out_from_last_years_actual(env, store):
    ctx, backend, h = make_ctx(env, store, [], servers=(Yahoo(AMD),))
    made = charts.Charts()
    caption = await future.fast(ctx, "amd", made)
    assert [title for _, title in made.photos] == ["AMD revenue forecast ($B)", "AMD EPS forecast ($)"]
    revenue, eps = made.calls
    assert [s["name"] for s in revenue["series"]] == ["Low", "Average", "High"]
    assert {tuple(s["x"]) for s in revenue["series"]} == {("FY2025", "FY2026", "FY2027")}  # this FY ends in 2026
    low, avg, high = (s["y"] for s in revenue["series"])
    assert low[0] == avg[0] == high[0] == 34.64  # all three start at last year's actual
    assert (low[2], avg[2], high[2]) == (61.26, 90.87, 120.27)
    assert [s["total"] for s in revenue["series"]] == ["$61.3B", "$90.9B", "$120B"]
    assert [s["y"][2] for s in eps["series"]] == [9.6, 16.27, 23.66] and revenue["source"] == future.SOURCE
    assert "FY2027 $61.3B / $90.9B / $120B, 48 analysts" in caption and "EPS: FY2025 $4.17" in caption
    assert backend.seen == []  # no model


async def test_an_asx_name_uses_its_yearly_rows_and_currency(env, store):
    ctx, _, _ = make_ctx(env, store, [], servers=(Yahoo(NXT),))
    made = charts.Charts()
    caption = await future.fast(ctx, "ASX:NXT", made)
    assert [title for _, title in made.photos] == ["NXT.AX revenue forecast (A$B)", "NXT.AX EPS forecast (A$)"]
    assert ctx.registry.servers["yahoo"].calls == [
        ("quoteSummary", {"symbol": "NXT.AX", "queryOptions": {"modules": ["earningsTrend"]}})]
    assert made.calls[0]["series"][0]["x"] == ["FY2026", "FY2027", "FY2028"]  # a June year: FY2027 ends 2027-06-30
    assert "-A$0.84 / -A$0.31 / -A$0.02" in caption  # losses stay losses


async def test_nothing_usable_gives_none(env, store):
    empty = trend(("0y", None, est(None, None, None, None, None, None, "eps"), None))
    ctx, _, _ = make_ctx(env, store, [], servers=(Yahoo(empty),))
    assert await future.fast(ctx, "zzzz", charts.Charts()) is None
    ctx, _, _ = make_ctx(env, store, [], servers=())  # no Yahoo server
    assert await future.fast(ctx, "amd", charts.Charts()) is None


async def test_the_command_replies_with_the_charts_or_says_why_not(env, store):
    ctx, backend, h = make_ctx(env, store, [], servers=(Yahoo(AMD),))
    m = user_msg(ctx.bot, "/future amd", chat_id=-100, mid=3)
    await h.future(update_for(m), N(args=["amd"]))
    [reply] = m.replies
    assert reply["caption"].startswith("<b>AMD</b> analyst consensus") and reply["photo"].startswith(b"\x89PNG")
    assert store.history(-100, 20)[0].text == "/future amd" and backend.seen == []
    for args, said in (([], "Usage: /future TICKER"), (["zzzz"], "No analyst forecasts found for ZZZZ.")):
        ctx, _, h = make_ctx(env, store, [], servers=(Yahoo({"result": {}}),))
        m = user_msg(ctx.bot, "/future " + " ".join(args))
        await h.future(update_for(m), N(args=args))
        assert m.replies[0]["text"].startswith(said)
