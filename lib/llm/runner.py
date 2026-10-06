"""The tool loop: ask the model, run the tool calls it hands back, repeat until it answers."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import re
import time

from openai import APIStatusError

from .. import config
from ..mcp.server import Registry
from ..textfmt import TOOL_SYNTAX_RE
from .base import Answer, Backend, Call, Request, Step, Usage

log = logging.getLogger("bot")

# Some models print a search call as text instead of making it; the queries are usually fine.
FAKE_CALL_RE = re.compile(
    r"<parameter=query>(.*?)(?:</parameter>|</tool_call>|<|$)", re.DOTALL | re.IGNORECASE)
NOISY_ARGS = {"consolidated", "report_combined", "grouping", "include_limited",
              "include_sales", "response_format"}
SAME_AS_EARLIER = "Same call as one already answered in this conversation; use that result."
WRITE_NOW = "Write the answer now, from the tool results above. Keep any thinking brief."
TOO_MANY_SEARCHES = "Skipped: too many searches at once. Use what you already have."


def call_query(arguments: str) -> str:
    """The search string in a tool call's JSON arguments, whatever the model named the field."""
    try:
        args = json.loads(arguments or "{}")
    except ValueError:
        return ""
    if not isinstance(args, dict):
        return ""
    for key in ("query", "q", "search_query", "keywords", "input"):
        if args.get(key):
            return str(args[key])
    return ""


def brief_args(args: dict) -> str:
    """Tool arguments for the log, short: "portfolio_id=684141 2026-09-04..2026-10-04"."""
    parts = []
    for k, v in args.items():
        if k in NOISY_ARGS or k in ("start_date", "end_date") or v is None or isinstance(v, bool):
            continue
        if isinstance(v, float) and v.is_integer():
            v = int(v)
        elif isinstance(v, list):
            v = ",".join(str(x) for x in v)
        parts.append(f"{k}={v}")
    if args.get("start_date") or args.get("end_date"):
        parts.append(f"{args.get('start_date', '')}..{args.get('end_date', '')}")
    return " ".join(parts)


async def _run_mcp(call: Call, registry: Registry) -> str:
    found = registry.lookup(call.name)
    if not found:
        return f"Error: tool {call.name} isn't available."
    server, tool = found
    try:
        args = json.loads(call.arguments or "{}")
        if not isinstance(args, dict):
            return f"Error: arguments for {tool} must be a JSON object."
        log.info("MCP %s.%s %s", server.label, tool, brief_args(args))
        out = await server.call(tool, args)
        # A tiny result is usually an empty list or an error: show it, so "why did it say
        # nothing came back?" can be answered from the log.
        log.info("MCP %s.%s -> %d chars%s", server.label, tool, len(out),
                 f": {out}" if len(out) <= 120 else "")
        return out
    except TimeoutError:
        return f"Error: {tool} timed out after {server.timeout:g}s."
    except Exception as e:
        log.exception("MCP call %s.%s failed", server.label, tool)
        return f"Error: {type(e).__name__}: {e}"


async def _run_search(call: Call, backend: Backend) -> str:
    query = call_query(call.arguments)
    if not query:
        return "Search call had no usable query (expected JSON with a 'query' field)."
    try:
        result = await backend.run_search(query)
    except Exception as e:
        return f"Search failed: {e}"
    return result if result is not None else f"Error: tool {call.name} isn't available."


async def run_calls(calls: list[Call], backend: Backend, registry: Registry, req: Request,
                    seen: dict[tuple[str, str], int], dedupe: bool) -> list[str]:
    """Reply to every call the model made: offered MCP tools, searches the provider handed
    back (capped), repeats of an earlier call (a short note when `dedupe`), or an error note."""
    offered = {t.name for t in req.tools}
    searches, jobs = 0, []
    for c in calls:
        key = (c.name, c.arguments)
        if dedupe and key in seen:
            jobs.append(_note(SAME_AS_EARLIER))
        elif c.name in offered:
            jobs.append(_run_mcp(c, registry))
        elif registry.lookup(c.name) or not ("search" in c.name.lower() or call_query(c.arguments)):
            jobs.append(_note(f"Error: tool {c.name} isn't available."))
        else:
            searches += 1
            jobs.append(_note(TOO_MANY_SEARCHES) if searches > config.MAX_SEARCHES
                        else _run_search(c, backend))
        seen[key] = seen.get(key, 0) + 1
    return list(await asyncio.gather(*jobs))


