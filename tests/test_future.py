import json
import math
from types import SimpleNamespace as N

import pytest

from lib.features import charts
from lib.features.charts import future

from .conftest import user_msg
from .fakes import FakeMcp
from .test_handlers import make_ctx, update_for

NAN = math.nan


def row(period, avg, low, high, ago, n, currency="USD", ago_key="yearAgoRevenue"):
    return {"period": period, "avg": avg, "low": low, "high": high, ago_key: ago, "numberOfAnalysts": n,
            "growth": 0.5, "currency": currency}


AMD = {"ticker": "AMD", "priceTargets": {"current": 419.8},
       "earningsEstimate": [row("0q", 2.95, 2.92, 2.99, 1.53, 7, ago_key="yearAgoEps"),
                            row("0y", 8.064, 8.018, 8.13, 4.17, 8, ago_key="yearAgoEps"),
                            row("+1y", 16.27, 9.6, 23.66, 8.064, 49, ago_key="yearAgoEps")],
       "revenueEstimate": [row("0y", 51.2e9, 48.34e9, 52.51e9, 34.64e9, 45),
                           row("+1y", 90.87e9, 61.26e9, 120.27e9, 51.2e9, 48)]}
# An ASX name: half-yearly reporting, so the quarter rows are empty (NaN), and the yearly ones are what counts.
NXT = {"ticker": "NXT.AX",
       "earningsEstimate": [row("0q", NAN, NAN, NAN, NAN, NAN, "AUD", "yearAgoEps"),
                            row("0y", -0.353, -0.454, -0.225, -0.0332, 10, "AUD", "yearAgoEps"),
                            row("+1y", -0.313, -0.839, -0.02, -0.353, 12, "AUD", "yearAgoEps")],
       "revenueEstimate": [row("0y", 601.97e6, 580.0e6, 618.5e6, 496.5e6, 15, "AUD"),
                           row("+1y", 1039.7e6, 889.4e6, 1201e6, 601.97e6, 15, "AUD")]}
STATEMENTS = "# Financial Statements: AMD\n\n## Income Statement (Annual)\n\n|  | 2025-12-31 | 2024-12-31 |\n"


class Yahoo(FakeMcp):
    def __init__(self, estimates):
        super().__init__("yahoo", tools=(future.ESTIMATES, future.STATEMENTS))
        self.estimates = estimates

    async def call(self, tool, args):
        self.calls.append((tool, args))
        if tool == future.ESTIMATES:
            return json.dumps(self.estimates)
        return STATEMENTS


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
    assert {tuple(s["x"]) for s in revenue["series"]} == {("FY2025", "FY2026", "FY2027")}  # from the statements
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
    assert ctx.registry.servers["yahoo"].calls[0] == (future.ESTIMATES, {"ticker": "NXT.AX", "response_format": "json"})
    assert "-A$0.84 / -A$0.31 / -A$0.02" in caption  # losses stay losses


async def test_nothing_usable_gives_none(env, store):
    empty = {"ticker": "ZZZZ", "earningsEstimate": [row("0y", NAN, NAN, NAN, NAN, NAN, ago_key="yearAgoEps")],
             "revenueEstimate": []}
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
        ctx, _, h = make_ctx(env, store, [], servers=(Yahoo({"ticker": "ZZZZ"}),))
        m = user_msg(ctx.bot, "/future " + " ".join(args))
        await h.future(update_for(m), N(args=args))
        assert m.replies[0]["text"].startswith(said)
