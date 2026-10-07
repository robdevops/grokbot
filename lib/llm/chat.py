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


class ChatBackend(Backend):
    """Subclasses provide `_kwargs` and may hook `_on_chunk`, `_assistant_extra` and `_cost`."""

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

    def _on_chunk(self, chunk, delta, acc: dict) -> None:
        """Read provider extras from a streamed chunk (delta is None on a choiceless chunk)
        into `acc`; acc["cites"] counts searches."""

    def _assistant_extra(self, acc: dict) -> dict:
        """Extra fields the next request must carry on the assistant message."""
        return {}

    def _cost(self, usage_obj, req: Request) -> float:
        return 0.0

    async def step(self, conv: dict, req: Request, *, tool_choice: str | None) -> Step:
        stream = await self.client.chat.completions.create(
            messages=conv["messages"], **self._kwargs(req, tool_choice))
        text, finish, usage_obj = "", None, None
        slots: dict[int, dict] = {}
        acc: dict = {}
        async for chunk in stream:
            choice = chunk.choices[0] if chunk.choices else None
            self._on_chunk(chunk, choice.delta if choice else None, acc)
            if chunk.usage:
                usage_obj = chunk.usage
            if not choice:
                continue
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
            finish = choice.finish_reason or finish
        calls = [Call(**slots[k]) for k in sorted(slots)]
        assistant: dict = {"role": "assistant", "content": text or None}
        if calls:
            assistant["tool_calls"] = [
                {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
                for c in calls]
        assistant.update(self._assistant_extra(acc))
        u = usage_obj
        usage = Usage(
            tokens_in=getattr(u, "prompt_tokens", 0) or 0,
            cached=getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0) or 0,
            tokens_out=getattr(u, "completion_tokens", 0) or 0,
            cost=self._cost(u, req),
        )
        return Step(text.strip(), calls, usage, finish, acc.get("cites", 0), raw=assistant)

    def add_results(self, conv: dict, step: Step, results: list[str]) -> None:
        conv["messages"].append(step.raw)
        conv["messages"] += [{"role": "tool", "tool_call_id": c.id, "content": out}
                             for c, out in zip(step.calls, results, strict=True)]

    def add_user_message(self, conv: dict, text: str) -> None:
        conv["messages"].append({"role": "user", "content": text})
