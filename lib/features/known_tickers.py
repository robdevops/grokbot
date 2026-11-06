"""Tickers a reply may name bare and still get a link: the S&P 500, the NASDAQ-100, the ASX 200 and the holdings.

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

from .. import config, holdings, quote_links, tickers
from ..context import Ctx
from ..holdings import code_and_market, current_holdings, portfolios

log = logging.getLogger("bot")
KV_KEY = "known_tickers"
US_SYMBOL_RE = re.compile(r"^[A-Z]{1,5}(?:-[A-Z])?$")
ASX_SYMBOL_RE = re.compile(r"^[A-Z0-9]{3,5}$")  # BHP, A2M, AX1
# Wikimedia refuses programs whose User-Agent names no contact; its action API is the way in for programs.
HEADERS = {"User-Agent": "grokbot/1.0 (+https://github.com/robdevops/grokbot; daily index list)"}
API = "https://en.wikipedia.org/w/api.php"
HEADER_NAMES = ("symbol", "ticker", "ticker symbol", "code", "asx code")


# Where a US list's ticker links point -> the exchange as Google Finance names it (the S&P 500 list links each
# ticker to its exchange's own quote page).
EXCHANGE_SITES = {"nyse.com": "NYSE", "nasdaq.com": "NASDAQ", "cboe.com": "BATS"}


class _Tables(HTMLParser):
    """Every <table> on a page as {"id": ..., "rows": [[{"text": [...], "href": first link}, ...], ...]}, nested
    ones included."""

    def __init__(self) -> None:
        super().__init__()
        self.tables: list[dict] = []
        self._open: list[dict] = []
        self._cell: dict | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            table = {"id": dict(attrs).get("id") or "", "rows": []}
            self.tables.append(table)
            self._open.append(table)
        elif tag == "tr" and self._open:
            self._open[-1]["rows"].append([])
        elif tag in ("td", "th") and self._open and self._open[-1]["rows"]:
            self._cell = {"text": [], "href": ""}
            self._open[-1]["rows"][-1].append(self._cell)
        elif tag == "a" and self._cell is not None and not self._cell["href"]:
            self._cell["href"] = dict(attrs).get("href") or ""

    def handle_endtag(self, tag):
        if tag == "table" and self._open:
            self._open.pop()
        elif tag in ("td", "th"):
            self._cell = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell["text"].append(data)


def symbols_from_html(page: str, suffix: str = "") -> list[str]:
    """The ticker column of a Wikipedia index page: the table with id "constituents", else the first table with a
    "Symbol", "Ticker" or "Code" header. US share classes as Yahoo writes them (BRK.B -> BRK-B); ASX codes as listed."""
    return list(listings(page, suffix))


def listings(page: str, suffix: str = "") -> dict[str, str]:
    """symbols_from_html's codes, each with the exchange its link points to ("NYSE", "NASDAQ"; "" if none)."""
    parser = _Tables()
    parser.feed(page)
    tables = sorted(parser.tables, key=lambda t: t["id"] != "constituents")
    for table in tables:
        rows = table["rows"]
        if not rows:
            continue
        header = [re.sub(r"\[.*?\]", "", "".join(h["text"])).strip().lower() for h in rows[0]]  # "Ticker[12]"
        col = next((i for i, h in enumerate(header) if h in HEADER_NAMES), None)
        if col is None:
            continue
        found = {}
        for row in rows[1:]:
            if len(row) <= col:
                continue
            code = "".join(row[col]["text"]).strip()
            code = code if suffix else code.replace(".", "-")
            if (ASX_SYMBOL_RE if suffix else US_SYMBOL_RE).match(code):
                href = row[col]["href"]
                found[code] = next((x for site, x in EXCHANGE_SITES.items() if site in href), "")
        return found
    return {}


async def fetch_index(client: httpx.AsyncClient, name: str, title: str, suffix: str, low: int,
                      high: int) -> dict[str, str] | None:
    """An index's codes, each with its exchange where the page says ("" if not), or None if the page couldn't be read
    or doesn't look right."""
    params = {"action": "parse", "page": title, "prop": "text", "format": "json", "formatversion": "2",
              "redirects": "1"}
    try:
        r = await client.get(API, params=params, headers=HEADERS, follow_redirects=True)
        if r.status_code != 200:
            log.warning("Known tickers: couldn't fetch the %s list: HTTP %d: %s", name, r.status_code,
                        " ".join(r.text.split())[:200])
            return None
        page = r.json()["parse"]["text"]
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
        log.warning("Known tickers: couldn't fetch the %s list: %s", name, e)
        return None
    found = listings(page, suffix)
    if not low <= len(found) <= high:
        log.warning("Known tickers: the %s page gave %d tickers (expected %d-%d); keeping the last list",
                    name, len(found), low, high)
        return None
    if name == "NASDAQ-100":
        found = dict.fromkeys(found, "NASDAQ")  # its members are NASDAQ listings by definition
    return found


