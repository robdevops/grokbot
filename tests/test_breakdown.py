import json
from pathlib import Path

import pytest

from lib.features import charts
from lib.features.charts import breakdown

from .conftest import user_msg
from .fakes import FakeMcp, step
from .test_charts import make
from .test_handlers import make_ctx, run

NVDA = (Path(__file__).parent / "fixtures" / "tickerscout" / "get_segments__NVDA.json").read_text()


def scout(reply=NVDA):
    return FakeMcp("ticker-scout", tools=("get_segments",), reply=reply)


@pytest.mark.parametrize("text, ticker", [
    ("TSLA revenue breakdown", "TSLA"),
    ("@stockbot nvda revenue breakdown", "NVDA"),
    ("segments for $AMD please", "AMD"),
    ("BRK-B breakdown", "BRK-B"),
    ("what is the revenue breakdown of the iPhone", None),  # no ticker, or more than one candidate
    ("NVDA vs AMD revenue breakdown", None),
    ("NVDA revenue breakdown in Q1 FY2025", None),  # a period needs the model
    ("how is NVDA doing", None),
])
def test_only_a_plain_breakdown_request_takes_the_fast_path(text, ticker):
    assert breakdown.requested_ticker(text) == ticker


def test_groupings_are_the_segments_and_each_breakdown_without_double_counting():
    period = json.loads(NVDA)["periods"][0]
    found = dict(breakdown.groupings(period))
    assert list(found) == ["segment", "market platform", "customer headquarters"]
    assert [n for n, _ in found["segment"]] == ["Compute & Networking", "Graphics"]  # the operating-income total is no slice
    assert "Data Center" not in [n for n, _ in found["market platform"]]  # the subtotal of its two sub-lines


async def test_fast_path_draws_every_pie_and_gives_a_one_line_caption(env, store):
    ctx = make(env, store, scout())
    made = charts.Charts()
    caption = await breakdown.fast(ctx, "NVDA revenue breakdown", made)
    assert [title for _, title in made.photos] == [
        "NVDA Q2 FY2027 revenue by segment ($B)", "NVDA Q2 FY2027 revenue by market platform ($B)",
        "NVDA Q2 FY2027 revenue by customer headquarters ($B)"]
    assert caption == "<b>NVDA</b> Q2 FY2027 revenue $96.2B: Compute & Networking is 92% of the segment split.".replace("&", "&amp;")
    assert ctx.registry.servers["ticker-scout"].calls == [
        ("get_segments", {"ticker": "NVDA", "breakdown": "all", "period": "latest quarter"})]


async def test_the_handler_answers_without_calling_the_model(env, store):
    ctx, backend, h = make_ctx(env, store, [], servers=(scout(),))
    m = user_msg(ctx.bot, "@stockbot NVDA revenue breakdown")
    await run(h, m)
    assert backend.seen == []
    [reply] = m.replies
    assert reply["photo"].startswith(b"\x89PNG") and reply["caption"].startswith("<b>NVDA</b> Q2 FY2027 revenue $96.2B")
    assert [b.text for row in reply["reply_markup"].inline_keyboard for b in row] == ["Segment", "Platform", "Headquarters"]


@pytest.mark.parametrize("servers", [(), ("not json",), (json.dumps({"error": "unknown ticker"}),)])
async def test_anything_unusable_is_left_to_the_model(env, store, servers):
    ctx, backend, h = make_ctx(env, store, [step("from the model")], servers=(scout(servers[0]),) if servers else ())
    m = user_msg(ctx.bot, "@stockbot NVDA revenue breakdown")
    await run(h, m)
    assert len(backend.seen) == 1 and m.replies[0]["text"] == "from the model"
