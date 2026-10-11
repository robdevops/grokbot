"""PERIOD_BACKFILL: fetch finished days, weeks, months, quarters and years of selected Sharesight portfolios into
the period cache in the background, politely (one call at a time, never while a user's request is using the server)."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, timedelta

from ... import config
from ...context import Ctx
from . import sharesight
from .rows import fetch_row
from .windows import Window, key, parse_date, period_windows

log = logging.getLogger("bot")

ORDER = ("month", "quarter", "year", "week", "day")  # what pays off first: few calls, most questions
BUSY_POLL = 2.0
RECHECK = 6 * 3600  # seconds between passes (a pass fetches only what newly finished)
MAX_FAILS = 5  # consecutive failed fetches that end a pass (Sharesight down or throttling)
PROGRESS_EVERY = 100

_wake: asyncio.Event | None = None  # set to start a pass now (after a flush)
BAD: set[tuple[int, str, str]] = set()  # windows Sharesight had nothing for; not retried until reset()


def wake_event() -> asyncio.Event:
    global _wake
    if _wake is None:
        _wake = asyncio.Event()
    return _wake


def reset() -> None:
    """After a cache flush: forget what failed and run a pass now."""
    BAD.clear()
    wake_event().set()


def natural_end(period: str, w: Window) -> bool:
    """Does the window end where its period ends (so it is a finished, canonical window)?"""
    end, nxt = w[2], w[2] + timedelta(days=1)
    return {"day": True, "week": end.weekday() == 6, "month": nxt.day == 1,
            "quarter": nxt.day == 1 and nxt.month in (1, 4, 7, 10),
            "year": end.month == 12 and end.day == 31}[period]


def wanted(first: date, today: date) -> list[Window]:
    """Finished windows from `first` up to yesterday, in the order worth fetching them."""
    yesterday = today - timedelta(days=1)
    return [w for p in ORDER for w in period_windows(p, first, yesterday) if natural_end(p, w)] \
        if first <= yesterday else []


def selected(st: config.Settings, listing: str) -> list[tuple[int, str, date]]:
    """(id, name, inception) of the portfolios PERIOD_BACKFILL names (or all of them)."""
    try:
        portfolios = json.loads(listing).get("portfolios") or []
    except (ValueError, AttributeError):
        return []
    out = []
    for p in portfolios:
        if not isinstance(p, dict) or not isinstance(p.get("id"), int):
            continue
        name = str(p.get("name") or "")
        start = parse_date(p.get("inception_date") or "")
        if start and ("all" in st.period_backfill or name.lower() in st.period_backfill):
            out.append((p["id"], name, start))
    return out


async def fill_once(ctx: Ctx, server, portfolios: list[tuple[int, str, date]], today: date) -> int:
    """One pass: fetch every wanted window that isn't cached yet. Returns how many were stored."""
    stored = 0
    for pid, name, first in portfolios:
        windows = wanted(first, today)
        have = await asyncio.to_thread(ctx.store.period_get, pid, first.isoformat(), today.isoformat())
        todo = [w for w in windows if key(w) not in have and (pid, *key(w)) not in BAD]
        if todo:
            log.info("Period backfill: %d windows to fetch for %s", len(todo), name)
        fails = 0
        for i, w in enumerate(todo, 1):
            while server.busy:  # a user's request comes first
                await asyncio.sleep(BUSY_POLL)
            row = await fetch_row(server, pid, w[1], w[2])
            if row is None:
                BAD.add((pid, *key(w)))
                fails += 1
                if fails >= MAX_FAILS:
                    log.warning("Period backfill: %d fetches in a row failed; stopping this pass", fails)
                    return stored
            else:
                fails = 0
                await asyncio.to_thread(ctx.store.period_put, [row])
                stored += 1
            if i % PROGRESS_EVERY == 0:
                log.info("Period backfill: %s %d/%d", name, i, len(todo))
    return stored


async def loop(ctx: Ctx) -> None:
    await ctx.registry.wait_started(config.MCP_STARTUP_WAIT)
    while True:
        wait = RECHECK
        server = sharesight(ctx)
        if server is None:
            log.warning("Period backfill: Sharesight isn't connected; trying again in 10 minutes")
            wait = 600
        else:
            try:
                portfolios = selected(ctx.st, await server.call("list_portfolios", {}))
                if not portfolios:
                    log.warning("Period backfill: no Sharesight portfolio matches PERIOD_BACKFILL")
                n = await fill_once(ctx, server, portfolios, datetime.now(ctx.st.tz).date())
                log.info("Period backfill: pass done, %d new windows cached", n)
            except Exception:
                log.exception("Period backfill pass failed")
        try:
            await asyncio.wait_for(wake_event().wait(), wait)
        except TimeoutError:
            pass
        wake_event().clear()