async def held_symbols(ctx: Ctx) -> tuple[dict[str, str], dict[str, str], list[str]] | None:
    """Every Sharesight portfolio's holdings on exchanges we know: code -> Yahoo symbol ({"NXT": "NXT.AX", "GM": "GM"}),
    for US ones code -> exchange ({"GM": "NYSE"}), and the portfolios' names; None if Sharesight isn't up."""
    server = next((s for s in ctx.registry.up() if s.cfg.get("gate") == "portfolio"), None)
    if server is None:
        return None
    try:
        names = [p["name"] for p in await portfolios(server) if p.get("name")]
        held = await current_holdings(server, None)
    except Exception as e:  # the MCP server's own errors, bad JSON
        log.warning("Known tickers: couldn't read the holdings: %s", e)
        return None
    symbols, exchanges = {}, {}
    for code, market in (code_and_market(key) for key in held):
        symbol = tickers.yahoo_symbol(code, market)
        if symbol == code:  # a US listing: Yahoo writes share classes BRK-B
            code = symbol = code.replace(".", "-")
            exchanges[code] = market.upper()
        if symbol:
            symbols[code] = symbol
    return symbols, exchanges, names


def known(saved: dict) -> tuple[dict[str, str], set[str]]:
    """Code -> Yahoo symbol from the saved lists, and the codes left out because they name a company on two
    exchanges (in a US index and in the ASX 200) unless a holding settles which. US share classes are known both
    ways (BRK-B and BRK.B, as the model may write it)."""
    by_code: dict[str, set[str]] = {}
    for name, _, suffix, _, _ in config.INDEX_LISTS:
        for code in saved.get("indexes", {}).get(name, []):
            by_code.setdefault(code, set()).add(f"{code}.{suffix}" if suffix else code)
    ambiguous = {code for code, symbols in by_code.items() if len(symbols) > 1}
    codes = {code: next(iter(symbols)) for code, symbols in by_code.items() if code not in ambiguous}
    held = saved.get("held", {})
    if isinstance(held, list):  # saved by an earlier version: US codes only
        held = {code.replace(".", "-"): code.replace(".", "-") for code in held}
    codes.update(held)
    ambiguous -= set(held)
    codes.update({c.replace("-", "."): s for c, s in list(codes.items()) if "-" in c and s == c})
    return codes, ambiguous


def apply(saved: dict) -> set[str]:
    """Make the saved lists the ones replies link; returns the ambiguous codes left out."""
    codes, ambiguous = known(saved)
    tickers.set_known(codes)
    holdings.set_portfolio_names(saved.get("portfolios", []))
    exchanges = {c: x for c, x in saved.get("exchanges", {}).items() if x}
    quote_links.set_us_exchanges({**exchanges, **{c.replace("-", "."): x for c, x in exchanges.items() if "-" in c}})
    return ambiguous


def load(ctx: Ctx) -> dict:
    """The saved lists (empty on a first start), applied."""
    try:
        saved = json.loads(ctx.store.kv_get(KV_KEY) or "{}")
    except ValueError:
        saved = {}
    apply(saved)
    return saved


async def refresh(ctx: Ctx, saved: dict, client: httpx.AsyncClient) -> dict:
    """Fetch the indexes and the holdings; a part that fails keeps its last list and is tried again sooner
    (KNOWN_TICKERS_RETRY) than the daily refresh."""
    indexes, failed = dict(saved.get("indexes", {})), False
    exchanges = dict(saved.get("exchanges", {}))
    for name, title, suffix, low, high in config.INDEX_LISTS:
        found = await fetch_index(client, name, title, suffix, low, high)
        if found is None:
            failed = True
        else:
            indexes[name] = list(found)
            exchanges.update({c: x for c, x in found.items() if x and not suffix})
    got = await held_symbols(ctx)
    held = None if got is None else got[0]
    if got is not None:
        exchanges.update(got[1])
    failed |= held is None and any(s.cfg.get("gate") == "portfolio" for s in ctx.registry.servers.values())
    ts = int(time.time()) - (config.KNOWN_TICKERS_REFRESH - config.KNOWN_TICKERS_RETRY if failed else 0)
    new = {"indexes": indexes, "held": saved.get("held", {}) if held is None else held, "exchanges": exchanges,
           "portfolios": saved.get("portfolios", []) if got is None else got[2],
           "ts": ts}
    await asyncio.to_thread(ctx.store.kv_set, KV_KEY, json.dumps(new))
    ambiguous = apply(new)
    log.info("Known tickers: %s, %d held in %d portfolios, %d in all (%d on two exchanges left out), %d US with "
             "their exchange", ", ".join(f"{len(v)} {k}" for k, v in indexes.items()) or "no index lists",
             len(new["held"]), len(new["portfolios"]),
             len(tickers.KNOWN), len(ambiguous), len(exchanges))
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
                saved = {**saved, "ts": int(time.time()) - config.KNOWN_TICKERS_REFRESH + config.KNOWN_TICKERS_RETRY}
