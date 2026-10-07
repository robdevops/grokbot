import httpx
import pytest
from openai import APIStatusError, BadRequestError

from lib import config
from lib.llm.gate import route, wants_tools
from lib.llm.policy import ask
from lib.llm.runner import call_query, run, run_calls
from lib.mcp.schema import ToolDef

from .fakes import FakeMcp, ScriptedBackend, registry, req, step


def status_error(code, cls=APIStatusError):
    r = httpx.Response(code, request=httpx.Request("POST", "http://x"))
    return cls("err", response=r, body=None)


def with_tools(mcp, **kw):
    return req(tools=[ToolDef(n, "d") for n in mcp.fn_names], **kw)


def test_call_query_is_dict_safe():
    assert call_query('{"query":"x"}') == "x" and call_query('{"q":"y"}') == "y"
    assert call_query("[]") == "" and call_query('"x"') == "" and call_query("{bad") == "" and call_query(None) == ""


async def test_tool_round_trip_runs_mcp_and_returns_answer():
    mcp = FakeMcp()
    b = ScriptedBackend([step(calls=[("1", "yahoo__get_quote", '{"t":"X"}')], finish="tool_calls"), step("done")])
    ans = await run(b, registry(mcp), with_tools(mcp))
    assert ans.text == "done" and ans.rounds == 2 and ans.tool_calls == 1
    assert mcp.calls == [("get_quote", {"t": "X"})] and b.results_log == [["price 5"]]
    assert ans.usage.tokens_in == 200 and ans.usage.cached == 80


async def test_final_round_forces_answer_without_tools():
    mcp = FakeMcp()
    loop = [step(calls=[(str(i), "yahoo__get_quote", f'{{"n":{i}}}')], finish="tool_calls")
            for i in range(config.MAX_TOOL_ROUNDS)]
    b = ScriptedBackend(loop + [step("final")])
    ans = await run(b, registry(mcp), with_tools(mcp))
    assert ans.text == "final" and b.seen[-1]["choice"] == "none" and b.seen[0]["choice"] is None


async def test_duplicate_calls_get_a_short_note_instead_of_a_second_result():
    mcp = FakeMcp()
    c = ("1", "yahoo__get_quote", '{"t":"X"}')
    b = ScriptedBackend([step(calls=[c, ("2", *c[1:])], finish="tool_calls"), step("ok")])
    await run(b, registry(mcp), with_tools(mcp))
    assert len(mcp.calls) == 1 and b.results_log[0][0] == "price 5" and "Same call" in b.results_log[0][1]
    mcp2 = FakeMcp()
    b2 = ScriptedBackend([step(calls=[c, ("2", *c[1:])], finish="tool_calls"), step("ok")])
    await run(b2, registry(mcp2), with_tools(mcp2), dedupe=False)
    assert len(mcp2.calls) == 2


async def test_search_cap_unknown_and_unoffered_tools_and_bad_arguments():
    mcp = FakeMcp()

    async def search(q):
        return "found " + q

    calls = [(str(i), "web_search", f'{{"query":"q{i}"}}') for i in range(7)]
    calls += [("m", "yahoo__get_quote", "{}"), ("u", "zzz", "{}")]
    b = ScriptedBackend([step(calls=calls, finish="tool_calls"), step("ok")], search_runner=search)
    await run(b, registry(mcp), with_tools(mcp, search=True))
    res = b.results_log[0]
    assert res[:5] == [f"found q{i}" for i in range(5)] and all("Skipped" in r for r in res[5:7])
    assert res[7] == "price 5" and "isn't available" in res[8]
    b = ScriptedBackend([step(calls=[("1", "web_search", "[]"), ("2", "web_search", '"foo"')],
                              finish="tool_calls"), step("ok")], search_runner=search)
    await run(b, registry(mcp), with_tools(mcp, search=True))
    assert all("no usable query" in r for r in b.results_log[0])


async def test_tool_result_size_and_small_results_are_logged(caplog):
    import logging
    big, small = FakeMcp("yahoo", reply="x" * 500), FakeMcp("sharesight", tools=("list_portfolios",),
                                                            reply='{"portfolios":[]}')
    b = ScriptedBackend([step(calls=[("1", "yahoo__get_quote", "{}"), ("2", "sharesight__list_portfolios", "{}")],
                              finish="tool_calls"), step("ok")])
    with caplog.at_level(logging.INFO, logger="bot"):
        await run(b, registry(big, small), req(tools=[ToolDef(n, "d") for m in (big, small) for n in m.fn_names]))
    assert "MCP yahoo.get_quote -> 500 chars" in caplog.text
    assert 'MCP sharesight.list_portfolios -> 17 chars: {"portfolios":[]}' in caplog.text
    assert "x" * 200 not in caplog.text


