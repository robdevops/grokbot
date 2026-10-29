"""PERIOD_BACKFILL: fetch finished days, weeks, months, quarters and years of selected Sharesight portfolios into
the period cache in the background, politely (two calls at a time, never while a user's request is using the server)."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import date, datetime, timedelta

from ... import config
from ...context import Ctx
from . import sharesight
from .rows import fetch
from .windows import Window, key, parse_date, period_windows

log = logging.getLogger("bot")

ORDER = ("month", "quarter", "year", "week", "day")  # what pays off first: few calls, most questions
BUSY_POLL = 2.0
RECHECK = 6 * 3600  # seconds between passes (a pass fetches only what newly finished)
RETRY_AFTER_FAILURE = 600  # seconds before trying again after a pass stopped by MAX_FAILS failures in a row
WORKERS = 2  # fetches at a time (needs max_concurrent >= 2 on the Sharesight server in mcp_servers.json)
MAX_FAILS = 5  # consecutive failed fetches that end a pass (Sharesight down or throttling)
PROGRESS_EVERY = 50
WAIT_LOG = 30.0  # seconds held back by a user's request before the journal says so
SAMPLE_NODATA = 3  # "no data" windows logged individually per step
SLOW_FETCH = 20.0  # seconds one Sharesight fetch may take before the journal says so

_wake: asyncio.Event | None = None  # set to start a pass now (after a flush)
BAD: set[tuple[int, str, str]] = set()  # windows Sharesight had no data for; not retried until restart or reset()


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


def wanted(first: date, today: date, periods: tuple[str, ...] = ORDER) -> list[Window]:
    """Finished windows of these period types from `first` up to yesterday, in the order worth fetching them."""
    yesterday = today - timedelta(days=1)
    return [w for p in periods for w in period_windows(p, first, yesterday) if natural_end(p, w)] \
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


async def fill_portfolio(ctx: Ctx, server, pid: int, name: str, todo: list[Window],
                         streak: list[int] | None = None) -> tuple[int, bool]:
    """Fetch these windows with WORKERS fetches at a time. Returns (how many were stored, whether the pass
    was stopped by MAX_FAILS transient failures in a row). A window Sharesight has no data for is skipped
    (until the next restart or flush) without counting as a failure: early days before any trade are normal."""
    pending = iter(todo)  # shared: each window goes to exactly one worker
    streak = streak if streak is not None else [0]  # failures in a row; shared across a pass's steps
    stored = done = nodata = transient = 0
    stopped = False

    async def worker() -> None:
        nonlocal stored, done, nodata, transient, stopped
        for w in pending:
            waited, said = time.monotonic(), False
            while (users := server.user_calls()) and not stopped:  # a user's request comes first
                await asyncio.sleep(BUSY_POLL)
                if not said and time.monotonic() - waited >= WAIT_LOG:
                    said = True
                    log.debug("Period backfill: %s held back for %.0f s by: %s", name, WAIT_LOG, "; ".join(users[:3]))
            if stopped:
                return
            began = time.monotonic()
            got = await fetch(server, pid, w[1], w[2], background=True)
            if time.monotonic() - began >= SLOW_FETCH:
                log.warning("Period backfill: %s %s..%s was slow: %.0f s", name, w[1], w[2], time.monotonic() - began)
            if got.row is not None:
                streak[0] = 0
                await asyncio.to_thread(ctx.store.period_put, [got.row])
                stored += 1
            elif got.kind == "nodata":
                BAD.add((pid, *key(w)))
                nodata += 1
                if nodata <= SAMPLE_NODATA:  # enough of them to see what Sharesight says, not one line per empty day
                    log.debug("Period backfill: %s %s..%s has no data: %s", name, w[1], w[2], got.text[:150])
            else:  # a blip says nothing about the window itself, so it is tried again next pass
                streak[0] += 1
                transient += 1
                log.warning("Period backfill: %s %s..%s failed: %s", name, w[1], w[2], got.text[:150])
                if streak[0] >= MAX_FAILS and not stopped:
                    stopped = True
                    log.warning("Period backfill: %d fetches in a row failed; stopping this pass", streak[0])
            done += 1
            if done % PROGRESS_EVERY == 0:
                log.debug("Period backfill: %s %d/%d", name, done, len(todo))

    await asyncio.gather(*(worker() for _ in range(WORKERS)))
    log.info("Period backfill: %s: %d stored, %d had no data, %d failed", name, stored, nodata, transient)
    return stored, stopped


async def fill_pass(ctx: Ctx, server, portfolios: list[tuple[int, str, date]], today: date) -> tuple[int, bool]:
    """One pass: fetch every wanted window that isn't cached yet, one period type at a time across all the
    portfolios (everyone's months, then quarters, years, weeks, and the many days last). Returns (how many
    were stored, whether failures in a row cut it short)."""
    cached = {}
    for pid, _, first in portfolios:
        cached[pid] = set(await asyncio.to_thread(ctx.store.period_get, pid, first.isoformat(), today.isoformat()))
    stored, streak = 0, [0]
    for period in ORDER:
        for pid, name, first in portfolios:
            todo = [w for w in wanted(first, today, (period,))
                    if key(w) not in cached[pid] and (pid, *key(w)) not in BAD]
            if not todo:
                continue
            log.debug("Period backfill: %d %ss to fetch for %s", len(todo), period, name)
            n, stopped = await fill_portfolio(ctx, server, pid, f"{name} {period}s", todo, streak)
            stored += n
            if stopped:
                return stored, True
    return stored, False


async def fill_once(ctx: Ctx, server, portfolios: list[tuple[int, str, date]], today: date) -> int:
    return (await fill_pass(ctx, server, portfolios, today))[0]


async def loop(ctx: Ctx) -> None:
    await ctx.registry.wait_started(config.MCP_STARTUP_WAIT)
    warned = False
    while True:
        wait = RECHECK
        server = sharesight(ctx)
        if server is None:  # on by default, so a bot without Sharesight must not nag every 10 minutes
            (log.debug if warned else log.warning)("Period backfill: Sharesight isn't connected; trying again in 10 minutes")
            warned = True
            wait = 600
        else:
            try:
                portfolios = selected(ctx.st, await server.call("list_portfolios", {}))
                if not portfolios:
                    log.warning("Period backfill: no Sharesight portfolio matches PERIOD_BACKFILL")
                n, stopped = await fill_pass(ctx, server, portfolios, datetime.now(ctx.st.tz).date())
                log.info("Period backfill: pass %s, %d new windows cached%s", "stopped" if stopped else "done", n,
                         f"; trying again in {RETRY_AFTER_FAILURE // 60} minutes" if stopped else "")
                if stopped:
                    wait = RETRY_AFTER_FAILURE
            except Exception:
                log.exception("Period backfill pass failed")
        try:
            await asyncio.wait_for(wake_event().wait(), wait)
        except TimeoutError:
            pass
        wake_event().clear()
