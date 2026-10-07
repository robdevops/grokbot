"""Fakes for the LLM layer: scripted backend, fake SDK clients and streams."""

from __future__ import annotations

from types import SimpleNamespace as N

from lib.llm.base import Backend, Call, Request, Step, Usage
from lib.mcp.schema import ToolDef
from lib.mcp.server import MCPServer, Registry


class ScriptedBackend(Backend):
    """Returns pre-made Steps (or raises pre-made exceptions) and records what it was sent."""

    name = "scripted"
    search_what = "the web"

    def __init__(self, script, search_runner=None):
        self.script = list(script)
        self.seen: list[dict] = []
        self.results_log: list[list[str]] = []
        self.search_runner = search_runner

    def start(self, req):
        return {"user": [req.parts]}

    async def step(self, conv, req, *, tool_choice):
        self.seen.append({"choice": tool_choice, "tools": [t.name for t in req.tools],
                          "search": req.search, "system": req.system, "model": req.model,
                          "prompt": req.parts[0].get("text", "") if req.parts else "",
                          "max_tokens": req.max_tokens, "reasoning": req.reasoning})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def add_results(self, conv, step, results):
        self.results_log.append(results)

    def add_user_message(self, conv, text):
        conv["user"].append(text)

    async def run_search(self, query):
        return await self.search_runner(query) if self.search_runner else None


def step(text="", calls=(), finish="stop", searches=0, usage=None):
    return Step(text, [Call(*c) for c in calls], usage or Usage(100, 40, 10), finish, searches)


class FakeMcp(MCPServer):
    """An MCPServer whose tools are canned."""

    def __init__(self, label="yahoo", tools=("get_quote",), reply="price 5", **cfg):
        super().__init__(label, cfg, timeout=5, max_output=10_000)
        self.fn_names = {f"{label}__{t}": t for t in tools}
        self.tools = [ToolDef(n, "d") for n in self.fn_names]
        self.session = object()
        self.calls: list[tuple[str, dict]] = []
        self.reply = reply

    async def call(self, tool, args):
        self.calls.append((tool, args))
        return self.reply


def registry(*servers):
    return Registry(list(servers))


class Stream:
    def __init__(self, items):
        self.items = list(items)

    def __aiter__(self):
        self._it = iter(self.items)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration from None


def chat_chunk(content=None, tool_calls=None, finish=None, usage=None, extra=None):
    return N(usage=usage, model_extra={}, choices=[N(
        delta=N(content=content, tool_calls=tool_calls, model_extra=extra or {}), finish_reason=finish)])


def chat_tc(index, id=None, name=None, args=None):
    return N(index=index, id=id, function=N(name=name, arguments=args))


class FakeChatClient:
    def __init__(self, *streams):
        self.streams = list(streams)
        self.kwargs: list[dict] = []
        self.chat = N(completions=N(create=self._create))

    async def _create(self, **kw):
        self.kwargs.append(kw)
        return self.streams.pop(0)


def xai_final(output=(), text="", usage=None, status="completed", id="r1"):
    u = usage or N(input_tokens=100, output_tokens=10, input_tokens_details=N(cached_tokens=40))
    return N(type="response.completed", response=N(
        status=status, output=list(output), output_text=text, usage=u, id=id, error=None))


def xai_call(call_id, name, args):
    d = {"type": "function_call", "call_id": call_id, "name": name, "arguments": args}
    return N(type="function_call", call_id=call_id, name=name, arguments=args,
             model_dump=lambda exclude_none=True: d)


class FakeXaiClient:
    def __init__(self, *streams):
        self.streams = list(streams)
        self.kwargs: list[dict] = []
        self.responses = N(create=self._create)

    async def _create(self, **kw):
        self.kwargs.append(kw)
        item = self.streams.pop(0)
        if isinstance(item, Exception):
            raise item
        return Stream(item)


def req(**kw) -> Request:
    base = dict(system="SYS", parts=[{"type": "text", "text": "hi"}], model="m")
    base.update(kw)
    return Request(**base)
