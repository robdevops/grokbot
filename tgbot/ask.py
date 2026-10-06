"""One entry point for "ask the model": routing, prompts, usage ledger, NEEDS_TOOLS escalation."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import re

from .context import Ctx
from .llm import policy
from .llm.base import Answer, Request
from .llm.gate import NEEDS_TOOLS, Route, wants_tools
from .prompts import no_tools_system, system_prompt

log = logging.getLogger("bot")


def cache_id(bot_name: str, key: str | None) -> str | None:
    """Keeps a chat's requests on the server holding its cached prompt. Goes into HTTP headers,
    so ASCII only (bot names can hold anything) and short."""
    if not key:
        return None
    return f"{re.sub(r'[^A-Za-z0-9_.-]', '', bot_name) or 'bot'}-{key}"[:128]


def _hide_needs_tools(on_text):
    """Don't stream the NEEDS_TOOLS marker into a draft."""
    if on_text is None:
        return None

    def forward(text: str) -> None:
        head = text.strip().upper()
        if not (head.startswith(NEEDS_TOOLS) or NEEDS_TOOLS.startswith(head)):
            on_text(text)

    return forward


async def ask(ctx: Ctx, parts: list[dict], route: Route, *, must_search: bool = False,
              cache_key: str | None = None, on_text=None, kind: str = "chat",
              chat_id: int = 0) -> Answer:
    """Ask the configured provider and record the usage. `route` says which tools to offer;
    must_search forces a search (and offers nothing else). A message the gate gave fewer tools
    than exist that gets back NEEDS_TOOLS is asked again with everything on."""
    st = ctx.st
    if must_search:
        if not st.search:
            raise RuntimeError("search is disabled, but this request needs it")
        route = Route([], True)
    saver = st.token_saver
    req = Request(
        system=system_prompt(ctx.first_name, route, ctx.backend.search_what, saver=saver),
        parts=parts,
        model=(st.fast_model or st.model) if route.simple else st.model,
        tools=[t for s in route.servers for t in s.tools],
        search=route.search, must_search=must_search, reasoning=st.reasoning,
        cache_id=cache_id(ctx.first_name, cache_key),
        on_text=_hide_needs_tools(on_text) if (route.simple or route.partial) else on_text,
        no_tools_system=no_tools_system(ctx.first_name, saver=saver),
    )
    answer = await policy.ask(ctx.backend, ctx.registry, req)
    await _record(ctx, req.model, kind, chat_id, answer)
    if (route.simple or route.partial) and wants_tools(answer.text):
        log.info("Gate offered fewer tools, the model asked for more: asking again with everything on")
        full = Route(ctx.registry.up(), st.search)
        req = dataclasses.replace(
            req, system=system_prompt(ctx.first_name, full, ctx.backend.search_what, saver=saver),
            model=st.model, tools=[t for s in full.servers for t in s.tools], search=full.search,
            on_text=on_text)
        answer = await policy.ask(ctx.backend, ctx.registry, req)
        await _record(ctx, req.model, kind, chat_id, answer)
    return answer


async def _record(ctx: Ctx, model: str, kind: str, chat_id: int, a: Answer) -> None:
    u = a.usage
    try:
        await asyncio.to_thread(ctx.store.add_usage, chat_id, model, kind, a.rounds,
                                u.tokens_in, u.cached, u.tokens_out, u.cost)
    except Exception:
        log.exception("Couldn't record usage")