async def _note(text: str) -> str:
    return text


async def _step(backend: Backend, conv, req: Request, choice: str | None) -> Step:
    try:
        return await backend.step(conv, req, tool_choice=choice)
    except APIStatusError as e:
        if choice != "required" or e.status_code not in (400, 422):
            raise
        log.warning("Provider rejected tool_choice=required (%s); retrying without it", e)
        return await backend.step(conv, req, tool_choice=None)


async def _recover_fake_calls(step: Step, backend: Backend) -> str | None:
    """A model that prints a search call as text: run its queries and return the nudge message."""
    queries = [q.strip() for q in FAKE_CALL_RE.findall(step.text) if q.strip()][:config.MAX_SEARCHES]
    if not queries:
        return None
    results = await asyncio.gather(*(backend.run_search(q) for q in queries), return_exceptions=True)
    if all(r is None for r in results):
        return None  # this provider runs searches itself; there's nothing to recover
    parts = []
    for q, r in zip(queries, results, strict=True):
        if isinstance(r, Exception):
            log.warning("Search for %r failed: %s", q, r)
            r = f"Search failed: {r}"
        parts.append(f'Results for "{q}":\n{r}')
    return "\n\n".join(parts) + "\n\nAnswer the question now using these results."


async def run(backend: Backend, registry: Registry, req: Request, *, dedupe: bool = True) -> Answer:
    t0 = time.monotonic()
    conv = backend.start(req)
    total, rounds, tool_calls = Usage(), 0, 0
    seen: dict[tuple[str, str], int] = {}
    searched = 0
    while True:
        final = rounds >= config.MAX_TOOL_ROUNDS
        choice = "none" if final else ("required" if req.must_search and rounds == 0 else None)
        step = await _step(backend, conv, req, choice)
        rounds += 1
        total.add(step.usage)
        searched += step.searches
        u = step.usage
        log.info("Round %d: in %d (%d cached) out %d, %s", rounds, u.tokens_in, u.cached,
                 u.tokens_out, step.finish)
        if final:
            log.warning("Hit MAX_TOOL_ROUNDS (%d); made the model answer with what it has",
                        config.MAX_TOOL_ROUNDS)
            break
        if step.calls:
            tool_calls += len(step.calls)
            results = await run_calls(step.calls, backend, registry, req, seen, dedupe)
            backend.add_results(conv, step, results)
            continue
        nudge = await _recover_fake_calls(step, backend) if req.search else None
        if nudge:
            backend.add_user_message(conv, nudge)
            req = dataclasses.replace(req, search=False)  # recovery happens once
            continue
        break
    if req.must_search and not (tool_calls or searched):
        log.warning("This request needed a search, but the answer shows no sign of one")
    log.info("%.1fs, %d rounds, %d tools: in %d (%.0f%% cached) out %d, $%.4f",
             time.monotonic() - t0, rounds, tool_calls, total.tokens_in, total.cached_pct,
             total.tokens_out, total.cost)
    text = TOOL_SYNTAX_RE.sub("", step.text).strip()
    if not text and step.finish == "length" and tool_calls:
        # The model spent the whole cap thinking about the tool results. Keep them (answering
        # from memory without them invents things): ask again with more room and less thinking.
        log.warning("Hit the token cap before writing an answer; asking again with more room")
        backend.add_user_message(conv, WRITE_NOW)
        more = dataclasses.replace(req, max_tokens=(req.max_tokens or 3000) * 2, reasoning="low")
        step = await _step(backend, conv, more, "none")
        total.add(step.usage)
        log.info("Recovery round: in %d out %d, %s", step.usage.tokens_in, step.usage.tokens_out, step.finish)
        text = TOOL_SYNTAX_RE.sub("", step.text).strip()
    if not text:
        if step.finish == "length":
            raise RuntimeError("hit the token cap before writing an answer")
        raise RuntimeError(f"empty reply (finish_reason={step.finish})")
    return Answer(text, total, rounds, tool_calls)
