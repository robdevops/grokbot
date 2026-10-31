import asyncio
import io
import json
import math
from dataclasses import replace
from datetime import date, datetime
from types import SimpleNamespace as N

import pytest
from PIL import Image

from lib import config
from lib.context import Ctx
from lib.features import charts
from lib.features.charts import buttons, pages, render
from lib.features.charts.render import Chart, Series
from lib.features.charts.scene import PT

from .conftest import FakeBot, user_msg
from .fakes import FakeMcp, ScriptedBackend, registry, step
from .test_handlers import make_ctx, run

REAL_PNG = render.png  # the real renderer, for the few tests that look at the picture


@pytest.fixture(autouse=True)
def fast_png(monkeypatch):
    """Most tests need a picture to exist, not to be drawn: a PNG header stands in (drawing takes ~0.15 s each, and
    the suite makes dozens). The tests that check drawing call REAL_PNG; the rest look at the scene."""
    monkeypatch.setattr(render, "png", lambda chart: b"\x89PNG\r\n\x1a\nfake")


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
    scene = render.scene(chart)
    assert len(scene.find("line")) == 4
    assert scene.inverted == {"left": False, "right": True}
    assert scene.axis_titles == {"left": "USD/oz", "right": "USD (inverted)"}
    assert scene.legend == ["Gold", "Silver", "AUD (right)", "NZD (right)"]
    chart.invert_left = True
    assert render.scene(chart).inverted["left"]


def test_an_inverted_axis_draws_bigger_values_lower():
    def ends(chart):
        [line] = render.scene(chart).find("line")
        return line.points[0][1], line.points[-1][1]  # y in pixels: down the picture
    chart = Chart("t", [Series("a", DAYS, [1.0, 2.0])])
    first, last = ends(chart)
    assert last < first  # 2 is higher up than 1
    first, last = ends(replace(chart, invert_left=True))
    assert last > first


def test_one_axis_bars_and_labels():
    chart = Chart("Monthly", [Series("A", ["Jan", "Feb"], [1.0, -2.0], kind="bar"),
                              Series("B", ["Jan", "Feb"], [0.5, 1.0], kind="bar")], labels=["Jan", "Feb"])
    scene = render.scene(chart)
    assert len(scene.find("bar")) == 4 and list(scene.inverted) == ["left"]
    assert scene.x_labels == ["Jan", "Feb"]
    picture = Image.open(io.BytesIO(REAL_PNG(chart)))
    assert picture.size == (1224, 714) and picture.getpixel((2, 2))[:3] == (255, 255, 255)
    assert (0x25, 0x63, 0xeb) in {c[:3] for _, c in picture.getcolors(1224 * 714)}  # the first bar's blue


def test_tick_labels_share_their_decimals_and_shorten_big_numbers():
    label = render.tick_label
    assert [label(v, [0.58, 0.6, 0.62], False) for v in (0.58, 0.6)] == ["0.58", "0.60"]
    assert [label(v, [2600, 2800, 3000], False) for v in (2600, 3000)] == ["2,600", "3,000"]
    assert [label(v, [1e8, 1.5e8, 2e8], False) for v in (1e8, 1.5e8)] == ["100M", "150M"]
    assert label(1.25e9, [1e9, 1.25e9, 1.5e9], False) == "1.25B"
    assert [label(v, [-10, 0, 10], True) for v in (-10, -1e-15, 10)] == ["-10%", "0%", "10%"]


DAYS = [datetime(2026, 1, d) for d in (1, 2)]


def end_label_texts(chart):
    return render.scene(chart).end_labels


def test_lines_are_named_at_their_ends_with_their_totals_and_have_no_legend():
    chart = Chart("t", [Series("NBIS", DAYS, [0.0, -27.1], total="-27.1%"), Series("Mine", DAYS, [1.0, 2.0])])
    assert render.scene(chart).legend == []
    assert end_label_texts(chart) == ["NBIS  -27.1%", "Mine"]
    assert end_label_texts(Chart("t", [Series("NVDA", DAYS, [1.0, 2.0], total="+100.0%")])) == ["NVDA  +100.0%"]


def test_two_axes_or_bars_keep_a_legend_with_the_totals():
    legend = lambda chart: render.scene(chart).legend  # noqa: E731
    two = Chart("t", [Series("NBIS", DAYS, [0.0, -27.1], total="-27.1%"),
                      Series("Gold", DAYS, [1.0, 2.0], axis="right", total="+100.0%"),
                      Series("Mine", DAYS, [1.0, 2.0])])
    assert legend(two) == ["NBIS -27.1%", "Mine", "Gold +100.0% (right)"] and end_label_texts(two) == []
    assert legend(Chart("t", [Series("A", DAYS, [1.0, 2.0], kind="bar", total="+5%")])) == ["A +5%"]


def test_one_y_axis_has_no_title_unless_it_is_inverted_and_two_keep_theirs():
    one = Chart("t", [Series("a", DAYS, [1.0, 2.0])], left_label="Price")
    assert render.scene(one).axis_titles == {"left": ""} and not render.scene(one).find("axis-title")
    assert render.scene(replace(one, invert_left=True)).axis_titles == {"left": "inverted"}
    two = Chart("t", [Series("a", DAYS, [1.0, 2.0]), Series("b", DAYS, [1.0, 2.0], axis="right")],
                left_label="Price", right_label="Rate")
    assert render.scene(two).axis_titles == {"left": "Price", "right": "Rate"}


def test_a_chart_of_right_axis_series_alone_is_drawn_on_one_axis_with_the_right_settings():
    chart = Chart("t", [Series("a", DAYS, [1.0, 2.0], axis="right")], right_label="Rate", invert_right=True)
    scene = render.scene(chart)
    assert scene.inverted == {"left": True} and scene.axis_titles == {"left": "inverted"}
    assert end_label_texts(chart) == ["a"]


