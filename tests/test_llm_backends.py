import json

import httpx
import pytest
from openai import BadRequestError

from lib import config
from lib.llm.base import Usage
from lib.llm.openrouter import OpenRouterBackend
from lib.llm.xai import XaiBackend
from lib.llm.zai import ZaiBackend
from lib.mcp.schema import ToolDef

from .fakes import (
    FakeChatClient,
    FakeXaiClient,
    Stream,
    chat_chunk,
    chat_tc,
    req,
    xai_call,
    xai_final,
)

TOOL = ToolDef("yahoo__q", "Quote.", {"type": "object", "properties": {}})


def bad_request():
    r = httpx.Response(400, request=httpx.Request("POST", "http://x"))
    return BadRequestError("bad", response=r, body=None)


# ---- OpenRouter ----------------------------------------------------------------------
async def test_openrouter_cache_headers_and_tool_shapes(env):
    st = config.load(env)
    client = FakeChatClient(Stream([chat_chunk("hello"), chat_chunk(finish="stop")]))
    b = OpenRouterBackend(st, client)
    r = req(tools=[TOOL], search=True, cache_id="Bot-chat-1", reasoning="low",
            parts=[{"type": "text", "text": "hi"}, {"type": "image", "url": "data:x", "detail": "low"}])
    step = await b.step(b.start(r), r, tool_choice=None)
    kw = client.kwargs[0]
    assert step.text == "hello"
    assert kw["extra_headers"] == {"x-session-id": "Bot-chat-1"}
    assert kw["extra_body"]["session_id"] == "Bot-chat-1"
    assert kw["extra_body"]["prompt_cache_key"] == "Bot-chat-1"
    assert kw["extra_body"]["reasoning"] == {"effort": "low"} and kw["extra_body"]["usage"] == {"include": True}
    assert [t["type"] for t in kw["tools"]] == ["openrouter:web_search", "function"]
    assert kw["tools"][1]["function"]["name"] == "yahoo__q"
    assert kw["messages"][1]["content"][1] == {"type": "image_url", "image_url": {"url": "data:x", "detail": "low"}}
    assert kw["max_tokens"] == st.max_tokens and kw["stream"] is True


async def test_openrouter_no_cache_key_means_no_session_headers(env):
    client = FakeChatClient(Stream([chat_chunk("x", finish="stop")]))
    b = OpenRouterBackend(config.load(env), client)
    r = req()
    await b.step(b.start(r), r, tool_choice=None)
    assert "extra_headers" not in client.kwargs[0] and "session_id" not in client.kwargs[0]["extra_body"]


async def test_openrouter_tool_calls_without_index_and_replay(env):
    chunks = [chat_chunk(tool_calls=[chat_tc(None, "a", "y__q", "{")]), chat_chunk(tool_calls=[chat_tc(None, None, None, "}")]),
              chat_chunk(tool_calls=[chat_tc(None, "b", "web_search", '{"query":"z"}')]), chat_chunk(finish="tool_calls")]
    b = OpenRouterBackend(config.load(env), FakeChatClient(Stream(chunks)))
    r = req()
    conv = b.start(r)
    step = await b.step(conv, r, tool_choice="required")
    assert [(c.id, c.name, c.arguments) for c in step.calls] == [("a", "y__q", "{}"), ("b", "web_search", '{"query":"z"}')]
    b.add_results(conv, step, ["r1", "r2"])
    assert conv["messages"][-3]["tool_calls"][0]["id"] == "a"
    assert [m["content"] for m in conv["messages"][-2:]] == ["r1", "r2"]


async def test_openrouter_provider_error_in_stream_raises(env):
    bad = chat_chunk()
    bad.model_extra = {"error": {"code": 429, "message": "slow down"}}
    b = OpenRouterBackend(config.load(env), FakeChatClient(Stream([bad])))
    r = req()
    with pytest.raises(RuntimeError, match="slow down"):
        await b.step(b.start(r), r, tool_choice=None)


async def test_openrouter_run_search_and_empty_result(env):
    from types import SimpleNamespace as N

    def reply(text, finish="stop", cost=0.002):
        return N(choices=[N(message=N(content=text), finish_reason=finish)], usage=N(cost=cost))

    class C:
        def __init__(self, *replies):
            self.replies = list(replies)
            self.chat = N(completions=N(create=self.create))

        async def create(self, **kw):
            return self.replies.pop(0)

    b = OpenRouterBackend(config.load(env), C(reply("- NVDA 100 [Reuters]"), reply("", "length")))
    spent = Usage()
    assert await b.run_search("nvda", spent, {}) == "- NVDA 100 [Reuters]"
    assert "returned no text" in await b.run_search("nvda", spent, {})
    assert spent.cost == pytest.approx(0.004)  # the search model's cost is counted, even when empty
    off = OpenRouterBackend(config.load({**env, "SEARCH": "off"}), C())
    assert await off.run_search("x", Usage(), {}) is None


