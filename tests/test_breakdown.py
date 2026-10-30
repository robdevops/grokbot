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
    assert [a["breakdown"] for _, a in ctx.registry.servers["ticker-scout"].calls] == [
        "all", "revenue_by_product", "revenue_by_geography"]  # `showing` lists two the data has under other names


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


async def test_revenuepie_command_replies_with_the_pies_and_logs_the_message(env, store):
    from types import SimpleNamespace as N

    from .test_handlers import update_for
    ctx, backend, h = make_ctx(env, store, [], servers=(scout(),))
    m = user_msg(ctx.bot, "/revenuepie $nvda", chat_id=-100, mid=3)
    await h.revenuepie(update_for(m), N(args=["$nvda"]))
    assert backend.seen == []
    [reply] = m.replies
    assert reply["caption"].startswith("<b>NVDA</b> Q2 FY2027 revenue $96.2B") and reply["photo"].startswith(b"\x89PNG")
    assert store.history(-100, 20)[0].text == "/revenuepie $nvda"


@pytest.mark.parametrize("args, said", [([], "Usage: /revenuepie TICKER"), (["zzzz"], "No revenue breakdown found for ZZZZ.")])
async def test_revenuepie_without_a_ticker_or_data_says_so(env, store, args, said):
    from types import SimpleNamespace as N

    from .test_handlers import update_for
    ctx, backend, h = make_ctx(env, store, [], servers=(scout(json.dumps({"error": "unknown"})),))
    m = user_msg(ctx.bot, "/revenuepie " + " ".join(args))
    await h.revenuepie(update_for(m), N(args=args))
    assert m.replies[0]["text"] == said


async def test_figures_in_millions_are_scaled_and_a_result_that_cannot_be_used_is_logged(env, store, caplog):
    tesla = {"units": "USD millions", "reporting_currency": "USD", "periods": [{"period": "Q3 FY2026", "revenue_by_product": {
        "Automotive sales": 20094, "Services & other": 4605, "Energy generation & storage": 2881}}]}
    made = charts.Charts()
    caption = await breakdown.fast(make(env, store, scout(json.dumps(tesla))), "TSLA revenue breakdown", made)
    assert caption.startswith("<b>TSLA</b> Q3 FY2026 revenue $27.6B") and [t for _, t in made.photos] == [
        "TSLA Q3 FY2026 revenue by product ($B)"]
    odd = {**tesla, "units": "furlongs"}
    with caplog.at_level("WARNING", logger="bot"):
        assert await breakdown.fast(make(env, store, scout(json.dumps(odd))), "TSLA revenue breakdown", charts.Charts()) is None
    assert "units 'furlongs' not understood" in caplog.text


@pytest.mark.parametrize("detail, labels", [
    ({"revenue_by_product": {"Automotive sales": 20094, "Services & other": 4605},
      "revenue_by_geography": {"United States": 12495, "China": 5209}}, ["product", "geography"]),
    ({"segments": {"Automotive": {"revenue": 21000, "operating_income": 2000},
                   "Energy": {"revenue": 2881, "operating_income": 500}}}, ["segment"]),
    ({"revenue": {"Automotive": 21000, "Energy": 2881}, "operating_income": {"Automotive": 2000, "Energy": 500}}, ["revenue"]),
    ({"by_product": [{"name": "Automotive", "revenue": 21000}, {"name": "Energy", "revenue": 2881}]}, ["by product"]),
])
def test_groupings_are_found_wherever_the_source_nests_them(detail, labels):
    period = {"period": "Q3 FY2026", "fiscal_year": 2026, "fiscal_quarter": 3, "months": 3, "segment_detail": detail}
    assert [label for label, _ in breakdown.groupings(period, 1e6)] == labels


async def test_breakdowns_listed_in_showing_but_missing_from_all_are_fetched_one_by_one(env, store):
    def result(**kw):
        return {"units": "USD millions", "reporting_currency": "USD", "periods": [{"period": "Q3 FY2026", **kw}]}

    answers = {
        "all": result(segment_detail={"segments": {"Automotive": {"revenue": 21000}, "Energy": {"revenue": 2881}}},
                      ) | {"showing": ["segments", "revenue_by_product", "revenue_by_geography"]},
        "revenue_by_product": result(revenue_by_product={"Automotive sales": 20094, "Services & other": 4605}),
        "revenue_by_geography": result(revenue_by_geography={"United States": 12495, "China": 5209}),
    }

    class Scout(FakeMcp):
        async def call(self, tool, args):
            self.calls.append((tool, args))
            return json.dumps(answers[args["breakdown"]])

    server = Scout("ticker-scout", tools=("get_segments",))
    made = charts.Charts()
    await breakdown.fast(make(env, store, server), "TSLA revenue breakdown", made)
    assert [t for _, t in made.photos] == ["TSLA Q3 FY2026 revenue by segment ($B)",
                                           "TSLA Q3 FY2026 revenue by product ($B)",
                                           "TSLA Q3 FY2026 revenue by geography ($B)"]
    assert [a["breakdown"] for _, a in server.calls] == ["all", "revenue_by_product", "revenue_by_geography"]


