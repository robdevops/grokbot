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


def tap(sent, data):
    """A tap on a button under `sent`, recording the answer and the edit."""
    q = N(message=sent, data=data, answered=[], edits=[])

    async def answer(text=None, **kw):
        q.answered.append(text)

    async def edit_message_text(text, **kw):
        q.edits.append({"text": text, **kw})

    q.answer, q.edit_message_text = answer, edit_message_text
    return q


def tool_call(tool, ticker, section):
    return [("1", f"ticker-scout__{tool}", json.dumps({"ticker": ticker, "section": section}))]


LONG = "# Tesla (TSLA) — Business\n\n" + "Long paragraph of detail. " * 400


async def test_narrative_goes_straight_to_the_model_which_calls_the_tool_itself(env, store):
    scout = Scout("# Tesla (TSLA) — Business\n\nA long text.")
    ctx, backend, _ = make_ctx(env, store, [step(calls=tool_call("get_narrative", "TSLA", "business"), finish="tool_calls"),
                                            step("**Tesla** sells cars and energy.")], servers=(scout,))
    m = user_msg(ctx.bot, "/narrative tsla", chat_id=-100)
    await commands.narrative(ctx, update_for(m), N(args=["tsla"]))
    assert m.replies[0]["text"] == "<b>Tesla</b> sells cars and energy."
    assert len(backend.seen) == 2 and scout.calls == [("get_narrative", {"ticker": "TSLA", "section": "business"})]
    assert {seen["max_tokens"] for seen in backend.seen} == {32_000} and ctx.st.max_tokens == 3000  # not the chat cap
    assert shaded(m.replies[0]["reply_markup"]) == ["fl:n:TSLA:0"]


async def test_a_narrative_section_is_remembered_so_a_second_tap_asks_nothing(env, store):
    scout = Scout()
    ctx, backend, _ = make_ctx(env, store, [step(calls=tool_call("get_narrative", "TSLA", "legal"), finish="tool_calls"),
                                            step("A case.")], servers=(scout,))
    for _ in range(2):
        q = tap(user_msg(ctx.bot, "earlier reply", chat_id=-100), "fl:n:TSLA:5")
        await commands.on_filing_button(ctx, N(callback_query=q), None)
        [edit] = q.edits
        assert edit["text"] == "A case." and shaded(edit["reply_markup"]) == ["fl:n:TSLA:5"] and q.answered == [None]
    assert len(backend.seen) == 2 and len(scout.calls) == 1


async def test_when_the_model_fails_a_narrative_shows_the_start_of_the_text(env, store):
    scout = Scout("# Tesla (TSLA) — Business\n\n" + "Line of detail.\n" * 600)
    ctx, backend, _ = make_ctx(env, store, [], servers=(scout,))  # nothing scripted: the model call fails
    q = tap(user_msg(ctx.bot, "earlier reply", chat_id=-100), "fl:n:TSLA:0")
    await commands.on_filing_button(ctx, N(callback_query=q), None)
    text = q.edits[0]["text"]
    assert text.startswith("<b>Tesla (TSLA) — Business</b>") and len(text) <= filings.LIMIT and text.endswith("…")


async def test_an_events_section_that_fits_is_shown_as_it_is_with_no_model(env, store):
    scout = Scout()
    ctx, backend, _ = make_ctx(env, store, [], servers=(scout,))
    q = tap(user_msg(ctx.bot, "earlier reply", chat_id=-100), "fl:e:TSLA:0")
    await commands.on_filing_button(ctx, N(callback_query=q), None)
    [edit] = q.edits
    assert edit["text"].startswith("<b>Tesla (TSLA) — Earnings</b>") and backend.seen == []
    assert shaded(edit["reply_markup"]) == ["fl:e:TSLA:0"] and q.answered == [None]


async def test_an_events_section_too_long_for_one_message_is_shortened_by_the_model(env, store):
    scout = Scout("# Tesla (TSLA) — Earnings\n\n" + "Long paragraph of detail. " * 400)
    ctx, backend, _ = make_ctx(env, store, [step(calls=tool_call("get_events", "TSLA", "earnings"), finish="tool_calls"),
                                            step("Short earnings.")], servers=(scout,))
    q = tap(user_msg(ctx.bot, "earlier reply", chat_id=-100), "fl:e:TSLA:0")
    await commands.on_filing_button(ctx, N(callback_query=q), None)
    assert q.edits[0]["text"] == "Short earnings." and len(backend.seen) == 2
    assert [c for c, _ in scout.calls] == ["get_events", "get_events"]  # the size check, then the model's


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


HEADINGS = """# Tesla (TSLA) — Earnings

*Record FY26Q3. 3 matches in events.md.*

## October 22, 2026 — Q3 results

Revenue rose.

## Jul 23, 2026 — Q2 results

Margins fell.

## 2026-04-22 — Q1 results

Deliveries up.
"""

