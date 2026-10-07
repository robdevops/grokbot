import httpx
import pytest
from openai import BadRequestError

from lib import config
from lib.llm.openrouter import OpenRouterBackend
from lib.llm.xai import XaiBackend
from lib.llm.zai import ZaiBackend
from lib.mcp.schema import ToolDef

from .fakes import (
    FakeOpenRouterClient,
    FakeXaiClient,
    Stream,
    or_chunk,
    or_tc,
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
    client = FakeOpenRouterClient(Stream([or_chunk("hello"), or_chunk(finish="stop")]))
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
    client = FakeOpenRouterClient(Stream([or_chunk("x", finish="stop")]))
    b = OpenRouterBackend(config.load(env), client)
    r = req()
    await b.step(b.start(r), r, tool_choice=None)
    assert "extra_headers" not in client.kwargs[0] and "session_id" not in client.kwargs[0]["extra_body"]


async def test_openrouter_tool_calls_without_index_and_replay(env):
    chunks = [or_chunk(tool_calls=[or_tc(None, "a", "y__q", "{")]), or_chunk(tool_calls=[or_tc(None, None, None, "}")]),
              or_chunk(tool_calls=[or_tc(None, "b", "web_search", '{"query":"z"}')]), or_chunk(finish="tool_calls")]
    b = OpenRouterBackend(config.load(env), FakeOpenRouterClient(Stream(chunks)))
    r = req()
    conv = b.start(r)
    step = await b.step(conv, r, tool_choice="required")
    assert [(c.id, c.name, c.arguments) for c in step.calls] == [("a", "y__q", "{}"), ("b", "web_search", '{"query":"z"}')]
    b.add_results(conv, step, ["r1", "r2"])
    assert conv["messages"][-3]["tool_calls"][0]["id"] == "a"
    assert [m["content"] for m in conv["messages"][-2:]] == ["r1", "r2"]


async def test_openrouter_provider_error_in_stream_raises(env):
    bad = or_chunk()
    bad.model_extra = {"error": {"code": 429, "message": "slow down"}}
    b = OpenRouterBackend(config.load(env), FakeOpenRouterClient(Stream([bad])))
    r = req()
    with pytest.raises(RuntimeError, match="slow down"):
        await b.step(b.start(r), r, tool_choice=None)


async def test_openrouter_run_search_and_empty_result(env):
    from types import SimpleNamespace as N

    def reply(text, finish="stop"):
        return N(choices=[N(message=N(content=text), finish_reason=finish)])

    class C:
        def __init__(self, *replies):
            self.replies = list(replies)
            self.chat = N(completions=N(create=self.create))

        async def create(self, **kw):
            return self.replies.pop(0)

    b = OpenRouterBackend(config.load(env), C(reply("- NVDA 100 [Reuters]"), reply("", "length")))
    assert await b.run_search("nvda") == "- NVDA 100 [Reuters]"
    assert "returned no text" in await b.run_search("nvda")
    off = OpenRouterBackend(config.load({**env, "SEARCH": "off"}), C())
    assert await off.run_search("x") is None


# ---- z.ai ----------------------------------------------------------------------------
ZAI_ENV = {"TELEGRAM_BOT_TOKEN": "1:x", "ZAI_API_KEY": "key"}


async def test_zai_request_shape_usage_cost_and_no_search():
    from types import SimpleNamespace as N
    usage = N(prompt_tokens=1_000_000, completion_tokens=1_000_000, prompt_tokens_details=N(cached_tokens=10))
    client = FakeOpenRouterClient(Stream([or_chunk("hi", extra={"reasoning_content": "hmm"}),
                                          or_chunk(finish="stop", usage=usage)]))
    b = ZaiBackend(config.load(ZAI_ENV), client)
    r = req(tools=[TOOL], search=True, cache_id="c", reasoning="low", model="glm-5.3-flash")
    step = await b.step(b.start(r), r, tool_choice="required")
    kw = client.kwargs[0]
    assert step.text == "hi" and step.usage.tokens_in == 1_000_000 and step.usage.cached == 10
    assert step.usage.cost == pytest.approx(0.65)
    assert [t["type"] for t in kw["tools"]] == ["function"] and "tool_choice" not in kw
    assert kw["extra_body"] == {"reasoning_effort": "low"} and "extra_headers" not in kw


async def test_zai_none_leaves_tools_out_and_unknown_model_costs_zero():
    client = FakeOpenRouterClient(Stream([or_chunk("x", finish="stop")]))
    b = ZaiBackend(config.load(ZAI_ENV), client)
    r = req(tools=[TOOL], model="glm-other")
    step = await b.step(b.start(r), r, tool_choice="none")
    assert "tools" not in client.kwargs[0] and step.usage.cost == 0


async def test_zai_whole_tool_call_chunk_and_replay():
    chunks = [or_chunk(tool_calls=[or_tc(0, "call_1", "y__q", '{"t":"NVDA"}')]), or_chunk(finish="tool_calls")]
    b = ZaiBackend(config.load(ZAI_ENV), FakeOpenRouterClient(Stream(chunks)))
    r = req()
    conv = b.start(r)
    step = await b.step(conv, r, tool_choice=None)
    assert [(c.id, c.name, c.arguments) for c in step.calls] == [("call_1", "y__q", '{"t":"NVDA"}')]
    b.add_results(conv, step, ["$239.24"])
    assert conv["messages"][-1] == {"role": "tool", "tool_call_id": "call_1", "content": "$239.24"}


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
