"""z.ai (GLM) over chat completions: web search through z.ai's search API, cost from a price table."""

from __future__ import annotations

import logging

import httpx
from openai import AsyncOpenAI

from .. import config
from ..config import Settings
from .base import Request
from .chat import ChatBackend

log = logging.getLogger("bot")

BASE_URL = "https://api.z.ai/api/paas/v4/"
# USD per million tokens (input, cached input, output). A model missing from the table shows $0
# (logged once).
PRICES = {"glm-5.3-flash": (0.15, 0.03, 0.50)}
# z.ai models always think and accept only low, high or max. Low costs almost no reasoning
# tokens, so it is the default; the bot's REASONING values map onto z.ai's.
EFFORT = {"": "low", "low": "low", "medium": "high", "high": "high", "max": "max"}
# z.ai drops its built-in web_search tool when function tools are in the request, so the model
# gets an ordinary function tool and the bot runs the search through z.ai's search API.
SEARCH_COUNT = 5
SEARCH_SNIPPET = 400  # characters kept of each result
SEARCH_TOOL = {"type": "function", "function": {
    "name": "web_search",
    "description": "Search the web for current information. Returns result titles, links and snippets.",
    "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "Search query."}},
                   "required": ["query"]}}}


class ZaiBackend(ChatBackend):
    name = "zai"
    search_what = "the web"

    def __init__(self, st: Settings, client: AsyncOpenAI | None = None, http: httpx.AsyncClient | None = None):
        super().__init__(st, client or AsyncOpenAI(api_key=st.api_key, base_url=BASE_URL, timeout=180))
        self.http = http or httpx.AsyncClient(timeout=30)
        self._warned = False

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

    def _cost(self, usage_obj, req: Request) -> float:
        price = PRICES.get(req.model)
        if price is None:
            if not self._warned:
                log.warning("No z.ai price for model %s; cost will show $0", req.model)
                self._warned = True
            return 0.0
        tokens_in = getattr(usage_obj, "prompt_tokens", 0) or 0
        cached = getattr(getattr(usage_obj, "prompt_tokens_details", None), "cached_tokens", 0) or 0
        tokens_out = getattr(usage_obj, "completion_tokens", 0) or 0
        return ((tokens_in - cached) * price[0] + cached * price[1] + tokens_out * price[2]) / 1_000_000

    async def run_search(self, query: str) -> str | None:
        """Search through z.ai's search API; each result is one line of title, link and snippet."""
        if not self.st.search:
            return None
        log.info("Search: %s", query)
        r = await self.http.post(
            BASE_URL + "web_search", headers={"Authorization": f"Bearer {self.st.api_key}"},
            json={"search_engine": "search-prime", "search_query": query, "count": SEARCH_COUNT})
        r.raise_for_status()
        results = r.json().get("search_result") or []
        if not results:
            return "The search returned no results."
        return "\n".join(
            f"- {x.get('title', '')} ({x.get('link', '')}"
            f"{', ' + x['publish_date'] if x.get('publish_date') else ''}): "
            f"{(x.get('content') or '')[:SEARCH_SNIPPET]}" for x in results)
