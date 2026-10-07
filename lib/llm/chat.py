"""Shared chat-completions adapter: streaming loop, tool-call assembly, usage. Providers subclass it."""

from __future__ import annotations

from openai import AsyncOpenAI

from ..config import Settings
from .base import Backend, Call, Request, Step, Usage


def to_content(parts: list[dict]) -> list[dict]:
    out = []
    for p in parts:
        if p["type"] == "text":
            out.append({"type": "text", "text": p["text"]})
        else:
            out.append({"type": "image_url",
                        "image_url": {"url": p["url"], "detail": p.get("detail", "low")}})
    return out


def raise_provider_error(obj) -> None:
    """Some providers report failures inside a normal HTTP 200 body or stream chunk."""
    err = (getattr(obj, "model_extra", None) or {}).get("error")
    if err:
        raise RuntimeError(f"provider error {err.get('code', '')}: {err.get('message', err)}")


class ChatBackend(Backend):
    """Subclasses provide `_kwargs` and may override `_read_delta`, `_assistant_fields` and `_cost`.
    `extra` is a per-step dict a subclass fills in `_read_delta` (`extra["searches"]` counts searches)."""

    def __init__(self, st: Settings, client: AsyncOpenAI):
        self.st = st
        self.client = client

    def start(self, req: Request) -> dict:
        return {"messages": [
            {"role": "system", "content": req.system},
            {"role": "user", "content": to_content(req.parts)},
        ]}

    def _kwargs(self, req: Request, tool_choice: str | None) -> dict:
        raise NotImplementedError

    def _read_delta(self, delta, extra: dict) -> None:
        """Pick provider-specific fields out of a streamed delta."""

    def _assistant_fields(self, extra: dict) -> dict:
        """Extra fields the next request must carry on this step's assistant message."""
        return {}

    def _cost(self, usage_obj, model: str) -> float:
        """The step's cost in USD."""
        return 0.0

    async def step(self, conv: dict, req: Request, *, tool_choice: str | None) -> Step:
        stream = await self.client.chat.completions.create(
            messages=conv["messages"], **self._kwargs(req, tool_choice))
        text, finish, usage_obj = "", None, None
        slots: dict[int, dict] = {}
        extra: dict = {}
        async for chunk in stream:
            raise_provider_error(chunk)
            if chunk.usage:
                usage_obj = chunk.usage
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            if delta.content:
                text += delta.content
                if req.on_text:
                    req.on_text(text)
            for c in delta.tool_calls or []:
                # Some providers omit the index: an id starts a new call, no id continues the last.
                key = c.index if c.index is not None else (len(slots) if c.id or not slots else max(slots))
                slot = slots.setdefault(key, {"id": "", "name": "", "arguments": ""})
                slot["id"] = c.id or slot["id"]
                if c.function:
                    slot["name"] += c.function.name or ""
                    slot["arguments"] += c.function.arguments or ""
            self._read_delta(delta, extra)
            finish = choice.finish_reason or finish
        calls = [Call(**slots[k]) for k in sorted(slots)]
        assistant: dict = {"role": "assistant", "content": text or None}
        if calls:
            assistant["tool_calls"] = [
                {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
                for c in calls]
        assistant.update(self._assistant_fields(extra))
        u = usage_obj
        usage = Usage(
            tokens_in=getattr(u, "prompt_tokens", 0) or 0,
            cached=getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0) or 0,
            tokens_out=getattr(u, "completion_tokens", 0) or 0,
            cost=self._cost(u, req.model),
        )
        return Step(text.strip(), calls, usage, finish, extra.get("searches", 0), raw=assistant)

    def add_results(self, conv: dict, step: Step, results: list[str]) -> None:
        conv["messages"].append(step.raw)
        conv["messages"] += [{"role": "tool", "tool_call_id": c.id, "content": out}
                             for c, out in zip(step.calls, results, strict=True)]

    def add_user_message(self, conv: dict, text: str) -> None:
        conv["messages"].append({"role": "user", "content": text})