async def test_unoffered_mcp_tool_is_refused():
    mcp = FakeMcp()
    b = ScriptedBackend([step(calls=[("1", "yahoo__get_quote", "{}")], finish="tool_calls"), step("ok")])
    await run(b, registry(mcp), req())  # the request offers no tools
    assert not mcp.calls and "isn't available" in b.results_log[0][0]


async def test_search_with_provider_that_runs_searches_itself_is_unavailable():
    b = ScriptedBackend([step(calls=[("1", "web_search", '{"query":"x"}')], finish="tool_calls"), step("ok")])
    await run(b, registry(), req(search=True))
    assert "isn't available" in b.results_log[0][0]


async def test_forced_search_first_round_and_retry_without_required_on_400():
    b = ScriptedBackend([status_error(400, BadRequestError), step("searched", searches=1)])
    ans = await run(b, registry(), req(must_search=True))
    assert ans.text == "searched" and [s["choice"] for s in b.seen] == ["required", None]
    b2 = ScriptedBackend([status_error(429)])
    with pytest.raises(APIStatusError):
        await run(b2, registry(), req(must_search=True))


async def test_fake_search_call_text_is_recovered_once_and_failures_reported():
    async def search(q):
        raise RuntimeError("rate limited")

    text = "<tool_call><function=web_search><parameter=query>nvda</parameter></function></tool_call>"
    b = ScriptedBackend([step(text), step("final")], search_runner=search)
    ans = await run(b, registry(), req(search=True))
    assert ans.text == "final" and b.seen[1]["search"] is False


async def test_provider_without_search_runner_ignores_fake_calls_text():
    text = "Answer.\n\n<tool_call>junk\n\nMore answer"
    b = ScriptedBackend([step(text)])
    assert (await run(b, registry(), req(search=True))).text == "Answer.\n\nMore answer"


async def test_empty_reply_and_token_cap_errors():
    with pytest.raises(RuntimeError, match="empty reply"):
        await run(ScriptedBackend([step("")]), registry(), req())
    with pytest.raises(RuntimeError, match="token cap"):
        await run(ScriptedBackend([step("", finish="length")]), registry(), req())


async def test_token_cap_after_tool_results_is_retried_with_the_data_not_without_tools():
    mcp = FakeMcp()
    b = ScriptedBackend([step(calls=[("1", "yahoo__get_quote", "{}")], finish="tool_calls"),
                         step("", finish="length"), step("the answer")])
    ans = await run(b, registry(mcp), with_tools(mcp, max_tokens=1500, reasoning="high"))
    assert ans.text == "the answer" and ans.tool_calls == 1
    assert b.seen[-1]["max_tokens"] == 3000 and b.seen[-1]["reasoning"] == "low"
    assert b.seen[-1]["choice"] == "none" and b.results_log == [["price 5"]]


async def test_run_calls_reports_mcp_exceptions():
    class Boom(FakeMcp):
        async def call(self, tool, args):
            raise RuntimeError("down")

    mcp = Boom()
    out = await run_calls([__import__("lib.llm.base", fromlist=["Call"]).Call("1", "yahoo__get_quote", "{}")],
                          ScriptedBackend([]), registry(mcp), with_tools(mcp), {}, True)
    assert out == ["Error: RuntimeError: down"]


# ---- policy: the single retry ladder ------------------------------------------------------
async def test_retry_without_tools_for_tool_related_errors():
    mcp = FakeMcp()
    for first in (RuntimeError("provider error"), status_error(400, BadRequestError)):
        b = ScriptedBackend([first, step("recovered")])
        ans = await ask(b, registry(mcp), with_tools(mcp, system="FULL", no_tools_system="BARE", search=True))
        assert ans.text == "recovered"
        assert b.seen[1]["tools"] == [] and b.seen[1]["search"] is False and b.seen[1]["system"] == "BARE"


