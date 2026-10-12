import io
import json

import pytest

from lib import config
from lib.context import Ctx
from lib.features import charts
from lib.features.charts import render

from .conftest import FakeBot
from .fakes import FakeMcp, ScriptedBackend, registry

PNG = b"\x89PNG\r\n\x1a\n"
SECTORS = [{"label": "Semis", "value": 520.0}, {"label": "ETFs", "value": 250.0}, {"label": "Software", "value": 150.0},
           {"label": "Health", "value": 12.0}, {"label": "REA", "value": 10.0}]


def size(png: bytes) -> tuple[int, int]:
    from PIL import Image
    return Image.open(io.BytesIO(png)).size


def test_a_pie_is_a_png_and_small_slices_merge_into_other():
    png = render.draw("pie", "Sectors", SECTORS, "total 942")
    assert png.startswith(PNG) and size(png)[0] == render.WIDTH and len(png) < 150_000
    slices = render.pie_slices([(i["label"], i["value"]) for i in SECTORS])
    assert [s[0] for s in slices] == ["Semis", "ETFs", "Software", "Other"]  # Health and REA are under 2%
    assert slices[-1][1] == 22.0 and slices[-1][3] and abs(sum(s[2] for s in slices) - 1) < 1e-9


def test_more_than_ten_slices_fold_the_rest_into_other():
    items = [(f"s{i}", 100 - i) for i in range(15)]
    slices = render.pie_slices(items)
    assert len(slices) == render.MAX_SLICES + 1 and slices[-1][0] == "Other"


def test_a_bar_chart_takes_negatives_and_grows_with_the_rows():
    few = render.draw("bar", "Gain", [{"label": "Jan", "value": -5}, {"label": "Feb", "value": 12}])
    many = render.draw("bar", "Gain", [{"label": str(i), "value": i - 10} for i in range(25)])
    assert few.startswith(PNG) and many.startswith(PNG) and size(many)[1] > size(few)[1]


@pytest.mark.parametrize("kind, items, message", [
    ("donut", SECTORS, "kind must be"),
    ("pie", [], "list of items"),
    ("pie", "x", "list of items"),
    ("pie", [{"label": "a"}], "label and a numeric value"),
    ("pie", [{"label": "a", "value": "x"}], "numeric value"),
    ("pie", [{"label": "a", "value": float("nan")}], "isn't a number"),
    ("pie", [{"label": "a", "value": -1}, {"label": "b", "value": 0}], "positive value"),
    ("bar", [{"label": str(i), "value": 1} for i in range(31)], "too many"),
])
def test_bad_data_is_refused_with_a_clear_reason(kind, items, message):
    with pytest.raises(render.ChartError, match=message):
        render.draw(kind, "t", items)


def test_short_values_and_long_labels():
    assert [render.short(v) for v in (2_500_000, 173_600, 12, 3.2, -4_000)] == ["2.5M", "173.6k", "12", "3.2", "-4.0k"]
    label = render.clean("pie", [{"label": "x" * 60, "value": 1}])[0][0]
    assert len(label) == render.LABEL_CHARS and label.endswith("…")


async def test_the_tool_queues_the_picture_for_the_reply_and_sends_nothing_itself(env, store):
    bot, images = FakeBot(), []
    ctx = Ctx(config.load(env), store, ScriptedBackend([]), registry(), bot)
    defn, send = charts.tool(ctx, images)
    out = await send({"kind": "pie", "title": "Sectors", "items": SECTORS})
    assert defn.name == "send_chart" and out == charts.READY and "becomes its caption" in out
    assert len(images) == 1 and images[0].startswith(PNG)
    assert bot.photos == [] and bot.sent == []  # the handler sends it, with the answer as the caption
    assert "caption" not in defn.parameters["properties"]


async def test_the_tool_returns_errors_as_text_so_the_model_can_fall_back(env, store, monkeypatch):
    bot, images = FakeBot(), []
    ctx = Ctx(config.load(env), store, ScriptedBackend([]), registry(), bot)
    _, send = charts.tool(ctx, images)
    assert (await send({"kind": "pie", "title": "t", "items": []})).startswith("Error: give a list of items")

    def explode(*a, **kw):
        raise RuntimeError("font missing")

    monkeypatch.setattr(render, "draw", explode)
    assert "answer in text instead" in await send({"kind": "pie", "title": "t", "items": SECTORS})
    assert images == []


