import asyncio
import json
import math
from datetime import date, datetime
from types import SimpleNamespace as N

import pytest

from lib import config
from lib.context import Ctx
from lib.features import charts
from lib.features.charts import buttons, render
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


def test_tick_labels_share_their_decimals_and_shorten_big_numbers():
    label = render.tick_label
    assert [label(v, [0.58, 0.6, 0.62], False) for v in (0.58, 0.6)] == ["0.58", "0.60"]
    assert [label(v, [2600, 2800, 3000], False) for v in (2600, 3000)] == ["2,600", "3,000"]
    assert [label(v, [1e8, 1.5e8, 2e8], False) for v in (1e8, 1.5e8)] == ["100M", "150M"]
    assert label(1.25e9, [1e9, 1.25e9, 1.5e9], False) == "1.25B"
    assert [label(v, [-10, 0, 10], True) for v in (-10, -1e-15, 10)] == ["-10%", "0%", "10%"]


def test_totals_go_in_the_legend_even_for_one_series():
    days = [datetime(2026, 1, d) for d in (1, 2)]
    legend = lambda chart: [t.get_text() for t in render.figure(chart).axes[0].get_legend().get_texts()]  # noqa: E731
    assert legend(Chart("t", [Series("NBIS", days, [0.0, -27.1], total="-27.1%"),
                              Series("Gold", days, [1.0, 2.0], axis="right", total="+100.0%"),
                              Series("Mine", days, [1.0, 2.0])])) == ["NBIS -27.1%", "Mine", "Gold +100.0% (right)"]
    assert legend(Chart("t", [Series("NVDA", days, [1.0, 2.0], total="+100.0%")])) == ["NVDA +100.0%"]
    assert render.figure(Chart("t", [Series("NVDA", days, [1.0, 2.0])])).axes[0].get_legend() is None


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


def test_the_legend_reads_left_to_right():
    assert render.legend_order(list("abcdefg"), 4) == list("aebfcgd")  # rows: a b c d / e f g
    assert render.legend_order(list("abc"), 3) == list("abc")


def test_charts_have_no_frame_and_only_horizontal_rules():
    days = [datetime(2026, 1, d) for d in (1, 2, 3)]
    fig = render.figure(Chart("t", [Series("a", days, [1.0, -1.0, 2.0]), Series("b", days, [5.0, 6.0, 7.0], axis="right")]))
    left, right = fig.axes
    assert not any(sp.get_visible() for ax in (left, right) for sp in ax.spines.values())
    assert any(g.get_visible() for g in left.yaxis.get_gridlines())
    assert not any(g.get_visible() for g in left.xaxis.get_gridlines())
    assert [line.get_ydata()[0] for line in left.get_lines()][-1] == 0  # a zero line when the data crosses zero


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


async def test_several_charts_go_as_one_album_captioned_once(env, store):
    call = [("1", "make_chart", json.dumps({"title": "a", "series": [{"name": "a", "x": [1, 2], "y": [1, 2]}]})),
            ("2", "make_chart", json.dumps({"title": "b", "series": [{"name": "b", "x": [1, 2], "y": [2, 1]}]}))]
    ctx, _, h = make_ctx(env, store, [step(calls=call, finish="tool_calls"), step("Both.")])
    m = user_msg(ctx.bot, "@stockbot plot a and b")
    await run(h, m)
    [reply] = m.replies
    assert [caption for _, caption in reply["album"]] == ["Both.", None] and reply["do_quote"] is True
    assert [r.text for r in store.history(-100, 20)][-2:] == ["[photo] Both.", "[photo]"]


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


def callback(sent, data):
    """A tap on a button under `sent`, recording the answer and the edit."""
    from types import SimpleNamespace as N
    q = N(message=sent, data=data, answers=[], edits=[])

    async def answer(text=None, **kw):
        q.answers.append(text)

    async def edit_message_media(media, **kw):
        q.edits.append({"media": media, **kw})

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