# ---- z.ai ----------------------------------------------------------------------------
ZAI_ENV = {"TELEGRAM_BOT_TOKEN": "1:x", "ZAI_API_KEY": "key"}


async def test_zai_request_shape_usage_cost_and_search_tool():
    from types import SimpleNamespace as N
    usage = N(prompt_tokens=1_000_000, completion_tokens=1_000_000, prompt_tokens_details=N(cached_tokens=400_000))
    client = FakeChatClient(Stream([chat_chunk("hi", extra={"reasoning_content": "hmm"}),
                                    chat_chunk(finish="stop", usage=usage)]))
    b = ZaiBackend(config.load(ZAI_ENV), client)
    r = req(tools=[TOOL], search=True, cache_id="c", model="glm-5.3-flash")
    step = await b.step(b.start(r), r, tool_choice="required")
    kw = client.kwargs[0]
    assert step.text == "hi" and step.usage.tokens_in == 1_000_000 and step.usage.cached == 400_000
    assert step.usage.cost == pytest.approx(0.602)  # 600k x 0.15 + 400k x 0.03 + 1M x 0.50, per million
    assert [t["function"]["name"] for t in kw["tools"]] == ["web_search", "yahoo__q"]
    assert kw["tools"][0]["function"]["parameters"]["properties"]["recency"]["enum"][0] == "oneDay"
    assert "tool_choice" not in kw and "extra_headers" not in kw
    assert kw["extra_body"] == {"reasoning_effort": "low"}  # the default


async def test_zai_reasoning_maps_to_its_efforts_and_none_leaves_tools_out():
    client = FakeChatClient(Stream([chat_chunk("x", finish="stop")]), Stream([chat_chunk("y", finish="stop")]))
    b = ZaiBackend(config.load(ZAI_ENV), client)
    r = req(tools=[TOOL], search=True, reasoning="medium", model="glm-other")
    step = await b.step(b.start(r), r, tool_choice="none")
    assert "tools" not in client.kwargs[0] and client.kwargs[0]["extra_body"] == {"reasoning_effort": "high"}
    assert step.usage.cost == 0  # a model with no price shows $0
    r = req(reasoning="max")
    await b.step(b.start(r), r, tool_choice=None)
    assert client.kwargs[1]["extra_body"] == {"reasoning_effort": "max"} and "tools" not in client.kwargs[1]


async def test_zai_warns_once_about_models_without_a_price(caplog):
    ZaiBackend(config.load({**ZAI_ENV, "MODEL": "glm-x", "FAST_MODEL": "glm-5.3-flash"}), FakeChatClient())
    assert [r.getMessage() for r in caplog.records if "No z.ai price" in r.getMessage()] == [
        "No z.ai price for model glm-x; its cost will show $0"]


async def test_zai_whole_tool_call_chunk_and_replay():
    chunks = [chat_chunk(tool_calls=[chat_tc(0, "call_1", "y__q", '{"t":"NVDA"}')]), chat_chunk(finish="tool_calls")]
    b = ZaiBackend(config.load(ZAI_ENV), FakeChatClient(Stream(chunks)))
    r = req()
    conv = b.start(r)
    step = await b.step(conv, r, tool_choice=None)
    assert [(c.id, c.name, c.arguments) for c in step.calls] == [("call_1", "y__q", '{"t":"NVDA"}')]
    b.add_results(conv, step, ["$239.24"])
    assert conv["messages"][-1] == {"role": "tool", "tool_call_id": "call_1", "content": "$239.24"}


def zai_search(reply):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(url=str(request.url), auth=request.headers["authorization"], body=json.loads(request.read()))
        return httpx.Response(200, json=reply)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ZaiBackend(config.load(ZAI_ENV), FakeChatClient(), http), seen


async def test_zai_run_search_request_format_and_cost():
    b, seen = zai_search({"search_result": [
        {"title": "NVDA close", "link": "https://x.test/a", "content": "closed at $238.90", "media": "Bloomberg",
         "publish_date": "Oct 6, 2026"},
        {"title": "News", "link": "https://x.test/b", "content": "c" * 900, "media": "", "publish_date": ""}]})
    spent = Usage()
    out = await b.run_search("nvda close", spent, {"recency": "oneDay"})
    assert seen["url"].endswith("/paas/v4/web_search") and seen["auth"] == "Bearer key"
    assert seen["body"] == {"search_engine": "search-prime", "search_query": "nvda close", "count": 5,
                            "search_recency_filter": "oneDay"}
    assert spent.cost == pytest.approx(0.01)
    lines = out.splitlines()
    assert lines[0] == "- NVDA close [Bloomberg, Oct 6, 2026]: closed at $238.90 (https://x.test/a)"
    assert lines[1].startswith("- News: ccc") and lines[1].endswith("(https://x.test/b)") and len(lines[1]) < 450