def test_end_labels_are_pushed_apart_in_order_and_kept_on_the_plot():
    assert render.spread([0.5], 0.1) == [0.5]
    assert render.spread([0.5, 0.52, 0.9], 0.1) == pytest.approx([0.5, 0.6, 0.9])
    assert render.spread([0.52, 0.5], 0.1) == pytest.approx([0.6, 0.5])  # order is by height, not by series
    assert render.spread([0.95, 0.98, 1.0], 0.1) == pytest.approx([0.8, 0.9, 1.0])  # pushed back down from the top


def test_the_x_axis_has_strokes_above_its_labels_and_the_y_axis_none():
    scene = render.scene(Chart("t", [Series("a", DAYS, [1.0, 2.0])]))
    strokes, labels = scene.find("x-tick"), scene.find("x-label")
    assert strokes and len(strokes) == len(labels)
    assert all(s.points[0][0] == s.points[1][0] and s.points[1][1] < label.y for s, label in zip(strokes, labels, strict=True))
    assert scene.find("y-label") and not scene.find("y-tick")


def test_the_dark_theme_has_a_dark_background_and_light_text():
    light = render.scene(Chart("t", [Series("a", DAYS, [1.0, 2.0])]))
    dark = render.scene(Chart("t", [Series("a", DAYS, [1.0, 2.0])], dark=True))
    assert light.bg == "#ffffff" and dark.bg == "#141a22"
    [title] = dark.find("title")
    assert title.text == "t" and title.color == render.THEMES["dark"].ink and title.bold


def test_charts_are_dark_from_9pm_to_7am():
    assert [charts.is_night(h) for h in (6, 7, 12, 20, 21, 23, 0)] == [True, False, False, False, True, True, True]


async def test_a_chart_drawn_at_night_is_dark(env, store, monkeypatch):
    from zoneinfo import ZoneInfo
    args = {"title": "t", "series": [{"name": "a", "x": [1, 2], "y": [1, 2]}]}
    for hour, dark in ((22, True), (13, False)):
        monkeypatch.setattr(charts, "now", lambda tz, h=hour: datetime(2026, 10, 30, h, 0, tzinfo=ZoneInfo("UTC")))
        chart, _ = await charts.build(make(env, store), args)
        assert chart.dark is dark


async def test_totals_are_the_models_own_or_the_change_over_the_chart(env, store):
    yahoo = FakeMcp("yahoo", tools=("get_historical_prices",), reply=history_json([100, 120]))
    chart, _ = await charts.build(make(env, store, yahoo), {"title": "t", "series": [
        {"name": "Price", "ticker": "NVDA"},
        {"name": "Change", "ticker": "NVDA", "percent_change": True},
        {"name": "Fund", "x": ["2026-01-01", "2026-01-02"], "y": [5, 6], "total": "+12.4% since inception in 2019"},
        {"name": "P/E", "x": ["2026-01-01", "2026-01-02"], "y": [30, 35]}]})
    assert [s.total for s in chart.series] == ["+20.0%", "+20.0%", "+12.4% since inception", ""]


def test_legend_columns_fit_the_labels_across_the_image():
    assert render.legend_columns(["NBIS -31.1%", "CRWV -50.9%", "IREN -52.5%", "APLD -26.8%", "CORZ -9.3%"]) == 4
    assert render.legend_columns(["A", "B", "C"]) == 3
    assert render.legend_columns(list("abcdefgh")) == render.LEGEND_COLUMNS
    assert render.legend_columns(["x" * 200]) == 1


def test_the_legend_reads_left_to_right_row_by_row():
    chart = Chart("t", [Series(n, DAYS, [1.0, 2.0], kind="bar") for n in "abcdefg"])
    scene = render.scene(chart)
    texts = scene.find("legend")
    assert scene.legend == list("abcdefg")
    rows = sorted({t.y for t in texts})
    assert [t.text for t in texts if t.y == rows[0]] == ["a", "b", "c", "d", "e", "f"]  # six fit across


def test_charts_have_no_frame_and_only_horizontal_rules():
    days = [datetime(2026, 1, d) for d in (1, 2, 3)]
    scene = render.scene(Chart("t", [Series("a", days, [1.0, -1.0, 2.0]), Series("b", days, [5.0, 6.0, 7.0], axis="right")]))
    rules = scene.find("rule")
    assert rules and all(r.points[0][1] == r.points[1][1] for r in rules)  # horizontal only
    assert not scene.find("frame")
    assert len(scene.find("zero")) == 1  # the left axis crosses zero, the right doesn't


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


async def test_percent_change_rebases_and_puts_percent_on_the_ticks(env, store):
    yahoo = FakeMcp("yahoo", tools=("get_historical_prices",), reply=history_json([50, 75], truncated=True))
    ctx = make(env, store, yahoo)
    chart, lines = await charts.build(ctx, {"title": "t", "series": [
        {"name": "NVDA", "ticker": "NVDA", "percent_change": True},
        {"name": "Mine", "x": ["2026-01-01", "2026-01-02"], "y": [10, None], "axis": "right"}]})
    assert chart.series[0].y == [0.0, 50.0] and chart.left_percent and not chart.right_percent
    assert math.isnan(chart.series[1].y[1])
    assert lines[0].endswith("high 50% (newest 2 of 7)") and lines[1].startswith("Mine (right axis, 1 points)")


