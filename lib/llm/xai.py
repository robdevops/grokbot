"""xAI (Grok) over the Responses API: server-side web and X search, prompt-cache headers."""

from __future__ import annotations

import logging

from openai import AsyncOpenAI, BadRequestError

from ..config import Settings
from .base import Backend, Call, Overloaded, Request, Step, Usage

log = logging.getLogger("bot")

TICKS_PER_USD = 10_000_000_000  # xAI reports each response's cost in 1e-10 USD ticks
SEARCH_TOOLS = [{"type": "web_search"}, {"type": "x_search"}]
# Output items replayed into the next round: the model's tool calls, its reasoning (needed to
# continue its line of thought) and any text it wrote. Server-side search calls aren't replayed;
# their results show in its text.
REPLAY_TYPES = ("function_call", "reasoning", "message")


def to_input_parts(parts: list[dict]) -> list[dict]:
    out = []
    for p in parts:
        if p["type"] == "text":
            out.append({"type": "input_text", "text": p["text"]})
        else:
            out.append({"type": "input_image", "image_url": p["url"], "detail": p.get("detail", "low")})
    return out


def _as_input(item) -> dict:
    return item.model_dump(exclude_none=True) if hasattr(item, "model_dump") else dict(item)


def answer_text(resp, tail: str | None = None) -> str:
    """The response's last message, and in it the last text part. `output_text` joins everything, and
    with server-side search the model can write a draft, search, then write the answer, as separate
    messages, as parts of one, or as one run of text with the searches in between. `tail` is the text
    streamed after the last search call; when the chosen text ends with it, the draft before it goes."""
    messages = [i for i in resp.output if i.type == "message"]
    parts = [[getattr(c, "text", "") or "" for c in m.content if c.type == "output_text"] for m in messages]
    texts = ["".join(p) for p in parts]
    if len([t for t in texts if t.strip()]) > 1:
        log.debug("xAI response had %d messages; kept the last (items: %s)", len(texts),
                 " ".join(i.type for i in resp.output))
    for p in parts:
        if len([t for t in p if t.strip()]) > 1:  # not handled yet: this line tells us if it happens
            log.warning("xAI message had %d text parts (%s chars)", len(p), "+".join(str(len(t)) for t in p))
    text = next((t for t in reversed(texts) if t.strip()), resp.output_text or "").strip()
    if tail and tail.strip() and len(tail.strip()) < len(text) and text.endswith(tail.strip()):
        log.debug("xAI wrote %d chars before a search call; kept the %d after it (items: %s)",
                 len(text) - len(tail.strip()), len(tail.strip()), " ".join(i.type for i in resp.output))
        return tail.strip()
    return text


class XaiBackend(Backend):
    name = "xai"
    search_what = "the web and X (Twitter)"

    def __init__(self, st: Settings, client: AsyncOpenAI | None = None):
        # Searches (especially X search) can take a while, so allow a generous timeout.
        self.client = client or AsyncOpenAI(api_key=st.api_key, base_url="https://api.x.ai/v1", timeout=180)
        # Replaying the whole conversation each round caches well; previous_response_id
        # (the fallback) barely does. Flipped off for good if xAI rejects a replay.
        self.stateless = True

    def start(self, req: Request) -> dict:
        first = [
            # A system message rather than `instructions`: xAI rejects `instructions` alongside
            # previous_response_id, and as a message it is stored with the conversation.
            {"role": "system", "content": req.system},
            {"role": "user", "content": to_input_parts(req.parts)},
        ]
        return {"input": first, "resp": None, "pending": None, "last_results": None}

    def _kwargs(self, req: Request, tool_choice: str | None) -> dict:
        tools = (SEARCH_TOOLS if (req.search or req.must_search) else []) + [
            {"type": "function", "name": t.name, "description": t.description, "parameters": t.parameters}
            for t in req.tools]
        kw: dict = {"model": req.model, "stream": True}
        if tools:
            kw["tools"] = tools
            if tool_choice:
                kw["tool_choice"] = tool_choice
        if req.reasoning:
            kw["reasoning"] = {"effort": req.reasoning}
        if req.cache_id:
            # Keep a chat's requests on the xAI server holding its cached prompt.
            kw["extra_body"] = {"prompt_cache_key": req.cache_id}
            kw["extra_headers"] = {"x-grok-conv-id": req.cache_id}
        return kw

    async def _create(self, req: Request, kw: dict, **inputs):
        stream = await self.client.responses.create(**kw, **inputs)
        text, final, tail = "", None, None
        async for event in stream:
            if event.type == "response.output_item.added" and event.item.type == "message":
                text = ""  # a new message replaces the draft before it
            elif event.type == "response.output_item.added" and event.item.type.endswith("_search_call"):
                if text.strip():  # text written before a search is a draft; only what follows counts
                    text, tail = "", ""
            elif event.type == "response.output_text.delta":
                text += event.delta
                if tail is not None:
                    tail = text
                if req.on_text:
                    req.on_text(text)
            elif event.type in ("response.completed", "response.incomplete", "response.failed"):
                final = event.response
        if final is None:
            raise RuntimeError("The stream ended without a final response")
        if final.status == "failed":
            msg = f"xAI response failed: {final.error}"
            raise (Overloaded if getattr(final.error, "code", None) == "server_error" else RuntimeError)(msg)
        return final, tail

    async def step(self, conv: dict, req: Request, *, tool_choice: str | None) -> Step:
        kw = self._kwargs(req, tool_choice)
        prev = conv["resp"]
        try:
            if self.stateless or prev is None:
                resp, tail = await self._create(req, kw, input=conv["input"])
            else:
                resp, tail = await self._create(req, kw, previous_response_id=prev.id, input=conv["pending"])
        except BadRequestError as e:
            if prev is None or not self.stateless:
                raise
            log.warning("xAI rejected the replayed conversation (%s); using previous_response_id "
                        "from now on", e)
            self.stateless = False
            resp, tail = await self._create(req, kw, previous_response_id=prev.id, input=conv["last_results"])
        conv["resp"] = resp
        calls = [Call(i.call_id, i.name, i.arguments) for i in resp.output if i.type == "function_call"]
        u = resp.usage
        details = getattr(u, "input_tokens_details", None)
        usage = Usage(
            tokens_in=getattr(u, "input_tokens", 0) or 0,
            cached=getattr(details, "cached_tokens", 0) or 0,
            tokens_out=getattr(u, "output_tokens", 0) or 0,
            cost=(getattr(u, "cost_in_usd_ticks", 0) or 0) / TICKS_PER_USD,
        )
        searches = sum(1 for i in resp.output if i.type.endswith("_search_call"))
        return Step(answer_text(resp, tail), calls, usage, resp.status, searches)

    def add_results(self, conv: dict, step: Step, results: list[str]) -> None:
        items = [{"type": "function_call_output", "call_id": c.id, "output": out}
                 for c, out in zip(step.calls, results, strict=True)]
        conv["last_results"], conv["pending"] = items, items
        if self.stateless:
            conv["input"] += [_as_input(i) for i in conv["resp"].output if i.type in REPLAY_TYPES]
            conv["input"] += items

    def add_user_message(self, conv: dict, text: str) -> None:
        conv["input"].append({"role": "user", "content": text})