HOLDINGS = {"columns": ["code", "market", "name", "sector", "industry", "value", "quantity"], "rows": [
    ["CRWD", "NASDAQ", "Crowdstrike", "Technology Services", "Packaged Software", 90.0, 1],
    ["S", "NYSE", "SentinelOne", "Technology Services", "Packaged Software", 10.0, 1],
    ["NVDA", "NASDAQ", "Nvidia", "Electronic Technology", "Semiconductors", 170.0, 1],
    ["TSM", "NYSE", "TSMC", "Electronic Technology", "Semiconductors", 50.0, 1],
    ["SMH", "NASDAQ", "VanEck Semi", None, "Investment Trusts/Mutual Funds", 80.0, 1],
    ["OLD", "ASX", "Sold", "Health", "Biotech", 0.0, 0]]}


class Sharesight(FakeMcp):
    async def call(self, tool, args):
        self.calls.append((tool, args))
        if tool == "list_portfolios":
            return json.dumps({"portfolios": [{"id": 7, "name": "Main"}]})
        return json.dumps({"report": {"currency": "AUD", "holdings": HOLDINGS,
                                      "cash_accounts": [{"name": "Cash", "value": 25.0, "currency": "AUD"}]}})


def portfolio_ctx(env, store):
    bot = FakeBot()
    ss = Sharesight("sharesight", tools=("list_portfolios", "get_performance_report"))
    return Ctx(config.load(env), store, ScriptedBackend([]), registry(ss), bot), bot, ss


def test_holdings_are_grouped_by_exactly_the_field_asked_for():
    rows = [dict(zip(HOLDINGS["columns"], r, strict=True)) for r in HOLDINGS["rows"]]
    assert charts.group_holdings(rows, "sector") == [("Electronic Technology", 220.0), ("Technology Services", 100.0),
                                                     ("Unclassified", 80.0)]  # the sold holding (value 0) is skipped
    assert charts.group_holdings(rows, "industry")[0] == ("Semiconductors", 220.0)
    assert charts.group_holdings(rows, "industry")[1] == ("Packaged Software", 100.0)
    assert charts.group_holdings(rows, "holding")[0] == ("NVDA", 170.0)


async def test_portfolio_chart_groups_by_sector_not_industry_and_names_the_portfolio(env, store, monkeypatch):
    ctx, bot, ss = portfolio_ctx(env, store)
    drawn = {}
    real = render.draw

    def spy(kind, title, items, subtitle=""):
        drawn.update(kind=kind, title=title, items=items, subtitle=subtitle)
        return real(kind, title, items, subtitle)

    monkeypatch.setattr(render, "draw", spy)
    images = []
    defn, send = charts.portfolio_tool(ctx, images)
    out = await send({"portfolio_id": 7, "by": "sector"})
    assert defn.name == "portfolio_chart" and "(sector: Electronic Technology 55.0%" in out and out.startswith(charts.READY)
    assert drawn["title"] == "Main by sector" and drawn["kind"] == "pie"
    assert [i["label"] for i in drawn["items"]] == ["Electronic Technology", "Technology Services", "Unclassified"]
    assert "AUD 400 in holdings, 25 cash" in drawn["subtitle"]
    assert len(images) == 1 and bot.photos == []
    assert ss.calls[0][1]["start_date"] == ss.calls[0][1]["end_date"] and "_keep_sales" not in ss.calls[0][1]  # current holdings


async def test_portfolio_chart_refuses_bad_input_as_text(env, store):
    ctx, bot, _ = portfolio_ctx(env, store)
    _, send = charts.portfolio_tool(ctx, [])
    assert "by must be one of" in await send({"portfolio_id": 7, "by": "colour"})
    assert "portfolio_id" in await send({"by": "sector"})
    none_ctx = Ctx(config.load(env), store, ScriptedBackend([]), registry(), bot)
    assert "isn't connected" in await charts.portfolio_tool(none_ctx, [])[1]({"portfolio_id": 7, "by": "sector"})
    assert bot.photos == []
