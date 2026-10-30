from types import SimpleNamespace as N

import pytest

from lib.features import filings
from lib.telegram import commands

from .conftest import update_for, user_msg
from .fakes import FakeMcp
from .test_charts import make

EARNINGS = ("# Tesla (TSLA) — Earnings\n\n*Record FY26Q3. 2 matches in events.md.*\n\n"
            "## Q3 results\n\n**Revenue** rose 12% & margins <fell>.\n\n- Guidance raised\n- See *note* 4\n")


class Scout(FakeMcp):
    def __init__(self, reply=EARNINGS):
        super().__init__("ticker-scout", tools=("get_events", "get_narrative"), reply=reply)


def rows(markup):
    return [[(b.text, b.callback_data) for b in row] for row in markup.inline_keyboard]


@pytest.mark.parametrize("words, expected", [
    ([], (0, "all")),
    (["risk", "factors"], (2, "risk factors")),
    (["MD&A"], (3, "md&a")),
    (["legal"], (6, "legal")),
    (["Item", "1A"], (None, "Item 1A")),  # a heading of the filing, passed on as it is
])
def test_narrative_sections_by_name_or_default(words, expected):
    assert filings.pick(filings.KINDS["n"], words) == expected


def test_events_default_to_earnings_and_match_a_name_prefix():
    kind = filings.KINDS["e"]
    assert filings.pick(kind, []) == (0, "earnings")
    assert filings.pick(kind, ["debt"]) == (3, "debt and financing")
    assert filings.pick(kind, ["m&a"]) == (4, "m&a")


def test_buttons_are_the_other_sections_in_rows_of_three():
    markup = filings.keyboard(filings.KINDS["e"], "TSLA", 0)
    assert [len(r) for r in rows(markup)] == [3, 3, 1]
    assert rows(markup)[0] == [("Management", "fl:e:TSLA:1"), ("Capital returns", "fl:e:TSLA:2"),
                               ("Debt & financing", "fl:e:TSLA:3")]
    assert "Earnings" not in [t for row in rows(markup) for t, _ in row]
    assert len(filings.keyboard(filings.KINDS["n"], "TSLA", None).inline_keyboard[0]) == 3


def test_markdown_becomes_telegram_html_and_a_cut_is_explained():
    out = filings.to_html(EARNINGS)
    assert out.startswith("<b>Tesla (TSLA) — Earnings</b>") and "<b>Q3 results</b>" in out
    assert "<b>Revenue</b> rose 12% &amp; margins &lt;fell&gt;." in out
    assert "• Guidance raised" in out and "• See <i>note</i> 4" in out
    cut = filings.to_html("text\n...[truncated; 51234 chars total]")
    assert cut.startswith("text") and "full text is 51,234 characters" in cut and "truncated" not in cut


async def test_events_command_replies_with_the_default_section_and_buttons_for_the_rest(env, store):
    scout = Scout()
    ctx = make(env, store, scout)
    m = user_msg(ctx.bot, "/events tsla", chat_id=-100)
    await commands.events(ctx, update_for(m), N(args=["tsla"]))
    assert scout.calls == [("get_events", {"ticker": "TSLA", "section": "earnings"})]
    [reply] = m.replies
    assert reply["text"].startswith("<b>Tesla (TSLA) — Earnings</b>")
    assert rows(reply["reply_markup"])[0][0] == ("Management", "fl:e:TSLA:1")
    assert store.history(-100, 20)[0].text == "/events tsla"


async def test_narrative_defaults_to_all_and_takes_a_section(env, store):
    scout = Scout("# Tesla (TSLA) — Legal\n\nA case.")
    ctx = make(env, store, scout)
    for words in (["TSLA"], ["TSLA", "risk", "factors"]):
        await commands.narrative(ctx, update_for(user_msg(ctx.bot, "/narrative", chat_id=-100)), N(args=words))
    assert [a["section"] for _, a in scout.calls] == ["all", "risk factors"]
    assert all(t == "get_narrative" for t, _ in scout.calls)


async def test_a_button_sends_that_section_as_a_new_reply(env, store):
    scout = Scout("# Tesla (TSLA) — Legal\n\nA case.")
    ctx = make(env, store, scout)
    sent = user_msg(ctx.bot, "earlier reply", chat_id=-100)
    q = N(message=sent, data="fl:n:TSLA:6", answered=[])

    async def answer(text=None, **kw):
        q.answered.append(text)

    q.answer = answer
    await commands.on_filing_button(ctx, N(callback_query=q), None)
    assert scout.calls == [("get_narrative", {"ticker": "TSLA", "section": "legal"})]
    assert sent.replies[0]["text"].startswith("<b>Tesla (TSLA) — Legal</b>") and q.answered == [None]
    assert "Legal" not in [t for row in rows(sent.replies[0]["reply_markup"]) for t, _ in row]


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
