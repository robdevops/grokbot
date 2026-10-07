"""z.ai (GLM) over chat completions: search through z.ai's search API, cost from a price table."""

from __future__ import annotations

import logging

import httpx
from openai import AsyncOpenAI

from .. import config
from ..config import Settings
from .base import Request, Usage
from .chat import ChatBackend

log = logging.getLogger("bot")

BASE_URL = "https://api.z.ai/api/paas/v4/"
# USD per million tokens (input, cached input, output); a model missing here shows $0.
PRICES = {"glm-5.3-flash": (0.15, 0.03, 0.50)}
# z.ai models always think and accept only low, high or max. Low costs almost no reasoning
# tokens, so it is the default; the bot's REASONING values map onto z.ai's.
EFFORT = {"": "low", "low": "low", "medium": "high", "high": "high", "max": "max"}
# z.ai ignores its built-in web_search tool whenever function tools are present, so the model
# gets an ordinary function tool and the bot runs the search through z.ai's search API. That API
# accepts any recency value without complaint, so the tool schema fixes the list.
RECENCY = ("oneDay", "oneWeek", "oneMonth", "oneYear", "noLimit")
SEARCH_COUNT = 5
SEARCH_SNIPPET = 400  # characters kept of each result (they are normally 110-170)
SEARCH_COST = 0.01  # USD per search
SEARCH_TOOL = {"type": "function", "function": {
    "name": "web_search",
    "description": "Search the web for current information. Returns titles, snippets and links. "
                   "Use recency oneDay or oneWeek for prices and news, noLimit for background.",
    "parameters": {"type": "object", "required": ["query"], "properties": {
        "query": {"type": "string", "description": "Search query."},
        "recency": {"type": "string", "enum": list(RECENCY), "description": "How recent the pages must be."}}}}}


class ZaiBackend(ChatBackend):
    name = "zai"
    search_what = "the web"

    def __init__(self, st: Settings, client: AsyncOpenAI | None = None, http: httpx.AsyncClient | None = None):
        super().__init__(st, client or AsyncOpenAI(api_key=st.api_key, base_url=BASE_URL, timeout=180))
        self.http = http or httpx.AsyncClient(timeout=30)
        for model in {st.model, st.fast_model} - {""}:
            if model not in PRICES:
                log.warning("No z.ai price for model %s; its cost will show $0", model)

    def _kwargs(self, req: Request, tool_choice: str | None) -> dict:
        kw: dict = {
            "model": req.model, "stream": True, "stream_options": {"include_usage": True},
            "max_tokens": req.max_tokens or self.st.max_tokens, "temperature": config.TEMPERATURE,
            "extra_body": {"reasoning_effort": EFFORT.get(req.reasoning, "low")},
        }
        # z.ai ignores tool_choice (even "none"), so "answer now" is done by leaving the tools out.
        if tool_choice != "none":
            tools = [SEARCH_TOOL] if (req.search or req.must_search) else []
            tools += [{"type": "function", "function": {
                "name": t.name, "description": t.description, "parameters": t.parameters}}
                for t in req.tools]
            if tools:
                kw["tools"] = tools
        return kw

    def _cost(self, usage_obj, model: str) -> float:
        price = PRICES.get(model)
        if price is None:
            return 0.0
        tokens_in = getattr(usage_obj, "prompt_tokens", 0) or 0
        cached = getattr(getattr(usage_obj, "prompt_tokens_details", None), "cached_tokens", 0) or 0
        tokens_out = getattr(usage_obj, "completion_tokens", 0) or 0
        return ((tokens_in - cached) * price[0] + cached * price[1] + tokens_out * price[2]) / 1_000_000

    async def run_search(self, query: str, usage: Usage, args: dict) -> str | None:
        """Search through z.ai's search API: one line per result with its site, date, snippet and link."""
        if not self.st.search:
            return None
        recency = args.get("recency")
        log.info("Search: %s (%s)", query, recency)
        r = await self.http.post(
            BASE_URL + "web_search", headers={"Authorization": f"Bearer {self.st.api_key}"},
            json={"search_engine": "search-prime", "search_query": query, "count": SEARCH_COUNT,
                  "search_recency_filter": recency if recency in RECENCY else "noLimit"})
        r.raise_for_status()
        usage.cost += SEARCH_COST
        body = r.json()
        results = body.get("search_result") or []
        if not results:
            intent = (body.get("search_intent") or [{}])[0]
            return (f"The search returned no results (intent {intent.get('intent', '?')}, "
                    f"keywords: {intent.get('keywords', query)}). Try other words or a wider recency.")
        lines = []
        for x in results:
            source = ", ".join(filter(None, [x.get("media"), x.get("publish_date")]))
            lines.append(f"- {x.get('title', '')}{f' [{source}]' if source else ''}: "
                         f"{(x.get('content') or '')[:SEARCH_SNIPPET]} ({x.get('link', '')})")
        return "\n".join(lines)