BULLETS = """# Tesla (TSLA) — Management changes

- **October 3, 2026** — CFO resigns.
- **June 1, 2026** — New director.
- **March 9, 2026** — COO appointed.
"""


def test_events_are_put_in_ascending_order_whether_headed_or_bulleted():
    out = filings.oldest_first(HEADINGS)
    assert [line for line in out.split("\n") if line.startswith("## ")] == [
        "## 2026-04-22 — Q1 results", "## Jul 23, 2026 — Q2 results", "## October 22, 2026 — Q3 results"]
    assert out.startswith("# Tesla (TSLA) — Earnings\n\n*Record FY26Q3.") and "Margins fell." in out
    assert [line.split("— ")[1] for line in filings.oldest_first(BULLETS).split("\n") if line.startswith("- ")] == [
        "COO appointed.", "New director.", "CFO resigns."]
    assert filings.oldest_first(filings.oldest_first(HEADINGS)) == filings.oldest_first(HEADINGS)  # already ascending


@pytest.mark.parametrize("text", [
    "# Title\n\nJust prose, no dated items.",
    "# Title\n\n## Earnings releases\n\n- **October 3, 2026** — one\n\n## Guidance\n\ntext",  # an undated heading: unreadable
    "# Title\n\n## October 3, 2026 — only one item\n\ntext",
])
def test_text_with_undated_or_single_items_is_left_alone(text):
    assert filings.oldest_first(text) == text


async def test_events_are_shown_oldest_first_and_narrative_is_left_as_the_model_wrote_it(env, store):
    ctx = make(env, store, Scout(HEADINGS))
    m = user_msg(ctx.bot, "/events tsla", chat_id=-100)
    await commands.events(ctx, update_for(m), N(args=["tsla"]))
    text = m.replies[0]["text"]
    assert text.index("2026-04-22") < text.index("Jul 23, 2026") < text.index("October 22, 2026")


async def test_what_the_model_writes_streams_into_the_message_and_ends_as_html(env, store, monkeypatch):
    import asyncio

    from lib.telegram.live import LiveMessage
    monkeypatch.setattr(LiveMessage, "INTERVAL", 0.01)

    async def streaming_ask(ctx, parts, route, *, on_text, **kw):
        on_text("**Tesla** sells <b>cars")  # half-written: shown as plain text
        await asyncio.sleep(0.15)
        on_text("**Tesla** sells cars.")
        await asyncio.sleep(0.15)
        return N(text="**Tesla** sells cars.")

    monkeypatch.setattr(filings, "ask", streaming_ask)
    ctx = make(env, store, Scout())
    m = user_msg(ctx.bot, "/narrative tsla", chat_id=-100)
    sent, plain_reply = [], m.reply_text

    async def reply_text(text, **kw):
        sent.append(await plain_reply(text, **kw))
        return sent[-1]

    m.reply_text = reply_text
    await commands.narrative(ctx, update_for(m), N(args=["tsla"]))
    [first] = m.replies  # written once while it streamed: plain text, with the buttons already there
    assert first["text"] == "**Tesla** sells cars" and "parse_mode" not in first and first["reply_markup"] is not None
    [final] = sent[0].edits[-1:]  # the end is an edit of that message, as HTML
    assert final["text"] == "<b>Tesla</b> sells cars." and final["parse_mode"] == "HTML"


async def test_a_message_is_cached_for_24_hours(env, store, monkeypatch):
    import time as real_time
    scout = Scout()
    ctx = make(env, store, scout)
    for _ in range(2):
        m = user_msg(ctx.bot, "/events tsla", chat_id=-100)
        await commands.events(ctx, update_for(m), N(args=["tsla"]))
        assert m.replies[0]["text"].startswith("<b>Tesla (TSLA) — Earnings</b>")
    assert len(scout.calls) == 1  # the second came from the cache
    now = real_time.time()
    monkeypatch.setattr("lib.store.time.time", lambda: now + 24 * 3600 + 60)
    m = user_msg(ctx.bot, "/events tsla", chat_id=-100)
    await commands.events(ctx, update_for(m), N(args=["tsla"]))
    assert len(scout.calls) == 2  # a day later it is fetched again


async def test_a_stand_in_for_a_failed_model_is_not_cached(env, store):
    scout = Scout("# Tesla (TSLA) — Business\n\n" + "Line of detail.\n" * 600)
    ctx, backend, _ = make_ctx(env, store, [], servers=(scout,))  # the model fails each time
    for _ in range(2):
        q = tap(user_msg(ctx.bot, "earlier reply", chat_id=-100), "fl:n:TSLA:0")
        await commands.on_filing_button(ctx, N(callback_query=q), None)
        assert q.edits[0]["text"].endswith("…")
    assert len(scout.calls) == 2  # fetched again: nothing was kept