async def test_zai_run_search_unknown_recency_means_no_limit_and_empty_results_name_the_intent():
    b, seen = zai_search({"search_result": [], "search_intent": [{"intent": "SEARCH_NOT_NEEDED", "keywords": "hi"}]})
    out = await b.run_search("hi", Usage(), {"recency": "bogus"})
    assert seen["body"]["search_recency_filter"] == "noLimit"
    assert "no results" in out and "SEARCH_NOT_NEEDED" in out and "keywords: hi" in out


async def test_zai_run_search_is_off_with_search_off():
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    b = ZaiBackend(config.load({**ZAI_ENV, "SEARCH": "off"}), FakeChatClient(), http)
    assert await b.run_search("x", Usage(), {}) is None


# ---- xAI -----------------------------------------------------------------------------
def xai_events(*final_args, text="", **kw):
    return [xai_final(*final_args, text=text, **kw)]


async def test_xai_cache_headers_and_flat_tools(env):
    st = config.load({"TELEGRAM_BOT_TOKEN": "1:x", "XAI_API_KEY": "k"})
    client = FakeXaiClient(xai_events(text="hi"))
    b = XaiBackend(st, client)
    r = req(tools=[TOOL], search=True, cache_id="Bot-chat-1", reasoning="low")
    step = await b.step(b.start(r), r, tool_choice=None)
    kw = client.kwargs[0]
    assert step.text == "hi" and (step.usage.tokens_in, step.usage.cached) == (100, 40)
    assert kw["extra_headers"] == {"x-grok-conv-id": "Bot-chat-1"}
    assert kw["extra_body"] == {"prompt_cache_key": "Bot-chat-1"}
    assert [t["type"] for t in kw["tools"]] == ["web_search", "x_search", "function"]
    assert kw["tools"][2]["name"] == "yahoo__q" and "function" not in kw["tools"][2]
    assert kw["input"][0] == {"role": "system", "content": "SYS"} and kw["reasoning"] == {"effort": "low"}


async def test_xai_cost_comes_from_usd_ticks(env):
    from types import SimpleNamespace as N
    st = config.load({"TELEGRAM_BOT_TOKEN": "1:x", "XAI_API_KEY": "k"})
    usage = N(input_tokens=100, output_tokens=10, input_tokens_details=N(cached_tokens=0),
              cost_in_usd_ticks=37_756_000)
    b = XaiBackend(st, FakeXaiClient(xai_events(text="hi", usage=usage)))
    r = req()
    step = await b.step(b.start(r), r, tool_choice=None)
    assert step.usage.cost == pytest.approx(0.0037756)


async def test_xai_forced_search_only_search_tools_and_choice():
    st = config.load({"TELEGRAM_BOT_TOKEN": "1:x", "XAI_API_KEY": "k"})
    client = FakeXaiClient(xai_events(text="ok"))
    b = XaiBackend(st, client)
    r = req(must_search=True)
    await b.step(b.start(r), r, tool_choice="required")
    assert [t["type"] for t in client.kwargs[0]["tools"]] == ["web_search", "x_search"]
    assert client.kwargs[0]["tool_choice"] == "required"


async def test_xai_replays_conversation_then_falls_back_to_previous_response_id():
    st = config.load({"TELEGRAM_BOT_TOKEN": "1:x", "XAI_API_KEY": "k"})
    call = xai_call("c1", "yahoo__q", "{}")
    client = FakeXaiClient(xai_events([call]), bad_request(), xai_events(text="done", id="r2"))
    b = XaiBackend(st, client)
    r = req(tools=[TOOL])
    conv = b.start(r)
    s1 = await b.step(conv, r, tool_choice=None)
    assert [(c.id, c.name) for c in s1.calls] == [("c1", "yahoo__q")]
    b.add_results(conv, s1, ["price 5"])
    assert conv["input"][-2]["type"] == "function_call" and conv["input"][-1]["output"] == "price 5"
    s2 = await b.step(conv, r, tool_choice=None)  # replay rejected -> previous_response_id
    assert s2.text == "done" and b.stateless is False
    assert client.kwargs[2]["previous_response_id"] == "r1"
    assert client.kwargs[2]["input"] == conv["last_results"]


async def test_xai_failed_stream_raises():
    st = config.load({"TELEGRAM_BOT_TOKEN": "1:x", "XAI_API_KEY": "k"})
    client = FakeXaiClient(xai_events(status="failed"))
    b = XaiBackend(st, client)
    r = req()
    with pytest.raises(RuntimeError, match="failed"):
        await b.step(b.start(r), r, tool_choice=None)
