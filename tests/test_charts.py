import json
import math
from datetime import date, datetime

import pytest

from lib import config
from lib.context import Ctx
from lib.features import charts
from lib.features.charts import render
from lib.features.charts.render import Chart, Series

from .conftest import FakeBot, user_msg
from .fakes import FakeMcp, ScriptedBackend, registry, step
from .test_handlers import make_ctx, run


@pytest.fixture(autouse=True)
def matplotlib_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("MPLCONFIGDIR", str(tmp_path / "mpl"))


def history_json(closes, start=1, truncated=False):
    rows = [{"Date": f"2026-01-{start + i:02d}T00:00:00-05:00", "Open": c, "Close": c} for i, c in enumerate(closes)]
    body = {"ticker": "NVDA", "period": "1y", "interval": "1wk", "totalRecords": len(rows) + (5 if truncated else 0),
            "data": rows, "returnedRecords": len(rows), "truncated": truncated}
    return json.dumps(body)


def make(env, store, *servers):
    return Ctx(config.load(env), store, ScriptedBackend([]), registry(*servers), FakeBot())


def test_two_axes_each_with_several_series_and_either_inverted():
    days = [datetime(2026, 1, d) for d in (1, 2, 3)]
    chart = Chart("Gold vs AUD", [
        Series("Gold", days, [1.0, 2.0, 3.0]), Series("Silver", days, [2.0, 1.0, 2.0]),
        Series("AUD", days, [0.6, 0.65, 0.7], axis="right"), Series("NZD", days, [0.5, 0.55, 0.6], axis="right")],
        left_label="USD/oz", right_label="USD", invert_left=False, invert_right=True)
    fig = render.figure(chart)
    left, right = fig.axes
    assert len(left.get_lines()) == 2 and len(right.get_lines()) == 2
    assert not left.yaxis_inverted() and right.yaxis_inverted()
    assert left.get_ylabel() == "USD/oz" and right.get_ylabel() == "USD (inverted)"
    assert [t.get_text() for t in left.get_legend().get_texts()] == ["Gold", "Silver", "AUD (right)", "NZD (right)"]
    chart.invert_left = True
    assert render.figure(chart).axes[0].yaxis_inverted()


def test_one_axis_bars_and_labels():
    chart = Chart("Monthly", [Series("A", ["Jan", "Feb"], [1.0, -2.0], kind="bar"),
                              Series("B", ["Jan", "Feb"], [0.5, 1.0], kind="bar")], labels=["Jan", "Feb"])
    fig = render.figure(chart)
    assert len(fig.axes) == 1 and len(fig.axes[0].patches) == 4
    assert [t.get_text() for t in fig.axes[0].get_xticklabels()] == ["Jan", "Feb"]
    assert render.png(chart).startswith(b"\x89PNG\r\n\x1a\n")


def test_x_values_become_dates_numbers_or_labels():
    s = [Series("a", ["2026-01-02", "2026-02"], []), Series("b", ["2026-03-01T10:00:00-04:00"], [])]
    assert charts.unify_x(s) == [] and s[0].x == [datetime(2026, 1, 2), datetime(2026, 2, 1)]
    assert s[1].x == [datetime(2026, 3, 1, 10)]
    s = [Series("a", [2023, "2024"], [])]
    assert charts.unify_x(s) == [] and s[0].x == [2023.0, 2024.0]
    s = [Series("a", ["2024-Q1", "2024-Q2"], []), Series("b", ["2024-Q2", "2024-Q3"], [])]
    assert charts.unify_x(s) == ["2024-Q1", "2024-Q2", "2024-Q3"]
    with pytest.raises(charts.ChartError, match="all dates, all numbers or all labels"):
        charts.unify_x([Series("a", ["2026-01-02"], []), Series("b", ["2024-Q2"], [])])


def test_default_interval_fits_yahoos_row_limit():
    assert charts.default_interval("3mo", date(2026, 10, 1)) == "1d"
    assert charts.default_interval("1y", date(2026, 10, 1)) == "1wk"
    assert charts.default_interval("ytd", date(2026, 3, 1)) == "1d"
    assert charts.default_interval("ytd", date(2026, 10, 1)) == "1wk"


async def test_a_ticker_series_is_fetched_as_json_and_summarised(env, store):
    yahoo = FakeMcp("yahoo", tools=("get_historical_prices",), reply=history_json([100, 110, 120]))
    ctx = make(env, store, yahoo)
    chart, lines = await charts.build(ctx, {"title": "NVDA", "series": [{"name": "NVDA", "ticker": "NVDA"}]})
    assert yahoo.calls == [("get_historical_prices",
                            {"ticker": "NVDA", "period": "1y", "interval": "1wk", "response_format": "json"})]
    assert chart.series[0].y == [100, 110, 120] and chart.series[0].x[0] == datetime(2026, 1, 1)
    assert lines == ["NVDA (left axis, 3 points): 2026-01-01 100 -> 2026-01-03 120 (+20.0%), low 100, high 120"]


