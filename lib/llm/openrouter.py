"""OpenRouter over chat completions: web_search tool plus a search-model runner, sticky routing."""

from __future__ import annotations

import logging

import httpx
from openai import AsyncOpenAI

from .. import config
from ..config import Settings
from .base import Request
from .chat import ChatBackend

log = logging.getLogger("bot")

BASE_URL = "https://openrouter.ai/api/v1"
SEARCH_PROMPT = (
    "Answer from web search results only, as terse bullet points: the figure or fact, then "
    "source and date in brackets. No sentences, no preamble, no advice, no repetition. If the "
    "results don't cover it, reply exactly: NOT FOUND."
)


def raise_provider_error(obj) -> None:
    """OpenRouter reports provider failures inside a normal HTTP 200 body."""
    err = (getattr(obj, "model_extra", None) or {}).get("error")
    if err:
        raise RuntimeError(f"provider error {err.get('code', '')}: {err.get('message', err)}")


def merge_reasoning(details: list[dict], new: list) -> None:
    """Append streamed reasoning_details, joining consecutive text fragments of one block."""
    for d in new:
        d = dict(d)
        last = details[-1] if details else None
        if (last and d.get("text") and last.get("text") is not None
                and last.get("type") == d.get("type") and last.get("index") == d.get("index")):
            last["text"] += d["text"]
            if d.get("signature"):
                last["signature"] = d["signature"]
        else:
            details.append(d)


class OpenRouterBackend(ChatBackend):
    name = "openrouter"
    search_what = "the web"

    def __init__(self, st: Settings, client: AsyncOpenAI | None = None):
        super().__init__(st, client or AsyncOpenAI(
            api_key=st.api_key, base_url=BASE_URL, timeout=180,
            default_headers={"X-Title": "Telegram group bot"}))

    def _kwargs(self, req: Request, tool_choice: str | None) -> dict:
        tools = []
        if req.search or req.must_search:
            tools.append({"type": "openrouter:web_search", "parameters": {
                "engine": config.SEARCH_ENGINE, "max_total_results": config.SEARCH_RESULTS}})
        tools += [{"type": "function", "function": {
            "name": t.name, "description": t.description, "parameters": t.parameters}}
            for t in req.tools]
        extra_body = {"usage": {"include": True}}  # ask OpenRouter for the real cost
        if req.reasoning:
            extra_body["reasoning"] = {"effort": req.reasoning}
        kw: dict = {
            "model": req.model, "stream": True, "stream_options": {"include_usage": True},
            "max_tokens": req.max_tokens or self.st.max_tokens,  # without it OpenRouter reserves credit for the model's max
            "temperature": config.TEMPERATURE, "extra_body": extra_body,
        }
        if req.cache_id:
            # Sticky routing: keep a chat's requests on the provider endpoint holding its cached
            # prompt (session_id, also sent as a header), with prompt_cache_key as the fallback.
            extra_body.update(session_id=req.cache_id, prompt_cache_key=req.cache_id)
            kw["extra_headers"] = {"x-session-id": req.cache_id}
        if tools:
            kw["tools"] = tools
            if tool_choice:
                kw["tool_choice"] = tool_choice
        return kw

    def _on_chunk(self, chunk, delta, acc: dict) -> None:
        raise_provider_error(chunk)
        if delta is None:
            return
        extra = getattr(delta, "model_extra", None) or {}
        merge_reasoning(acc.setdefault("details", []), extra.get("reasoning_details") or [])
        acc["cites"] = acc.get("cites", 0) + len(extra.get("annotations") or [])

    def _assistant_extra(self, acc: dict) -> dict:
        return {"reasoning_details": acc["details"]} if acc.get("details") else {}

    def _cost(self, usage_obj, req: Request) -> float:
        return float(getattr(usage_obj, "cost", 0) or 0)

    async def run_search(self, query: str) -> str | None:
        """Run one search through the cheap ':online' search model and return what it found."""
        if not (self.st.search and self.st.search_model):
            return None
        log.info("Search: %s", query)
        resp = await self.client.chat.completions.create(
            model=self.st.search_model, max_tokens=config.SEARCH_TOKENS, temperature=0.2,
            messages=[{"role": "system", "content": SEARCH_PROMPT}, {"role": "user", "content": query}])
        text = (resp.choices[0].message.content or "").strip()
        if not text:
            log.warning("Search model returned no text for %r (finish_reason=%s)",
                        query, resp.choices[0].finish_reason)
            return "The search returned no text (the search model may have run out of tokens)."
        return text

    async def credits(self) -> str | None:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(f"{BASE_URL}/credits",
                                 headers={"Authorization": f"Bearer {self.st.api_key}"})
            r.raise_for_status()
        d = r.json()["data"]
        total, used = float(d["total_credits"]), float(d["total_usage"])
        return f"OpenRouter: ${total - used:.2f} left (${used:.2f} used of ${total:.2f})"
