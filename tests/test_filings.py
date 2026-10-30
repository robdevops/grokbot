import json
from types import SimpleNamespace as N

import pytest

from lib.features import filings
from lib.telegram import commands

from .conftest import update_for, user_msg
from .fakes import FakeMcp, step
from .test_charts import make
from .test_handlers import make_ctx

EARNINGS = ("# Tesla (TSLA) — Earnings\n\n*Record FY26Q3. 2 matches in events.md.*\n\n"
            "## Q3 results\n\n**Revenue** rose 12% & margins <fell>.\n\n- Guidance raised\n- See *note* 4\n")


class Scout(FakeMcp):
    def __init__(self, reply=EARNINGS):
        super().__init__("ticker-scout", tools=("get_events", "get_narrative"), reply=reply)


def rows(markup):
    return [[(b.text, b.callback_data) for b in row] for row in markup.inline_keyboard]


@pytest.mark.parametrize("words, expected", [
    ([], (0, "business")),
    (["risk", "factors"], (1, "risk factors")),
    (["MD&A"], (2, "md&a")),
    (["legal"], (5, "legal")),
    (["all"], (None, "all")),  # not a button, but the tool takes it
    (["Item", "1A"], (None, "Item 1A")),  # a heading of the filing, passed on as it is
])
def test_narrative_sections_by_name_or_default_business(words, expected):
    assert filings.pick(filings.KINDS["n"], words) == expected


def test_events_default_to_earnings_and_match_a_name_prefix():
    kind = filings.KINDS["e"]
    assert filings.pick(kind, []) == (0, "earnings")
    assert filings.pick(kind, ["debt"]) == (3, "debt and financing")
    assert filings.pick(kind, ["m&a"]) == (4, "m&a")


def shaded(markup):
    return [b["callback_data"] for row in markup.to_dict()["inline_keyboard"] for b in row if b.get("style") == "primary"]


def test_every_section_has_a_button_in_rows_of_three_and_the_one_shown_is_shaded():
    markup = filings.keyboard(filings.KINDS["e"], "TSLA", 0)
    assert [len(r) for r in rows(markup)] == [3, 3, 2]
    assert rows(markup)[0] == [("Earnings", "fl:e:TSLA:0"), ("Management", "fl:e:TSLA:1"),
                               ("Capital returns", "fl:e:TSLA:2")]
    assert shaded(markup) == ["fl:e:TSLA:0"]
    narrative = filings.keyboard(filings.KINDS["n"], "TSLA", 2)
    assert [t for row in rows(narrative) for t, _ in row] == [
        "Business", "Risk factors", "MD&A", "Current quarter", "Subsequent events", "Legal"]
    assert shaded(narrative) == ["fl:n:TSLA:2"] and shaded(filings.keyboard(filings.KINDS["n"], "TSLA", None)) == []


def test_markdown_becomes_telegram_html_and_a_cut_is_explained():
    out = filings.to_html(EARNINGS)
    assert out.startswith("<b>Tesla (TSLA) — Earnings</b>") and "<b>Q3 results</b>" in out
    assert "<b>Revenue</b> rose 12% &amp; margins &lt;fell&gt;." in out
    assert "• Guidance raised" in out and "• See <i>note</i> 4" in out
    text, note = filings.cut_note("text\n...[truncated; 51234 chars total]")
    assert text == "text" and "full text is 51,234 characters" in note and "truncated" not in note
    assert filings.cut_note("plain") == ("plain", "")


async def test_events_command_replies_with_the_default_section_and_buttons_for_the_rest(env, store):
    scout = Scout()
    ctx = make(env, store, scout)
    m = user_msg(ctx.bot, "/events tsla", chat_id=-100)
    await commands.events(ctx, update_for(m), N(args=["tsla"]))
    assert scout.calls == [("get_events", {"ticker": "TSLA", "section": "earnings"})]
    [reply] = m.replies
    assert reply["text"].startswith("<b>Tesla (TSLA) — Earnings</b>")
    assert rows(reply["reply_markup"])[0][:2] == [("Earnings", "fl:e:TSLA:0"), ("Management", "fl:e:TSLA:1")]
    assert shaded(reply["reply_markup"]) == ["fl:e:TSLA:0"]
    assert store.history(-100, 20)[0].text == "/events tsla"


async def test_narrative_defaults_to_business_and_takes_a_section(env, store):
    scout = Scout("# Tesla (TSLA) — Legal\n\nA case.")
    ctx = make(env, store, scout)
    for words in (["TSLA"], ["TSLA", "risk", "factors"]):
        await commands.narrative(ctx, update_for(user_msg(ctx.bot, "/narrative", chat_id=-100)), N(args=words))
    assert [a["section"] for _, a in scout.calls] == ["business", "risk factors"]
    assert all(t == "get_narrative" for t, _ in scout.calls)