async def test_percent_change_rebases_and_labels_the_axis(env, store):
    yahoo = FakeMcp("yahoo", tools=("get_historical_prices",), reply=history_json([50, 75], truncated=True))
    ctx = make(env, store, yahoo)
    chart, lines = await charts.build(ctx, {"title": "t", "series": [
        {"name": "NVDA", "ticker": "NVDA", "percent_change": True},
        {"name": "Mine", "x": ["2026-01-01", "2026-01-02"], "y": [10, None], "axis": "right"}]})
    assert chart.series[0].y == [0.0, 50.0] and chart.left_label == "% change" and chart.right_label == ""
    assert math.isnan(chart.series[1].y[1])
    assert lines[0].endswith("high 50% (newest 2 of 7)") and lines[1].startswith("Mine (right axis, 1 points)")


@pytest.mark.parametrize("args, error", [
    ({"series": [{"name": "a", "x": [1], "y": [1]}]}, "give a title"),
    ({"title": "t", "series": [{"name": "a", "x": [1, 2], "y": [1]}]}, "x and y lists of the same length"),
    ({"title": "t", "series": [{"name": "a", "x": [1], "y": ["high"]}]}, "y values must be numbers"),
    ({"title": "t", "series": [{"name": "a", "x": [1], "y": [1], "axis": "top"}]}, "axis is left or right"),
    ({"title": "t", "series": [{"name": "a", "ticker": "X", "period": "3y"}]}, "period is one of"),
    ({"title": "t", "series": [{"name": f"s{i}", "x": [1], "y": [1]} for i in range(9)]}, "at most 8 series"),
    ({"title": "t", "series": [{"name": "a", "x": [1, 2], "y": [0, 1], "percent_change": True}]}, "non-zero first"),
])
async def test_bad_calls_get_an_error_the_model_can_fix(env, store, args, error):
    _, make_chart = charts.tool(make(env, store), charts.Charts())
    assert (await make_chart(args)).startswith("Error: ") and error in await make_chart(args)


async def test_missing_price_server_or_a_tool_error_is_reported(env, store):
    _, make_chart = charts.tool(make(env, store), charts.Charts())
    args = {"title": "t", "series": [{"name": "a", "ticker": "ZZZZ"}]}
    assert "price history isn't available" in await make_chart(args)
    yahoo = FakeMcp("yahoo", tools=("get_historical_prices",), reply="No historical data available for ZZZZ")
    _, make_chart = charts.tool(make(env, store, yahoo), charts.Charts())
    assert await make_chart(args) == "Error: ZZZZ: No historical data available for ZZZZ"


async def test_at_most_three_charts_per_reply(env, store):
    made = charts.Charts()
    _, make_chart = charts.tool(make(env, store), made)
    args = {"title": "t", "series": [{"name": "a", "x": [1, 2], "y": [1, 2]}]}
    for _ in range(3):
        assert (await make_chart(args)).startswith("Chart ")
    assert await make_chart(args) == "Error: at most 3 charts per reply."
    assert len(made.photos) == 3 and made.photos[0][1] == "t"


CHART_CALL = [("1", "make_chart", json.dumps({"title": "NVDA, 1 year", "series": [{"name": "NVDA", "ticker": "NVDA"}]}))]


async def test_a_chart_request_sends_the_photo_before_the_text_and_logs_it(env, store):
    yahoo = FakeMcp("yahoo", tools=("get_historical_prices",), reply=history_json([100, 120]))
    ctx, backend, h = make_ctx(env, store, [step(calls=CHART_CALL, finish="tool_calls"), step("Up 20%.")],
                               servers=(yahoo,))
    m = user_msg(ctx.bot, "@stockbot chart NVDA over the last year")
    await run(h, m)
    assert "make_chart" in backend.seen[0]["tools"]
    assert m.replies[0]["photo"].startswith(b"\x89PNG") and m.replies[0]["caption"] == "NVDA, 1 year"
    assert m.replies[0]["do_quote"] is True and m.replies[1]["text"] == "Up 20%."
    assert [r.text for r in store.history(-100, 20)][-2:] == ["[photo] NVDA, 1 year", "Up 20%."]


async def test_the_chart_tool_is_only_offered_when_a_chart_is_asked_for(env, store):
    ctx, backend, h = make_ctx(env, store, [step("fine"), step("ok")])
    await run(h, user_msg(ctx.bot, "@stockbot how is NVDA?"))
    assert "make_chart" not in backend.seen[0]["tools"]
    await run(h, user_msg(ctx.bot, "@stockbot plot these numbers", mid=2))
    assert backend.seen[1]["tools"] == ["make_chart"] and "no live data tools" not in backend.seen[1]["system"]


async def test_a_chart_that_telegram_rejects_still_lets_the_text_through(env, store):
    from telegram.error import BadRequest
    ctx, _, h = make_ctx(env, store, [step(calls=[("1", "make_chart", json.dumps(
        {"title": "t", "series": [{"name": "a", "x": [1, 2], "y": [1, 2]}]}))], finish="tool_calls"), step("Here.")])
    m = user_msg(ctx.bot, "@stockbot plot 1,2")

    async def rejected(photo, **kw):
        raise BadRequest("Photo_invalid_dimensions")

    m.reply_photo = rejected
    await run(h, m)
    assert [r["text"] for r in m.replies] == ["Here."]