@pytest.mark.parametrize("args, error", [
    ({"series": [{"name": "a", "x": [1], "y": [1]}]}, "give the chart a title"),
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


async def test_at_most_five_charts_per_reply(env, store):
    made = charts.Charts()
    _, make_chart = charts.tool(make(env, store), made)
    args = {"title": "t", "series": [{"name": "a", "x": [1, 2], "y": [1, 2]}]}
    for _ in range(5):
        assert (await make_chart(args)).startswith("Chart ")
    assert await make_chart(args) == "Error: at most 5 charts per reply."
    assert len(made.photos) == 5 and made.photos[0][1] == "t"


CHART_CALL = [("1", "make_chart", json.dumps({"title": "NVDA, 1 year", "series": [{"name": "NVDA", "ticker": "NVDA"}]}))]


async def test_a_chart_request_gets_one_reply_the_photo_with_the_answer_as_caption(env, store):
    yahoo = FakeMcp("yahoo", tools=("get_historical_prices",), reply=history_json([100, 120]))
    ctx, backend, h = make_ctx(env, store, [step(calls=CHART_CALL, finish="tool_calls"), step("Up <b>20%</b>.")],
                               servers=(yahoo,))
    m = user_msg(ctx.bot, "@stockbot chart NVDA over the last year")
    await run(h, m)
    assert "make_chart" in backend.seen[0]["tools"]
    assert "caption" in backend.results_log[0][0]
    [reply] = m.replies
    assert reply["photo"].startswith(b"\x89PNG") and reply["caption"] == "Up <b>20%</b>."
    assert reply["parse_mode"] == "HTML" and reply["do_quote"] is True
    assert store.history(-100, 20)[-1].text == "[photo] Up <b>20%</b>."


async def test_an_answer_too_long_for_a_caption_continues_in_a_text_message(env, store):
    long = "\n".join(f"Line {i}: " + "x" * 80 for i in range(20))  # about 1,800 characters
    ctx, _, h = make_ctx(env, store, [step(calls=[("1", "make_chart", json.dumps(
        {"title": "t", "series": [{"name": "a", "x": [1, 2], "y": [1, 2]}]}))], finish="tool_calls"), step(long)])
    m = user_msg(ctx.bot, "@stockbot plot it")
    await run(h, m)
    photo, text = m.replies
    assert len(photo["caption"]) <= 1000 and photo["caption"].startswith("Line 0:")
    assert text["text"].startswith("Line ") and (photo["caption"] + "\n" + text["text"]).count("Line ") == 20


async def test_a_caption_with_bad_html_is_resent_as_plain_text(env, store):
    from telegram.error import BadRequest
    ctx, _, h = make_ctx(env, store, [step(calls=[("1", "make_chart", json.dumps(
        {"title": "t", "series": [{"name": "a", "x": [1, 2], "y": [1, 2]}]}))], finish="tool_calls"),
        step("Up <b>a lot</b>.")])
    m = user_msg(ctx.bot, "@stockbot plot it")
    sent = []

    async def picky(photo, **kw):
        if kw.get("parse_mode"):
            raise BadRequest("Can't parse entities: unclosed tag")
        sent.append(kw)
        return ctx.bot._msg(-100, kw["caption"])

    m.reply_photo = picky
    await run(h, m)
    assert [kw["caption"] for kw in sent] == ["Up a lot."] and m.replies == []


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


def callback(sent, data, edited_photo=None):
    """A tap on a button under `sent`, recording the answer and the edit."""
    from types import SimpleNamespace as N
    q = N(message=sent, data=data, answers=[], edits=[])

    async def answer(text=None, **kw):
        q.answers.append(text)

    async def edit_message_media(media, **kw):
        q.edits.append({"media": media, **kw})
        if edited_photo:
            from .conftest import make_msg
            return make_msg(1, None, photo=[N(file_id=edited_photo)])

    q.answer, q.edit_message_media = answer, edit_message_media
    return q


def button_rows(markup):
    return [[(b.text, b.callback_data) for b in row] for row in markup.inline_keyboard]


def shaded(markup):
    """The callback data of the buttons drawn in Telegram's primary (shaded) style."""
    return [b["callback_data"] for row in markup.to_dict()["inline_keyboard"] for b in row if b.get("style") == "primary"]


async def chart_reply(env, store, reply, args):
    yahoo = FakeMcp("yahoo", tools=("get_historical_prices",), reply=reply)
    ctx, _, h = make_ctx(env, store, [step(calls=[("1", "make_chart", json.dumps(args))], finish="tool_calls"),
                                      step("Up 20%.")], servers=(yahoo,))
    m = user_msg(ctx.bot, "@stockbot chart NVDA")
    await run(h, m)
    return ctx, yahoo, m


TICKER_CHART = {"title": "NVDA, 1 year", "series": [{"name": "NVDA", "ticker": "NVDA"}]}


async def test_a_ticker_chart_gets_period_buttons_and_is_remembered(env, store):
    ctx, _, m = await chart_reply(env, store, history_json([100, 120]), TICKER_CHART)
    [reply] = m.replies
    assert button_rows(reply["reply_markup"]) == [[("1D", "ch:1d"), ("1M", "ch:1mo"), ("1Y", "ch:1y"),
                                                   ("5Y", "ch:5y"), ("🔄", "ch:reload")]]
    assert shaded(reply["reply_markup"]) == ["ch:1y"]  # only the period shown
    spec, caption, period = store.chart_get(-100, 1001)
    assert json.loads(spec) == TICKER_CHART and caption == "Up 20%." and period == "1y"


async def test_charts_of_passed_values_get_no_buttons(env, store):
    _, _, m = await chart_reply(env, store, "", {"title": "t", "series": [{"name": "a", "x": [1, 2], "y": [1, 2]}]})
    assert m.replies[0]["reply_markup"] is None and store.chart_get(-100, 1001) is None


async def test_a_period_tap_redraws_without_the_model_and_its_own_period_restores_the_caption(env, store):
    ctx, yahoo, m = await chart_reply(env, store, history_json([100, 120]), TICKER_CHART)
    sent = ctx.bot._msg(-100, None)
    sent.message_id = 1001
    yahoo.reply = history_json([50, 75])
    q = callback(sent, "ch:1mo")
    await buttons.on_button(ctx, N(callback_query=q), None)
    assert yahoo.calls[-1] == ("get_historical_prices",
                               {"ticker": "NVDA", "period": "1mo", "interval": "1d", "response_format": "json"})
    [edit] = q.edits
    assert edit["media"].caption == "NVDA, 1 month" and edit["media"].media.input_file_content.startswith(b"\x89PNG")
    assert shaded(edit["reply_markup"]) == ["ch:1mo"] and store.chart_get(-100, 1001)[2] == "1mo"
    assert len(ctx.backend.seen) == 2  # the model's two steps for the first reply, none since
    q = callback(sent, "ch:reload")
    await buttons.on_button(ctx, N(callback_query=q), None)
    assert yahoo.calls[-1][1]["period"] == "1mo" and q.edits[0]["media"].caption == "NVDA, 1 month"
    q = callback(sent, "ch:1y")
    await buttons.on_button(ctx, N(callback_query=q), None)
    assert q.edits[0]["media"].caption == "Up 20%." and q.answers == [None]


async def test_a_tap_on_a_forgotten_chart_or_a_failed_fetch_says_so(env, store):
    ctx, yahoo, _ = await chart_reply(env, store, history_json([100, 120]), TICKER_CHART)
    unknown = ctx.bot._msg(-100, None)
    q = callback(unknown, "ch:1d")
    await buttons.on_button(ctx, N(callback_query=q), None)
    assert q.answers == ["This chart can't be redrawn any more."] and q.edits == []
    known = ctx.bot._msg(-100, None)
    known.message_id = 1001
    yahoo.reply = "No historical data available for NVDA"
    q = callback(known, "ch:1d")
    await buttons.on_button(ctx, N(callback_query=q), None)
    assert q.answers == ["NVDA: No historical data available for NVDA"] and q.edits == []


async def settle():
    for _ in range(50):
        await asyncio.sleep(0)
    await asyncio.gather(*list(buttons._tasks))


PRELOAD = {"TELEGRAM_PRELOAD_CHAT": "-900"}


async def test_other_periods_are_uploaded_to_the_preload_chat_and_reused_by_the_buttons(env, store):
    ctx, yahoo, m = await chart_reply({**env, **PRELOAD}, store, history_json([100, 120]), TICKER_CHART)
    await settle()
    assert [p["chat_id"] for p in ctx.bot.photos] == [-900] * 3 and len(yahoo.calls) == 4  # the 3 other periods
    assert [p["caption"] for p in ctx.bot.photos] == [
        "NVDA, 1 day", "NVDA, 1 month", "NVDA, 5 years"]
    sent = ctx.bot._msg(-100, None)
    sent.message_id = 1001
    calls_before = len(yahoo.calls)
    q = callback(sent, "ch:1mo")
    await buttons.on_button(ctx, N(callback_query=q), None)
    assert len(yahoo.calls) == calls_before  # nothing fetched or drawn
    [edit] = q.edits
    assert edit["media"].media == "file-2" and edit["media"].caption == "NVDA, 1 month"
    assert shaded(edit["reply_markup"]) == ["ch:1mo"] and store.chart_get(-100, 1001)[2] == "1mo"
    q = callback(sent, "ch:1y")  # back to the chart as first sent
    await buttons.on_button(ctx, N(callback_query=q), None)
    assert q.edits[0]["media"].media == "sent" and q.edits[0]["media"].caption == "Up 20%."
    assert len(yahoo.calls) == calls_before


async def test_reload_and_stale_uploads_draw_again(env, store, monkeypatch):
    ctx, yahoo, m = await chart_reply({**env, **PRELOAD}, store, history_json([100, 120]), TICKER_CHART)
    await settle()
    sent = ctx.bot._msg(-100, None)
    sent.message_id = 1001
    before = len(yahoo.calls)
    await buttons.on_button(ctx, N(callback_query=callback(sent, "ch:reload")), None)
    assert len(yahoo.calls) == before + 1
    monkeypatch.setattr(buttons, "PRELOAD_MAX_AGE", -1)
    q = callback(sent, "ch:1mo")
    await buttons.on_button(ctx, N(callback_query=q), None)
    assert len(yahoo.calls) == before + 2 and not isinstance(q.edits[0]["media"].media, str)


async def test_without_the_variable_nothing_is_uploaded(env, store):
    ctx, yahoo, _ = await chart_reply(env, store, history_json([100, 120]), TICKER_CHART)
    await settle()
    assert ctx.bot.photos == [] and len(yahoo.calls) == 1


async def test_a_preload_chat_the_bot_cannot_post_in_stops_after_one_warning(env, store, caplog):
    from telegram.error import Forbidden
    ctx, _, h = make_ctx({**env, **PRELOAD}, store, [step(calls=[("1", "make_chart", json.dumps(TICKER_CHART))],
                                                          finish="tool_calls"), step("Up.")],
                         servers=(FakeMcp("yahoo", tools=("get_historical_prices",), reply=history_json([1, 2])),))
    ctx.bot.fail_send[-900] = Forbidden("bot was blocked")
    await run(h, user_msg(ctx.bot, "@stockbot chart NVDA"))
    await settle()
    assert [r.getMessage() for r in caplog.records if "preload" in r.getMessage()] == [
        "Chart preload to chat -900 failed: bot was blocked"]


def test_the_preload_chat_must_be_a_number(env):
    assert config.load({**env, "TELEGRAM_PRELOAD_CHAT": " -1001234 "}).preload_chat == -1001234
    assert config.load(env).preload_chat is None
    with pytest.raises(config.ConfigError, match="TELEGRAM_PRELOAD_CHAT must be a Telegram chat ID"):
        config.load({**env, "TELEGRAM_PRELOAD_CHAT": "mychat"})


async def test_an_upload_is_reused_for_an_hour_but_the_1d_chart_for_15_minutes(env, store, monkeypatch):
    ctx, yahoo, _ = await chart_reply({**env, **PRELOAD}, store, history_json([100, 120]), TICKER_CHART)
    await settle()
    sent = ctx.bot._msg(-100, None)
    sent.message_id = 1001
    now = buttons.time.time()
    before = len(yahoo.calls)

    async def tap(period, age):
        monkeypatch.setattr(buttons.time, "time", lambda: now + age)
        await buttons.on_button(ctx, N(callback_query=callback(sent, f"ch:{period}")), None)
        return len(yahoo.calls) - before

    assert await tap("1mo", 59 * 60) == 0 and await tap("1d", 14 * 60) == 0  # both still fresh
    assert await tap("1mo", 61 * 60) == 1  # an hour on: drawn again
    assert await tap("1d", 16 * 60) == 2  # the day chart already after 15 minutes


def test_date_ranges_show_the_year_once():
    d = datetime
    assert charts.date_range(d(2026, 9, 30), d(2026, 10, 28)) == "30 Sep - 28 Oct 2026"
    assert charts.date_range(d(2025, 12, 5), d(2026, 1, 9)) == "5 Dec 2025 - 9 Jan 2026"
    assert charts.date_range(d(2026, 10, 28, 9, 30), d(2026, 10, 28, 16, 0)) == "28 Oct 2026"


async def test_the_subtitle_says_what_the_chart_shows_and_when(env, store):
    yahoo = FakeMcp("yahoo", tools=("get_historical_prices",), reply=history_json([100, 120]))
    ctx = make(env, store, yahoo)
    sub = lambda *series, **kw: charts.build(ctx, {"title": "t", "series": list(series), **kw})  # noqa: E731
    chart, _ = await sub({"name": "A", "ticker": "A", "period": "1mo", "percent_change": True},
                         {"name": "B", "ticker": "B", "period": "1mo", "percent_change": True})
    assert chart.subtitle == "% change over the past month, 1 Jan - 2 Jan 2026"
    chart, _ = await sub({"name": "A", "ticker": "A"})
    assert chart.subtitle == "Closing price over the past year, 1 Jan - 2 Jan 2026"
    chart, _ = await sub({"name": "A", "ticker": "A", "period": "5y"}, {"name": "B", "ticker": "B", "period": "1y"})
    assert chart.subtitle == "1 Jan - 2 Jan 2026"  # periods differ: just the dates
    chart, _ = await sub({"name": "Mine", "x": ["a", "b"], "y": [1, 2]})
    assert chart.subtitle == ""


def test_the_subtitle_sits_under_the_title_and_the_date_is_not_repeated_bottom_right():
    chart = Chart("t", [Series("a", DAYS, [1.0, 2.0])], subtitle="Closing price, 1 Jan - 2 Jan 2026")
    scene = render.scene(chart)
    [subtitle], [title] = scene.find("subtitle"), scene.find("title")
    assert subtitle.text == "Closing price, 1 Jan - 2 Jan 2026" and subtitle.y > title.y
    assert not any("2026" in label for label in scene.x_labels if len(label) > 4)


def test_the_line_at_zero_is_as_light_as_the_other_rules():
    chart = Chart("t", [Series("a", DAYS, [-1.0, 2.0])])
    scene = render.scene(chart)
    [zero] = scene.find("zero")
    assert zero.width == scene.find("rule")[0].width == render.RULE_WIDTH * PT
    assert zero.color == scene.find("rule")[0].color == render.THEMES["light"].rule


async def test_a_total_that_is_not_a_figure_is_dropped_and_computed_ones_win(env, store):
    yahoo = FakeMcp("yahoo", tools=("get_historical_prices",), reply=history_json([100, 120]))
    chart, _ = await charts.build(make(env, store, yahoo), {"title": "t", "series": [
        {"name": "NVDA", "ticker": "NVDA", "total": "NVDA"},  # the model repeated the name
        {"name": "Pct", "ticker": "NVDA", "percent_change": True, "total": "+99%"},
        {"name": "Fund", "x": ["2026-01-01", "2026-01-02"], "y": [5, 6], "total": "Fund"},
        {"name": "Fund 2", "x": ["2026-01-01", "2026-01-02"], "y": [5, 6], "total": "+12.4%"}]})
    assert [s.total for s in chart.series] == ["+20.0%", "+20.0%", "", "+12.4%"]


def pie(name, labels, values, **kw):
    return {"name": name, "kind": "pie", "x": labels, "y": values, **kw}


def test_pie_slices_are_biggest_first_with_gaps_dropped_and_the_smallest_added_up():
    s = Series("p", ["a", "b", "c", "d"], [1.0, 3.0, math.nan, -2.0], kind="pie")
    assert render.pie_slices(s) == [("b", 3.0), ("a", 1.0)]
    many = Series("p", [str(i) for i in range(12)], [float(i + 1) for i in range(12)], kind="pie")
    slices = render.pie_slices(many)
    assert len(slices) == render.PIE_SLICES
    assert slices[0] == ("11", 12.0) and slices[-1][0] == "Other" and sum(y for _, y in slices) == sum(range(1, 13))
    assert [render.share(p, 100.0) for p in (62.43, 0.01, 100.0)] == ["62.4%", "<0.1%", "100.0%"]


def test_a_pie_is_a_donut_with_a_legend_of_shares_and_the_total_in_the_middle():
    chart = Chart("Revenue", [Series("p", ["US", "Taiwan"], [75.0, 25.0], kind="pie", total="$100B")],
                  subtitle="Share of total")
    scene = render.scene(chart)
    wedges = scene.find("wedge")
    assert len(wedges) == 2 and wedges[0].end - wedges[0].start == pytest.approx(270)  # 75% of the way round
    assert scene.texts("total") == ["$100B"] and scene.find("hole")
    assert scene.legend == ["US  75.0%", "Taiwan  25.0%"]
    for dark, bg in ((False, (255, 255, 255)), (True, (0x14, 0x1a, 0x22))):
        picture = Image.open(io.BytesIO(REAL_PNG(replace(chart, dark=dark))))
        assert picture.size == (1224, 714) and picture.getpixel((2, 2))[:3] == bg


async def test_a_pie_call_takes_slices_names_and_shares_for_the_model(env, store):
    _, make_chart = charts.tool(make(env, store), charts.Charts())
    out = await make_chart({"title": "t", "series": [pie("Revenue", ["Compute", "Graphics"], [88299, 7922])]})
    assert out.startswith("Chart 1 is ready") and "Revenue (pie, 2 slices, total 96,221): Compute 91.8%, Graphics 8.2%" in out
    chart, _ = await charts.build(make(env, store), {"title": "t", "series": [pie("R", [2024, 2025], [1, 3])]})
    assert chart.series[0].x == ["2024", "2025"] and chart.subtitle == "Share of total" and not chart.labels


@pytest.mark.parametrize("series, error", [
    ([pie("a", ["x"], [1]), pie("b", ["y"], [1])], "a pie chart is one series"),
    ([{"name": "a", "kind": "pie", "ticker": "NVDA"}], "a pie chart is one series"),
    ([pie("a", ["x", "y"], [0, -1])], "at least one value above zero"),
])
async def test_a_pie_is_one_series_of_values_above_zero(env, store, series, error):
    _, make_chart = charts.tool(make(env, store), charts.Charts())
    out = await make_chart({"title": "t", "series": series})
    assert out.startswith("Error: ") and error in out


def test_breakdown_and_segment_requests_offer_the_chart_tool_too():
    for text in ("segment breakdown NVDA", "NVDA revenue segments", "break down NVDA revenue", "show a pie of it",
                 "plot this", "give me the breakdown by region"):
        assert charts.wants_chart(text), text
    for text in ("how is NVDA doing?", "what does NVDA make", "any news on AMD"):
        assert not charts.wants_chart(text), text


async def test_a_chart_request_ends_with_a_line_telling_the_model_to_draw_it(env, store):
    ctx, backend, h = make_ctx(env, store, [step("ok"), step("ok"), step("ok")],
                               servers=(FakeMcp("yahoo", tools=("get_stock_quote",)),))
    for mid, text in enumerate(("@stockbot NVDA revenue breakdown", "@stockbot chart NVDA", "@stockbot how is NVDA?"), 1):
        await run(h, user_msg(ctx.bot, text, mid=mid))
    pies, plain, none = (seen["prompt"] for seen in backend.seen)
    assert "call make_chart with kind pie once for each grouping" in pies
    assert "call make_chart rather than describing the numbers" in plain and "pie" not in plain.split("Group chat")[-1][-200:]
    assert "make_chart" not in none


def test_the_model_is_told_to_use_the_finest_split_of_a_pie():
    assert "use the sub-lines, the finest split, not the subtotal" in charts.TOOL.description
    assert "finest split with no overlaps" in charts.chart_hint("NVDA revenue breakdown")


def test_a_subtotal_drawn_beside_its_parts_is_found_by_its_sum_and_its_name():
    both = [("Data Center", 89023.0), ("Data Center - Hyperscale", 48710.0),
            ("Data Center - AI Clouds, Industrial, & Enterprise", 40313.0), ("Edge Computing", 7198.0)]
    assert charts.subtotal_in(both) == ("Data Center", ["Data Center - Hyperscale",
                                                         "Data Center - AI Clouds, Industrial, & Enterprise"])
    assert charts.subtotal_in(both[1:]) is None  # the sub-lines and Edge
    assert charts.subtotal_in([both[0], both[3]]) is None  # the subtotal and Edge
    assert charts.subtotal_in([("A", 20.0), ("B", 10.0), ("C", 10.0)]) is None  # a coincidence: no name in common
    assert charts.subtotal_in([(f"s{i}", 1.0) for i in range(20)]) is None  # too many to search


async def test_a_pie_with_a_subtotal_and_its_parts_is_refused_with_the_fix(env, store):
    names = ["Data Center", "Data Center - Hyperscale", "Data Center - AI Clouds", "Edge Computing"]
    made = charts.Charts()
    _, make_chart = charts.tool(make(env, store), made)
    out = await make_chart({"title": "t", "series": [pie("p", names, [89.023, 48.710, 40.313, 7.198])]})
    assert out.startswith("Error: Data Center is a subtotal of Data Center - Hyperscale + Data Center - AI Clouds")
    assert "Draw the sub-lines without Data Center" in out and made.photos == []
    ok = await make_chart({"title": "t", "series": [pie("p", names[1:], [48.710, 40.313, 7.198])]})
    assert ok.startswith("Chart 1 is ready") and "Edge Computing 7.5%" in ok


def test_button_labels_are_the_key_word_of_what_differs_in_each_title():
    labels = pages.labels_for
    assert labels(["NVDA Q2 FY2027 Revenue by Reportable Segments", "NVDA Q2 FY2027 Revenue by Market Platform (finest)",
                   "NVDA Q2 FY2027 Revenue by Customer Headquarters"]) == ["Segments", "Platform", "Headquarters"]
    assert labels(["NVDA price", "AMD price"]) == ["NVDA", "AMD"]
    assert labels(["Sales by US state", "Sales by EU state"]) == ["US", "EU"]  # the words they share are dropped
    assert labels(["Same", "Same"]) == ["Chart 1", "Chart 2"]  # nothing differs
    assert labels(["A", "A B"]) == ["Chart 1", "Chart 2"]  # one title is the other's prefix
    assert labels(["Revenue by Intergovernmental", "Revenue by Segment"]) == ["Intergovernm…", "Segment"]


PIE_CALLS = [("1", "make_chart", json.dumps({"title": f"NVDA revenue by {n}", "series": [pie(n, ["a", "b"], [3, 1])]}))
             for n in ("segment", "platform", "region")]


async def test_several_charts_are_one_image_with_a_button_for_each_not_an_album(env, store):
    ctx, backend, h = make_ctx({**env, **PRELOAD}, store, [step(calls=PIE_CALLS, finish="tool_calls"), step("Mostly a.")])
    m = user_msg(ctx.bot, "@stockbot show NVDA revenue as pies")
    await run(h, m)
    await settle_pages()
    assert backend.seen[0]["tools"] == ["make_chart"]
    [reply] = m.replies
    assert reply["photo"].startswith(b"\x89PNG") and reply["caption"] == "Mostly a." and "album" not in reply
    assert [(b.text, b.callback_data) for b in reply["reply_markup"].inline_keyboard[0]] == [
        ("Segment", "cp:0"), ("Platform", "cp:1"), ("Region", "cp:2")]
    assert shaded(reply["reply_markup"]) == ["cp:0"]
    assert store.history(-100, 20)[-1].text == "[photo] Mostly a."
    assert [p["caption"] for p in ctx.bot.photos] == ["Platform", "Region"] and ctx.bot.photos[0]["chat_id"] == -900


async def settle_pages():
    for _ in range(50):
        await asyncio.sleep(0)
    await asyncio.gather(*list(pages._tasks))


async def test_a_button_shows_that_chart_in_place_from_its_upload_or_drawn_once_and_remembered(env, store):
    ctx, _, h = make_ctx({**env, **PRELOAD}, store, [step(calls=PIE_CALLS, finish="tool_calls"), step("Mostly a.")])
    await run(h, user_msg(ctx.bot, "@stockbot show NVDA revenue as pies"))
    await settle_pages()
    sent = ctx.bot._msg(-100, None)
    sent.message_id = 1001
    q = callback(sent, "cp:1")
    await pages.on_page(ctx, N(callback_query=q), None)
    [edit] = q.edits
    assert edit["media"].media == "file-1" and edit["media"].caption == "Mostly a."  # the upload, the answer kept
    assert shaded(edit["reply_markup"]) == ["cp:1"] and store.chart_get(-100, 1001)[2] == "#1"
    q = callback(sent, "cp:1")  # already shown
    await pages.on_page(ctx, N(callback_query=q), None)
    assert q.edits == [] and q.answers == [None]
    q = callback(sent, "cp:0")  # the picture first sent
    await pages.on_page(ctx, N(callback_query=q), None)
    assert q.edits[0]["media"].media == "sent"


async def test_without_the_preload_chat_a_chart_is_drawn_on_the_first_tap_then_reused(env, store):
    ctx, _, h = make_ctx(env, store, [step(calls=PIE_CALLS, finish="tool_calls"), step("Mostly a.")])
    await run(h, user_msg(ctx.bot, "@stockbot show NVDA revenue as pies"))
    assert ctx.bot.photos == []
    sent = ctx.bot._msg(-100, None)
    sent.message_id = 1001
    q = callback(sent, "cp:2", edited_photo="edited-2")
    await pages.on_page(ctx, N(callback_query=q), None)
    assert q.edits[0]["media"].media.input_file_content.startswith(b"\x89PNG")  # drawn from its call
    q = callback(sent, "cp:0")
    await pages.on_page(ctx, N(callback_query=q), None)
    q = callback(sent, "cp:2")
    await pages.on_page(ctx, N(callback_query=q), None)
    assert q.edits[0]["media"].media == "edited-2"  # now an upload


async def test_buttons_for_charts_that_are_gone_say_so(env, store):
    ctx, _, _ = make_ctx(env, store, [])
    sent = ctx.bot._msg(-100, None)
    q = callback(sent, "cp:1")
    await pages.on_page(ctx, N(callback_query=q), None)
    assert q.answers == ["These charts can't be shown any more."] and q.edits == []


async def test_charts_keep_the_order_they_were_asked_for_whichever_finishes_drawing_first(env, store, monkeypatch):
    drawn = []

    def slow_first(chart):  # the first chart takes the longest to draw
        import time
        time.sleep(0.15 if chart.title == "first" else 0.0)
        drawn.append(chart.title)
        return b"\x89PNG fake"

    monkeypatch.setattr(render, "png", slow_first)
    made = charts.Charts()
    _, make_chart = charts.tool(make(env, store), made)
    one = lambda t: {"title": t, "series": [{"name": "a", "x": [1, 2], "y": [1, 2]}]}  # noqa: E731
    results = await asyncio.gather(make_chart(one("first")), make_chart(one("second")), make_chart(one("third")))
    assert drawn[0] != "first" and [t for _, t in made.photos] == ["first", "second", "third"]
    assert [r.split(" is ready")[0] for r in results] == ["Chart 1", "Chart 2", "Chart 3"]


async def test_a_failed_chart_gives_up_its_place_and_its_number(env, store):
    made = charts.Charts()
    _, make_chart = charts.tool(make(env, store), made)
    bad = {"title": "t", "series": [{"name": "a", "x": [1], "y": [1, 2]}]}
    good = {"title": "t", "series": [{"name": "a", "x": [1, 2], "y": [1, 2]}]}
    assert (await make_chart(bad)).startswith("Error: ")
    assert [(await make_chart(good)).split(" is ready")[0] for _ in range(5)] == [f"Chart {i}" for i in range(1, 6)]
    assert (await make_chart(good)) == "Error: at most 5 charts per reply."


def test_a_breakdown_of_names_and_values_drawn_as_the_default_line_becomes_a_pie():
    line = {"title": "t", "series": [{"name": "p", "x": ["Compute", "Graphics"], "y": [88, 8]}]}
    pie_of = lambda args, prefer=True: charts.pie_default(args, prefer)["series"][0].get("kind")  # noqa: E731
    assert pie_of(line) == "pie"
    assert pie_of(line, prefer=False) is None  # not asked for a breakdown
    assert pie_of({**line, "series": [{**line["series"][0], "kind": "line"}]}) == "pie"
    assert pie_of({**line, "series": [{**line["series"][0], "kind": "bar"}]}) == "bar"  # the model's own choice
    assert pie_of({**line, "series": [{**line["series"][0], "x": ["2024-01-01", "2024-02-01"]}]}) is None  # dates
    assert pie_of({**line, "series": [{**line["series"][0], "x": [2024, 2025]}]}) is None  # numbers
    assert pie_of({**line, "series": [{"name": "p", "ticker": "NVDA"}]}) is None
    two = {**line, "series": line["series"] * 2}
    assert charts.pie_default(two, True) is two


async def test_asked_for_a_breakdown_a_line_of_names_and_values_is_drawn_as_a_donut(env, store):
    call = {"title": "By platform", "series": [{"name": "Platform", "x": ["Hyperscale", "AI Clouds", "Edge"],
                                                "y": [48.7, 40.3, 7.2]}]}
    ctx, backend, h = make_ctx(env, store, [step(calls=[("1", "make_chart", json.dumps(call))], finish="tool_calls"),
                                           step("ok")])
    await run(h, user_msg(ctx.bot, "@stockbot NVDA revenue breakdown"))
    assert "(pie, 3 slices" in backend.results_log[0][0]  # drawn as a pie, though the model left the kind out
    ctx, backend, h = make_ctx(env, store, [step(calls=[("1", "make_chart", json.dumps(call))], finish="tool_calls"),
                                           step("ok")])
    await run(h, user_msg(ctx.bot, "@stockbot chart NVDA revenue", mid=2))
    assert "(pie" not in backend.results_log[0][0]  # not a breakdown request: the model's line stays


def test_a_breakdown_drawn_as_one_series_per_slice_is_one_pie():
    dots = {"title": "NVDA revenue by segment", "series": [
        {"name": "Compute & Networking", "x": ["Compute & Networking"], "y": [88.3]},
        {"name": "Graphics", "x": ["Graphics"], "y": [7.9], "kind": "pie"}]}
    [merged] = charts.pie_default(dots, True)["series"]
    assert merged["kind"] == "pie" and merged["x"] == ["Compute & Networking", "Graphics"] and merged["y"] == [88.3, 7.9]
    assert charts.pie_default(dots, False) is dots  # not a breakdown request
    many = {"title": "t", "series": [{"name": "a", "x": [1, 2], "y": [1, 2]}, {"name": "b", "x": [1, 2], "y": [1, 2]}]}
    assert charts.pie_default(many, True) is many  # series of several points are lines
    bars = {"title": "t", "series": [{"name": "a", "x": ["q"], "y": [1], "kind": "bar"}, {"name": "b", "x": ["q"], "y": [2]}]}
    assert charts.pie_default(bars, True) is bars  # a kind the model chose


async def test_asked_for_a_breakdown_dots_named_at_their_ends_become_a_donut(env, store):
    call = {"title": "By segment", "series": [{"name": "Compute", "x": ["Compute"], "y": [88.3]},
                                              {"name": "Graphics", "x": ["Graphics"], "y": [7.9]}]}
    ctx, backend, h = make_ctx(env, store, [step(calls=[("1", "make_chart", json.dumps(call))], finish="tool_calls"),
                                           step("ok")])
    await run(h, user_msg(ctx.bot, "@stockbot NVDA revenue breakdown"))
    assert "(pie, 2 slices, total 96.2): Compute 91.8%, Graphics 8.2%" in backend.results_log[0][0]


async def test_series_sent_as_a_json_string_is_read(env, store):
    _, make_chart = charts.tool(make(env, store), charts.Charts())
    out = await make_chart({"title": "Split", "series": '[{"name": "S", "kind": "pie", "x": ["A", "B"], "y": [1, 2]}]'})
    assert out.startswith("Chart 1 is ready")


async def test_series_that_is_not_a_list_of_objects_says_how_it_should_look(env, store):
    _, make_chart = charts.tool(make(env, store), charts.Charts())
    out = await make_chart({"title": "Split", "series": "not json at all"})
    assert out.startswith("Error: series must be a list of objects")


async def test_one_series_object_with_x_values_and_y_values_is_a_pie(env, store):
    _, make_chart = charts.tool(make(env, store), charts.Charts())
    out = await make_chart({"title": "Split", "series": {"name": "S", "kind": "pie", "x_values": ["A", "B"],
                                                         "y_values": [1, 2], "x_values2": None}})
    assert out.startswith("Chart 1 is ready")


@pytest.mark.parametrize("title, expected", [
    ("Tesla revenue by product line ($B)", "$28.1B"),
    ("Revenue (€ million)", "€28.1M"),
    ("Revenue by segment", ""),
])
async def test_a_pie_shows_its_sum_in_the_middle_in_the_titles_unit(env, store, title, expected):
    spec = {"name": "S", "kind": "pie", "x": ["A", "B"], "y": [20.094, 8.0]}
    chart, _ = await charts.build(make(env, store), {"title": title, "series": [spec]})
    assert chart.series[0].total == expected


async def test_a_pie_asks_for_a_one_line_caption(env, store):
    _, make_chart = charts.tool(make(env, store), charts.Charts())
    out = await make_chart({"title": "Split ($B)", "series": [{"name": "S", "kind": "pie", "x": ["A", "B"], "y": [1, 2]}]})
    assert "one line under 120 characters" in out
    assert "one line under 120 characters" in charts.chart_hint("TSLA revenue breakdown")


def test_five_chart_buttons_make_two_rows():
    from lib.features.charts import pages
    rows = pages.keyboard(["A", "B", "C", "D", "E"], 0).inline_keyboard
    assert [len(r) for r in rows] == [3, 2]


def test_number_ticks_are_round_steps_inside_the_range():
    from lib.features.charts.ticks import nice_ticks
    assert nice_ticks(-24.0, 56.0) == [-20, -10, 0, 10, 20, 30, 40, 50]
    assert nice_ticks(44.2, 60.8) == [46, 48, 50, 52, 54, 56, 58, 60]
    assert nice_ticks(0.62, 0.71) == [0.62, 0.64, 0.66, 0.68, 0.7]
    assert nice_ticks(2020.9, 2025.1, integer=True) == [2021, 2022, 2023, 2024, 2025]  # years, never 2021.5
    assert nice_ticks(0.0, 0.4, integer=True) == [0]
    assert nice_ticks(3.0, 3.0) == [3.0]


@pytest.mark.parametrize("start, end, labels", [
    (datetime(2026, 10, 30, 9, 30), datetime(2026, 10, 30, 16, 0),
     ["10:00", "11:00", "12:00", "13:00", "14:00", "15:00", "16:00"]),
    (datetime(2026, 10, 29, 18, 0), datetime(2026, 10, 30, 12, 0), ["18:00", "21:00", "30 Oct", "03:00", "06:00", "09:00", "12:00"]),
    (datetime(2026, 1, 3), datetime(2026, 1, 31), ["8", "15", "22"]),  # not 29: too near the next month's start
    (datetime(2025, 10, 31), datetime(2026, 10, 30), ["Nov", "2026", "Mar", "May", "Jul", "Sep"]),
    (datetime(2021, 11, 1), datetime(2026, 10, 30), ["2022", "2023", "2024", "2025", "2026"]),
])
def test_date_ticks_pick_a_unit_for_the_span_and_name_the_bigger_one_at_its_boundary(start, end, labels):
    from lib.features.charts.ticks import date_ticks
    assert [label for _, label in date_ticks(start, end)] == labels