def tap(sent, data):
    """A tap on a button under `sent`, recording the answer and the edit."""
    q = N(message=sent, data=data, answered=[], edits=[])

    async def answer(text=None, **kw):
        q.answered.append(text)

    async def edit_message_text(text, **kw):
        q.edits.append({"text": text, **kw})

    q.answer, q.edit_message_text = answer, edit_message_text
    return q


async def test_a_button_edits_the_message_and_posts_nothing_more(env, store):
    scout = Scout("# Tesla (TSLA) — Legal\n\nA case.")
    ctx = make(env, store, scout)
    sent = user_msg(ctx.bot, "earlier reply", chat_id=-100)
    q = tap(sent, "fl:n:TSLA:5")
    await commands.on_filing_button(ctx, N(callback_query=q), None)
    assert scout.calls == [("get_narrative", {"ticker": "TSLA", "section": "legal"})]
    assert sent.replies == [] and q.answered == [None]
    [edit] = q.edits
    assert edit["text"].startswith("<b>Tesla (TSLA) — Legal</b>") and edit["parse_mode"] == "HTML"
    assert shaded(edit["reply_markup"]) == ["fl:n:TSLA:5"]  # the section now shown


async def test_a_section_too_long_for_one_message_is_shortened_by_a_model_that_calls_the_tool(env, store):
    filings._shortened.clear()
    scout = Scout("# Tesla (TSLA) — Business\n\n" + "Long paragraph of detail. " * 400)
    call = ("1", "ticker-scout__get_narrative", json.dumps({"ticker": "TSLA", "section": "business"}))
    ctx, backend, _ = make_ctx(env, store, [step(calls=[call], finish="tool_calls"), step("**Tesla** sells cars and energy.")],
                               servers=(scout,))
    for _ in range(2):  # the second tap on the same section reuses the answer
        q = tap(user_msg(ctx.bot, "earlier reply", chat_id=-100), "fl:n:TSLA:0")
        await commands.on_filing_button(ctx, N(callback_query=q), None)
        assert q.edits[0]["text"] == "<b>Tesla</b> sells cars and energy."
    assert len(backend.seen) == 2  # one request, two rounds: the tool call, then the answer
    assert [c for c, _ in scout.calls] == ["get_narrative"] * 3  # the size check, the model's call, the next tap's check


async def test_a_failing_model_leaves_the_start_of_the_text(env, store):
    filings._shortened.clear()
    scout = Scout("# Tesla (TSLA) — Business\n\n" + "Line of detail.\n" * 600)
    ctx, backend, _ = make_ctx(env, store, [], servers=(scout,))  # nothing scripted: the model call fails
    q = tap(user_msg(ctx.bot, "earlier reply", chat_id=-100), "fl:n:TSLA:0")
    await commands.on_filing_button(ctx, N(callback_query=q), None)
    text = q.edits[0]["text"]
    assert text.startswith("<b>Tesla (TSLA) — Business</b>") and len(text) <= filings.LIMIT and text.endswith("…")


@pytest.mark.parametrize("data", ["fl:x:TSLA:1", "fl:n:TSLA:99", "fl:n:TSLA:one", "fl:n"])
async def test_an_out_of_date_button_says_so(env, store, data):
    ctx = make(env, store, Scout())
    said = []
    q = N(message=user_msg(ctx.bot, "x"), data=data)

    async def answer(text=None, **kw):
        said.append(text)

    q.answer = answer
    await commands.on_filing_button(ctx, N(callback_query=q), None)
    assert said == ["That button is out of date."]


@pytest.mark.parametrize("args, reply", [([], "Usage: /events TICKER [section]"), (["not a ticker!"], "Usage: /events TICKER [section]")])
async def test_no_ticker_gets_the_usage(env, store, args, reply):
    ctx = make(env, store, Scout())
    m = user_msg(ctx.bot, "/events")
    await commands.events(ctx, update_for(m), N(args=args))
    assert m.replies[0]["text"] == reply


async def test_nothing_to_show_or_no_data_source_says_so(env, store):
    unavailable = '{"ticker": "ZZZZ", "available": false, "message": "No 8-K events for ZZZZ.", "sections": []}'
    ctx = make(env, store, Scout(unavailable))
    m = user_msg(ctx.bot, "/events zzzz")
    await commands.events(ctx, update_for(m), N(args=["zzzz"]))
    assert m.replies[0]["text"].startswith("No 8-K events for ZZZZ.")
    bare = make(env, store)  # no TickerScout configured
    m = user_msg(bare.bot, "/narrative nvda")
    await commands.narrative(bare, update_for(m), N(args=["nvda"]))
    assert m.replies[0]["text"] == "No narrative found for NVDA."
