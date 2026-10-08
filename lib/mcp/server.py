"""One MCP server kept connected for the life of the bot, and the registry of all of them."""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
import re
import tempfile
import textwrap
import time
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import date, timedelta

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

from .results import slim_result
from .schema import ToolDef, compact_description, compact_schema, expand_env, looks_read_only

log = logging.getLogger("bot")

START_TIMEOUT = 180  # generous: the first npx run downloads the package


@dataclass
class _Entry:
    """A cached result, with what it takes to fetch it again."""

    at: float
    out: str
    tool: str
    args: dict
    used: bool = True  # someone asked for it since it was fetched (a warm entry is only kept warm while used)


class MCPServer:
    """The connection lives in its own task because the MCP SDK's context managers must be
    entered and exited in the same task."""

    def __init__(self, label: str, cfg: dict, *, timeout: float, max_output: int,
                 on_down: Callable[[MCPServer], Awaitable[None]] | None = None):
        self.label = re.sub(r"[^A-Za-z0-9_-]", "_", label)
        self.cfg = cfg
        self.description = cfg.get("description", "")
        self.timeout, self.max_output, self.on_down = timeout, max_output, on_down
        self.session: ClientSession | None = None
        self.error: str | None = None  # why the server is down, if it is
        self.tools: list[ToolDef] = []
        self.fn_names: dict[str, str] = {}  # function name sent to the model -> MCP tool name
        # Cap simultaneous calls: a 20-stock request fired all at once gets rate-limited.
        self._sem = asyncio.Semaphore(int(cfg.get("max_concurrent", 4)))
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._ttl = float(cfg.get("cache_ttl", 0))  # seconds identical calls are shared; 0 = off
        self._cache: dict[str, _Entry] = {}
        self._inflight: dict[str, asyncio.Task] = {}
        # Tools whose cached results are refetched just before they expire, so asking never waits on a cold fetch:
        # {tool: longest start_date..end_date window in days that stays warm, or None for any call}.
        warm = cfg.get("keep_warm", [])
        self._warm: dict[str, int | None] = ({t: None for t in warm} if isinstance(warm, list) else dict(warm)) \
            if self._ttl > 0 else {}
        self._warm_every = min(30.0, max(1.0, self._ttl / 10))  # seconds between checks, and how early to refetch

    def start(self) -> None:
        """Connect in the background; tools appear once the server is up. Idempotent."""
        if self._task and not self._task.done():
            return
        self._ready.clear()
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name=f"mcp-{self.label}")

    @property
    def starting(self) -> bool:
        """Started but not yet connected (or failed)."""
        return self._task is not None and not self._ready.is_set()

    async def wait_ready(self, timeout: float = START_TIMEOUT) -> bool:
        try:
            await asyncio.wait_for(self._ready.wait(), timeout)
        except TimeoutError:
            log.warning("MCP %s is slow to start; its tools appear once it's up", self.label)
        return self.session is not None

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task

    async def _run(self) -> None:
        # stderr goes to a temp file so that when the server dies we can report what it said.
        errlog = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
        try:
            async with AsyncExitStack() as stack:
                if "url" in self.cfg:  # remote server over streamable HTTP
                    read, write, _ = await stack.enter_async_context(
                        streamablehttp_client(self.cfg["url"],
                                              headers=expand_env(self.cfg.get("headers"), self.label)))
                else:  # local server over stdio
                    params = StdioServerParameters(
                        command=self.cfg["command"], args=self.cfg.get("args", []),
                        env=expand_env(self.cfg.get("env"), self.label))
                    read, write = await stack.enter_async_context(stdio_client(params, errlog=errlog))
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                await self._load_tools(session)
                self.session, self.error = session, None
                self._ready.set()
                warmer = asyncio.create_task(self._warm_loop()) if self._warm else None
                try:
                    await self._stop.wait()
                finally:
                    if warmer:
                        warmer.cancel()
        except Exception as e:
            self.error = describe_failure(e, errlog)
            log.error("MCP server %s failed: %s", self.label, self.error, exc_info=True)
            if self.on_down and not self._stop.is_set():
                await self.on_down(self)
        finally:
            self.session = None
            errlog.close()
            self._ready.set()

    async def _load_tools(self, session: ClientSession) -> None:
        listed = (await session.list_tools()).tools
        self.tools, self.fn_names = [], {}  # a restart must not append every tool twice
        allow = set(self.cfg.get("allowed_tools", []))
        blocked = set(self.cfg.get("blocked_tools", []))
        for t in sorted(listed, key=lambda t: t.name):  # stable order keeps the prompt cacheable
            if t.name in blocked or not (t.name in allow if allow else looks_read_only(t)):
                continue
            fn = f"{self.label}__{t.name}"[:64]
            self.fn_names[fn] = t.name
            self.tools.append(ToolDef(
                fn, compact_description(t.description or ""),
                compact_schema(t.inputSchema or {"type": "object", "properties": {}},
                               hide=frozenset(self.cfg.get("hide_params", [])))))
        self._warn_unknown_names(listed, allow | blocked | set(self._warm))
        enabled = sorted(self.fn_names.values())
        skipped = sorted(t.name for t in listed if t.name not in enabled)
        log_names(f"MCP {self.label}: {len(enabled)} tools:", enabled)
        if skipped:
            log_names(f"MCP {self.label}: {len(skipped)} skipped:", skipped)

    def _warn_unknown_names(self, listed, configured: set[str]) -> None:
        """A blocked/allowed name that matches no tool does nothing; say so (and what was meant)."""
        real = [t.name for t in listed]
        for name in sorted(configured - set(real)):
            close = difflib.get_close_matches(name, real, n=1, cutoff=0.5) \
                or [r for r in real if name in r]
            hint = f" (did you mean '{close[0]}'?)" if close else ""
            log.warning("MCP %s: blocked_tools/allowed_tools entry '%s' matches no tool%s",
                        self.label, name, hint)

    async def call(self, tool: str, args: dict) -> str:
        """Run one tool and return its (slimmed, size-capped) text. Identical concurrent or
        recent calls share one result when cache_ttl is set."""
        if not self.session:
            return f"Error: the {self.label} data source isn't connected right now."
        if self._ttl <= 0:
            return await self._call(tool, args)
        key = f"{tool}:{json.dumps(args, sort_keys=True, default=str)}"
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit.at < self._ttl:
            hit.used = True
            return hit.out
        return await self._fetch(key, tool, args)

    async def _fetch(self, key: str, tool: str, args: dict) -> str:
        """Fetch and cache one result; concurrent fetches of the same call share one request."""
        task = self._inflight.get(key)
        if task is None:
            task = self._inflight[key] = asyncio.create_task(self._call(tool, args))
        try:
            out = await asyncio.shield(task)
        finally:
            if task.done():
                self._inflight.pop(key, None)
        if not out.startswith(("Tool error", "Error")):
            now = time.monotonic()
            # Expired entries go, except warm ones: _warm_once decides about those.
            self._cache = {k: v for k, v in self._cache.items() if now - v.at < self._ttl or self._warmable(v.tool, v.args)}
            self._cache[key] = _Entry(now, out, tool, args)
        return out

    def _warmable(self, tool: str, args: dict) -> bool:
        """Is this call kept warm? A tool with a day limit only when it asks for a window of at most that many days."""
        if tool not in self._warm:
            return False
        days = self._warm[tool]
        if days is None:
            return True
        try:
            window = date.fromisoformat(args["end_date"]) - date.fromisoformat(args["start_date"])
        except (KeyError, ValueError, TypeError):
            return False  # undated or malformed: the tool's default window is a long one
        return timedelta(0) <= window <= timedelta(days=days)

    async def _warm_loop(self) -> None:
        while True:
            await asyncio.sleep(self._warm_every)
            try:
                await self._warm_once()
            except Exception:
                log.exception("MCP %s cache refresh failed", self.label)

    async def _warm_once(self) -> None:
        """Refetch warm results about to expire if someone asked for them since the last fetch; drop the idle ones,
        so a query nobody repeats stops costing API calls."""
        now = time.monotonic()
        for key, entry in list(self._cache.items()):
            if not self._warmable(entry.tool, entry.args) or now - entry.at < self._ttl - self._warm_every:
                continue
            if not entry.used:
                self._cache.pop(key, None)
                continue
            try:
                await self._fetch(key, entry.tool, entry.args)
            except Exception as e:
                log.warning("MCP %s.%s refresh failed: %s", self.label, entry.tool, e)
            fresh = self._cache.get(key)
            if fresh is None or fresh is entry:  # the refetch failed or came back as an error: let it go
                self._cache.pop(key, None)
            else:
                fresh.used = False

    async def _call(self, tool: str, args: dict) -> str:
        holdings_only = bool(self.cfg.get("current_holdings_only"))
        if holdings_only and tool == "get_performance_report":
            args = {**args, "include_sales": False}  # Sharesight's own switch for sold holdings
        async with self._sem:
            result = await asyncio.wait_for(self.session.call_tool(tool, args), self.timeout)
        parts = [c.text if c.type == "text" else f"[{c.type} content omitted]" for c in result.content]
        if not parts and getattr(result, "structuredContent", None):
            parts.append(json.dumps(result.structuredContent))
        out = "\n".join(parts) or "(empty result)"
        if result.isError:
            out = "Tool error: " + out
        else:
            out = slim_result(out, self.cfg.get("slim", self.label), drop_closed=holdings_only)
        if len(out) > self.max_output:
            log.warning("MCP %s.%s result truncated (%d chars)", self.label, tool, len(out))
            out = out[:self.max_output] + f"\n...[truncated; {len(out)} chars total]"
        return out