async def test_no_retry_for_auth_credit_rate_limit_server_errors_or_must_search():
    mcp = FakeMcp()
    for code in (401, 402, 429, 500):
        b = ScriptedBackend([status_error(code), step("never")])
        with pytest.raises(APIStatusError):
            await ask(b, registry(mcp), with_tools(mcp))
        assert len(b.seen) == 1
    b = ScriptedBackend([RuntimeError("x"), step("never")])
    with pytest.raises(RuntimeError, match="x"):
        await ask(b, registry(), req(must_search=True, search=True))
    assert len(b.seen) == 1
    b = ScriptedBackend([RuntimeError("x"), step("never")])  # nothing to take away: no retry
    with pytest.raises(RuntimeError):
        await ask(b, registry(), req())


async def test_failed_retry_raises_the_original_error():
    mcp = FakeMcp()
    b = ScriptedBackend([RuntimeError("first"), RuntimeError("second")])
    with pytest.raises(RuntimeError, match="first"):
        await ask(b, registry(mcp), with_tools(mcp))


# ---- gate -----------------------------------------------------------------------------
def servers():
    return FakeMcp("yahoo"), FakeMcp("sharesight", gate="portfolio")


def test_gate_chit_chat_gets_no_tools(settings):
    y, s = servers()
    r = route("what do you think of the epistemological reasoning within the zeitgeist?", settings, registry(y, s))
    assert r.simple and not r.servers and not r.search


def test_gate_market_question_gets_yahoo_and_search_but_not_sharesight(settings):
    y, s = servers()
    r = route("how is NVDA looking after earnings?", settings, registry(y, s))
    assert r.servers == [y] and r.search and not r.simple


def test_gate_portfolio_question_adds_sharesight(settings):
    y, s = servers()
    assert route("how is my portfolio performing", settings, registry(y, s)).servers == [y, s]


def test_gate_live_question_gets_search_only(settings):
    y, s = servers()
    r = route("who won the match last night? latest news", settings, registry(y, s))
    assert r.search and not r.servers


def test_gate_full_when_forced_or_token_saver_off(settings, env):
    y, s = servers()
    assert route("hello", settings, registry(y, s), force_full=True).servers == [y, s]
    off = config.load({**env, "TOKEN_SAVER": "off"})
    r = route("hello", off, registry(y, s))
    assert r.servers == [y, s] and r.search and not r.simple


PORTFOLIO_PHRASES = [
    "how is my portfolio doing", "what are my holdings", "show my performance this year", "what do I own",
    "how are my stocks", "how is my smsf going", "what's in my SMSF", "how am I doing this year",
    "what did I make this month", "my returns ytd?", "what's my biggest winner", "what's my cash balance",
    "net worth update", "my super fund", "my watchlist", "who's my worst performer", "what's my P&L",
    "my position in CBA", "am I up or down today", "show performance this year", "bob smsf vs bob personal",
    "are my dividends coming", "why did I outperform Alice over the past month?", "how did I do last month",
    "did I beat Alice this year", "why did Alice underperform me", "who's ahead of who this quarter, me or Alice",
]


@pytest.mark.parametrize("text", PORTFOLIO_PHRASES)
def test_gate_recognises_portfolio_questions(text, settings):
    y, s = servers()
    assert s in route(text, settings, registry(y, s)).servers


@pytest.mark.parametrize("text", [
    "what is the performance of NVDA", "how is NVDA doing", "hello there", "what do you think of super mario",
    "any news on the fed?", "who won the match last night",
])
def test_gate_keeps_sharesight_out_of_other_questions(text, settings):
    y, s = servers()
    assert s not in route(text, settings, registry(y, s)).servers


def test_gate_matches_configured_portfolio_names_as_whole_words(env):
    st = config.load({**env, "PORTFOLIO_NAMES": "Family Trust, BobSMSF"})
    y, s = servers()
    for text in ("how is the family trust going", "BobSMSF?", "bobsmsf's returns"):
        assert s in route(text, st, registry(y, s)).servers, text
    assert s not in route("trustworthy family", st, registry(y, s)).servers


def test_gate_marks_routes_that_offer_fewer_tools(settings):
    y, s = servers()
    reg = registry(y, s)
    market = route("how is NVDA looking?", settings, reg)
    assert market.partial and not market.simple  # Yahoo + search, but not Sharesight
    assert not route("how is my portfolio and NVDA looking", settings, reg).partial
    assert route("hello", settings, reg).simple and not route("hello", settings, reg).partial
    assert not route("anything", settings, reg, force_full=True).partial


def test_wants_tools():
    assert wants_tools("NEEDS_TOOLS") and wants_tools("  needs_tools.") and not wants_tools("Sure thing")
