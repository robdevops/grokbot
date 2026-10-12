import io

import pytest
from telegram.error import TelegramError

from lib import config
from lib.context import Ctx
from lib.features import charts
from lib.features.charts import render

from .conftest import FakeBot, user_msg
from .fakes import ScriptedBackend, registry

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


async def test_the_tool_sends_one_photo_as_a_reply_and_saves_it(env, store):
    bot = FakeBot()
    ctx = Ctx(config.load(env), store, ScriptedBackend([]), registry(), bot)
    msg = user_msg(bot, "@stockbot pie chart of my sectors", mid=7)
    defn, send = charts.tool(ctx, msg)
    out = await send({"kind": "pie", "title": "Sectors", "items": SECTORS, "caption": "A <b>test</b> & more"})
    assert defn.name == "send_chart" and out.startswith("Chart sent")
    photo = bot.photos[0]
    assert photo["bytes"].startswith(PNG) and photo["reply_to_message_id"] == 7 and photo["chat_id"] == -100
    assert photo["caption"] == "A &lt;b&gt;test&lt;/b&gt; &amp; more" and photo["disable_notification"] is True
    assert any("A &lt;b&gt;" in r.text for r in store.history(-100, 20))  # the sent message is in history


async def test_the_tool_returns_errors_as_text_so_the_model_can_fall_back(env, store, monkeypatch):
    bot = FakeBot()
    ctx = Ctx(config.load(env), store, ScriptedBackend([]), registry(), bot)
    _, send = charts.tool(ctx, user_msg(bot, "chart", mid=1))
    assert (await send({"kind": "pie", "title": "t", "items": []})).startswith("Error: give a list of items")
    assert bot.photos == []

    async def broken(*a, **kw):
        raise TelegramError("Bad Request: not enough rights")

    monkeypatch.setattr(bot, "send_photo", broken)
    assert "couldn't send the chart" in await send({"kind": "bar", "title": "t", "items": SECTORS})

    def explode(*a, **kw):
        raise RuntimeError("font missing")

    monkeypatch.setattr(render, "draw", explode)
    assert "answer in text instead" in await send({"kind": "pie", "title": "t", "items": SECTORS})
