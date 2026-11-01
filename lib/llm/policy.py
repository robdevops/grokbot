"""The one retry ladder, shared by both providers."""

from __future__ import annotations

import dataclasses
import logging

from openai import APIStatusError

from ..mcp.server import Registry
from .base import Answer, Backend, Request, is_busy
from .runner import run

log = logging.getLogger("bot")


async def ask(backend: Backend, registry: Registry, req: Request, *, dedupe: bool = True) -> Answer:
    """Run the request. If it fails in a way the tools could cause (a provider error, an empty
    reply, a 400/422), retry once without tools. Never for 401/402/429/5xx, and never when a
    search or a tool call is required (answering one from memory would invent news or data).
    If the retry fails too, the original error is the one raised."""
    try:
        return await run(backend, registry, req, dedupe=dedupe)
    except (APIStatusError, RuntimeError) as first:
        retriable = (isinstance(first, RuntimeError) and not is_busy(first)) or (
            isinstance(first, APIStatusError) and first.status_code in (400, 422))
        if req.must_search or req.must_call or not retriable or not (req.search or req.tools):
            raise
        log.warning("Retrying without tools: %s", first)
        bare = dataclasses.replace(
            req, tools=[], search=False, system=req.no_tools_system or req.system)
        try:
            return await run(backend, registry, bare, dedupe=dedupe)
        except Exception:
            log.exception("The retry without tools failed too")
            raise first from None