class Registry:
    """All configured MCP servers."""

    def __init__(self, servers: list[MCPServer]):
        self.servers = {s.label: s for s in servers}

    @classmethod
    def load(cls, path: str, *, timeout: float, max_output: int,
             on_down: Callable[[MCPServer], Awaitable[None]] | None = None) -> Registry:
        """Servers from a JSON file ({"mcpServers": {...}} or Claude Desktop style); a missing
        file means no servers."""
        try:
            with open(path) as f:
                cfg = json.load(f)
        except FileNotFoundError:
            return cls([])
        cfg = cfg.get("mcpServers", cfg)
        return cls([MCPServer(label, c, timeout=timeout, max_output=max_output, on_down=on_down)
                    for label, c in cfg.items() if not c.get("disabled")])

    def start(self) -> None:
        for s in self.servers.values():
            s.start()

    async def stop(self) -> None:
        await asyncio.gather(*(s.stop() for s in self.servers.values()), return_exceptions=True)

    async def wait_started(self, timeout: float) -> None:
        """Hold a request until servers still connecting are up (or `timeout`), so a message that
        arrives just after a restart doesn't get answered without its tools."""
        starting = [s for s in self.servers.values() if s.starting]
        if starting:
            log.info("Waiting for MCP: %s", ", ".join(s.label for s in starting))
            await asyncio.gather(*(s.wait_ready(timeout) for s in starting))

    def up(self) -> list[MCPServer]:
        return [s for s in self.servers.values() if s.session]

    def down(self) -> list[MCPServer]:
        return [s for s in self.servers.values() if s.error]

    def lookup(self, fn_name: str) -> tuple[MCPServer, str] | None:
        for s in self.servers.values():
            if fn_name in s.fn_names:
                return s, s.fn_names[fn_name]
        return None


def log_names(head: str, names: list[str], width: int = 80) -> None:
    """Log a list of tool names on lines short enough not to wrap in a terminal or journal."""
    lines = textwrap.wrap(", ".join(names), width - len(head))
    for i, line in enumerate(lines):
        log.info("%s %s", head if i == 0 else " " * len(head), line)


def _leaf_errors(e: BaseException) -> list[BaseException]:
    """The real errors inside the nested ExceptionGroups the MCP SDK raises."""
    if isinstance(e, BaseExceptionGroup):
        return [leaf for sub in e.exceptions for leaf in _leaf_errors(sub)]
    return [e]


def describe_failure(e: BaseException, errlog) -> str:
    parts = [f"{type(x).__name__}: {x}" for x in _leaf_errors(e)]
    try:
        errlog.flush()
        errlog.seek(0)
        tail = errlog.read()[-800:].strip()
        if tail:
            parts.append(tail)
    except Exception:
        pass
    return "\n".join(parts)[:1500]
