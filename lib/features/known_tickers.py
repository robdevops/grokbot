"""Tickers a reply may name bare and still get a link: the S&P 500, the NASDAQ-100 and the US Sharesight holdings.

Refreshed in the background once a day and kept in the kv table, so a restart has them at once; replies only read
tickers.KNOWN and never wait for a fetch."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from html.parser import HTMLParser

import httpx

from .. import config, tickers
from ..context import Ctx
from ..holdings import code_and_market, current_holdings

log = logging.getLogger("bot")
KV_KEY = "known_tickers"
SYMBOL_RE = re.compile(r"^[A-Z]{1,5}(?:-[A-Z])?$")
HEADERS = {"User-Agent": "grokbot/1.0 (Telegram stock-chat bot; daily index list)"}


class _Tables(HTMLParser):
    """Every <table> on a page as {"id": ..., "rows": [[cell text, ...], ...]}, nested ones included."""

    def __init__(self) -> None:
        super().__init__()
        self.tables: list[dict] = []
        self._open: list[dict] = []
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            table = {"id": dict(attrs).get("id") or "", "rows": []}
            self.tables.append(table)
            self._open.append(table)
        elif tag == "tr" and self._open:
            self._open[-1]["rows"].append([])
        elif tag in ("td", "th") and self._open and self._open[-1]["rows"]:
            self._cell = []
            self._open[-1]["rows"][-1].append(self._cell)

    def handle_endtag(self, tag):
        if tag == "table" and self._open:
            self._open.pop()
        elif tag in ("td", "th"):
            self._cell = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def symbols_from_html(page: str) -> list[str]:
    """The ticker column of a Wikipedia index page: the table with id "constituents", else the first table with a
    "Symbol" or "Ticker" header. Yahoo writes share classes with a hyphen (BRK.B -> BRK-B)."""
    parser = _Tables()
    parser.feed(page)
    tables = sorted(parser.tables, key=lambda t: t["id"] != "constituents")
    for table in tables:
        rows = [["".join(cell).strip() for cell in row] for row in table["rows"]]
        if not rows:
            continue
        header = [h.lower() for h in rows[0]]
        col = next((i for i, h in enumerate(header) if h in ("symbol", "ticker", "ticker symbol")), None)
        if col is None:
            continue
        found = [row[col].replace(".", "-") for row in rows[1:] if len(row) > col]
        return [s for s in found if SYMBOL_RE.match(s)]
    return []


async def fetch_index(client: httpx.AsyncClient, name: str, url: str, low: int, high: int) -> list[str] | None:
    """An index's tickers, or None if the page couldn't be read or doesn't look right."""
    try:
        r = await client.get(url, headers=HEADERS, follow_redirects=True)
        r.raise_for_status()
    except httpx.HTTPError as e:
        log.warning("Known tickers: couldn't fetch the %s list: %s", name, e)
        return None
    found = symbols_from_html(r.text)
    if not low <= len(found) <= high:
        log.warning("Known tickers: the %s page gave %d tickers (expected %d-%d); keeping the last list",
                    name, len(found), low, high)
        return None
    return found


async def us_holdings(ctx: Ctx) -> list[str] | None:
    """The codes of the US-listed Sharesight holdings, or None if Sharesight isn't up or isn't set up."""
    server = next((s for s in ctx.registry.up() if s.cfg.get("gate") == "portfolio"), None)
    if server is None or not ctx.st.portfolio_names:
        return None
    try:
        held = await current_holdings(server, sorted(ctx.st.portfolio_names))
    except Exception as e:  # the MCP server's own errors, bad JSON
        log.warning("Known tickers: couldn't read the holdings: %s", e)
        return None
    pairs = (code_and_market(key) for key in held)
    return sorted(code for code, market in pairs if tickers.yahoo_symbol(code, market) == code)


def apply(saved: dict) -> None:
    """Make the saved lists the ones replies link."""
    codes = {c for name, _, _, _ in config.INDEX_LISTS for c in saved.get("indexes", {}).get(name, [])}
    tickers.set_known({c: c for c in codes | set(saved.get("held", []))})


def load(ctx: Ctx) -> dict:
    """The saved lists (empty on a first start), applied."""
    try:
        saved = json.loads(ctx.store.kv_get(KV_KEY) or "{}")
    except ValueError:
        saved = {}
    apply(saved)
    return saved


async def refresh(ctx: Ctx, saved: dict, client: httpx.AsyncClient) -> dict:
    """Fetch the indexes and the holdings; a part that fails keeps its last list."""
    indexes = dict(saved.get("indexes", {}))
    for name, url, low, high in config.INDEX_LISTS:
        found = await fetch_index(client, name, url, low, high)
        if found is not None:
            indexes[name] = found
    held = await us_holdings(ctx)
    new = {"indexes": indexes, "held": saved.get("held", []) if held is None else held, "ts": int(time.time())}
    await asyncio.to_thread(ctx.store.kv_set, KV_KEY, json.dumps(new))
    apply(new)
    log.info("Known tickers: %s, %d held, %d in all", ", ".join(f"{len(v)} {k}" for k, v in indexes.items()) or
             "no index lists", len(new["held"]), len(tickers.KNOWN))
    return new


async def loop(ctx: Ctx) -> None:
    saved = await asyncio.to_thread(load, ctx)
    log.info("Known tickers: %d loaded", len(tickers.KNOWN))
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            due = saved.get("ts", 0) + config.KNOWN_TICKERS_REFRESH - time.time()
            await asyncio.sleep(max(due, config.KNOWN_TICKERS_START_DELAY))  # overdue at startup: once the MCP is up
            try:
                saved = await refresh(ctx, saved, client)
            except Exception:
                log.exception("Known tickers: refresh failed")
                saved = {**saved, "ts": int(time.time())}  # try again tomorrow, not in a tight loop