@pytest.mark.parametrize("key, name", [
    ("automotive_segment", "Automotive"),
    ("energy_generation_and_storage_segment", "Energy generation and storage"),
    ("services_and_other", "Services and other"),
    ("Compute & Networking", "Compute & Networking"),
    ("Data Center - Hyperscale", "Data Center - Hyperscale"),
])
def test_snake_case_keys_become_slice_names(key, name):
    assert breakdown.pretty(key) == name


async def test_when_the_segments_tool_has_no_breakdowns_the_income_statement_is_searched(env, store, caplog):
    units = {"units": "USD millions", "reporting_currency": "USD"}
    unavailable = {"ticker": "TSLA", "available": False, "message": "TSLA's record carries no geographic breakdown."}
    statement = {**units, "periods": [{"period": "Q3 FY2026", "income_statement": {
        "revenue": 28000, "cost_of_revenue": 22000, "revenue_by_product_line": {
            "automotive_sales": 20094, "services_and_other": 4605, "energy_generation_and_storage": 2881,
            "total_revenues": 27580},
        "revenue_by_region": {"united_states": 12495, "china": 5209, "other_international": 10342}}}]}
    answers = {
        ("get_segments", "all"): {**units, "showing": ["segments", "revenue_by_product"], "periods": [
            {"period": "Q3 FY2026", "segment_detail": {"automotive_segment": {"revenue": 21000},
                                                         "energy_generation_and_storage_segment": {"revenue": 2881}}}]},
        ("get_segments", "revenue_by_product"): unavailable,
        ("get_statements", None): statement}

    class Scout(FakeMcp):
        async def call(self, tool, args):
            self.calls.append((tool, args))
            return json.dumps(answers[(tool, args.get("breakdown"))])

    server = Scout("ticker-scout", tools=("get_segments", "get_statements"))
    made = charts.Charts()
    with caplog.at_level("WARNING", logger="bot"):
        caption = await breakdown.fast(make(env, store, server), "TSLA revenue breakdown", made)
    assert [t for _, t in made.photos] == ["TSLA Q3 FY2026 revenue by segment ($B)",
                                           "TSLA Q3 FY2026 revenue by product line ($B)",
                                           "TSLA Q3 FY2026 revenue by region ($B)"]
    assert caption.startswith("<b>TSLA</b> Q3 FY2026 revenue $23.9B: Automotive is 88% of the segment split.")
    assert [c for c, _ in server.calls] == ["get_segments", "get_segments", "get_statements"] and not caplog.records


async def test_alphabet_has_revenue_by_type_and_segments_with_totals_and_subtotals_left_out(env, store):
    googl = (Path(__file__).parent / "fixtures" / "tickerscout" / "get_segments__GOOGL.json").read_text()

    class Scout(FakeMcp):
        async def call(self, tool, args):
            self.calls.append((tool, args))
            return googl if args["breakdown"] == "all" else json.dumps({"available": False, "message": "none"})

    made = charts.Charts()
    caption = await breakdown.fast(make(env, store, Scout("ticker-scout", tools=("get_segments",))),
                                   "GOOGL revenue breakdown", made)
    assert [t for _, t in made.photos] == ["GOOGL Q3 FY2026 revenue by segment ($B)",
                                           "GOOGL Q3 FY2026 revenue by type ($B)"]
    assert caption == "<b>GOOGL</b> Q3 FY2026 revenue $127.6B: Google services is 75% of the segment split."
    period = json.loads(googl)["periods"][0]
    by_type = dict(breakdown.groupings(period))["type"]
    assert [n for n, _ in by_type] == ["Google search and other", "Youtube ads", "Google network",
                                       "Google subscriptions platforms and devices", "Google cloud", "Other bets",
                                       "Hedging gains losses"]  # no advertising/services subtotals, no total revenues
