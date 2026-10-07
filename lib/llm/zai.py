"""z.ai (GLM) over chat completions: no web search, cost from a price table (usage carries none)."""

from __future__ import annotations

import logging

from openai import AsyncOpenAI

from .. import config
from ..config import Settings
from .base import Request
from .chat import ChatBackend

log = logging.getLogger("bot")

BASE_URL = "https://api.z.ai/api/paas/v4/"
# USD per million tokens (input, output). Cached input is billed at the input rate here, so the
# cost shown is an upper bound. A model missing from the table shows $0 (logged once).
PRICES = {"glm-5.3-flash": (0.15, 0.50)}


class ZaiBackend(ChatBackend):
    name = "zai"
    search_what = "the web"  # unused: search is off on z.ai

    def __init__(self, st: Settings, client: AsyncOpenAI | None = None):
        super().__init__(st, client or AsyncOpenAI(api_key=st.api_key, base_url=BASE_URL, timeout=180))
        self._warned = False

    def _kwargs(self, req: Request, tool_choice: str | None) -> dict:
        kw: dict = {
            "model": req.model, "stream": True, "stream_options": {"include_usage": True},
            "max_tokens": req.max_tokens or self.st.max_tokens, "temperature": config.TEMPERATURE,
        }
        if req.reasoning:
            kw["extra_body"] = {"reasoning_effort": req.reasoning}
        # z.ai only documents tool_choice "auto": "none" is done by leaving the tools out.
        if req.tools and tool_choice != "none":
            kw["tools"] = [{"type": "function", "function": {
                "name": t.name, "description": t.description, "parameters": t.parameters}}
                for t in req.tools]
        return kw

    def _cost(self, usage_obj, req: Request) -> float:
        price = PRICES.get(req.model)
        if price is None:
            if not self._warned:
                log.warning("No z.ai price for model %s; cost will show $0", req.model)
                self._warned = True
            return 0.0
        tokens_in = getattr(usage_obj, "prompt_tokens", 0) or 0
        tokens_out = getattr(usage_obj, "completion_tokens", 0) or 0
        return (tokens_in * price[0] + tokens_out * price[1]) / 1_000_000
