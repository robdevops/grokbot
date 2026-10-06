"""Decide per request which tools (and which model) a message needs; most chat needs none."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

from ..config import Settings
from ..mcp.server import MCPServer, Registry
from ..tickers import COMBINED_RE, is_ticker

NEEDS_TOOLS = "NEEDS_TOOLS"

MARKET_WORDS = re.compile(
    r"\b(price|prices|stock|stocks|share|shares|quote|earnings|dividend|dividends|market|markets|"
    r"asx|nasdaq|nyse|dow|index|etf|crypto|bitcoin|ethereum|valuation|p/e|pe ratio|analyst|"
    r"target|yield|inflation|rba|fed|rate cut|ipo|buyback|guidance|revenue|profit|short|options|"
    r"chart|rally|crash|bull|bear|sector|gold|oil|trade|buy|sell|ticker|beta|eps)\b", re.I)
# A portfolio question: strong phrases always count; "my <money word>" phrases count; weak words
# count only when no ticker is named ("performance of NVDA" is a market question).
PORTFOLIO_STRONG = re.compile(
    r"\b(portfolios?|holdings?|sharesight|smsf|superannuation|super fund|net worth|"
    r"what do i (?:own|hold)|how am i doing|am i (?:up|down)|"
    r"what (?:did|have) i (?:make|made|lose|lost))\b", re.I)
PORTFOLIO_MY = re.compile(
    r"\b(?:my|our)\s+(?:\w+\s+){0,2}?(?:stocks?|shares|positions?|holdings?|portfolio|smsf|super|"
    r"account|cash|balance|returns?|gains?|performance|dividends?|winners?|losers?|performers?|"
    r"p&l|pnl|investments?|funds?|etfs?|trades?|watchlist)\b", re.I)
PORTFOLIO_WEAK = re.compile(r"\b(performance|gains?|positions?|winners?|losers?|p&l|pnl)\b", re.I)
LIVE_WORDS = re.compile(
    r"\b(news|today|latest|yesterday|tonight|this week|right now|breaking|announce\w*|who won|"
    r"score|weather|happening|headlines?|current\w*|updates?|recent\w*|just (now|in))\b", re.I)


@dataclass(frozen=True)
class Route:
    servers: list[MCPServer] = field(default_factory=list)  # MCP servers to offer
    search: bool = False
    simple: bool = False  # no tools at all: cheap model, and the model may ask for tools
    partial: bool = False  # fewer tools than are available: the model may ask for the rest


def _has_ticker(text: str) -> bool:
    return any(len(m.group(0)) == 1 or is_ticker(m.group(0)) for m in COMBINED_RE.finditer(text)) \
        or bool(re.search(r"\$[A-Za-z]{1,6}\b", text))


@lru_cache(maxsize=8)
def _names_re(names: frozenset[str]) -> re.Pattern | None:
    if not names:
        return None
    return re.compile(r"\b(?:" + "|".join(re.escape(n) for n in sorted(names)) + r")\b", re.I)


def is_portfolio_question(text: str, st: Settings) -> bool:
    if PORTFOLIO_STRONG.search(text) or PORTFOLIO_MY.search(text):
        return True
    names = _names_re(st.portfolio_names)
    if names and names.search(text):
        return True
    return bool(PORTFOLIO_WEAK.search(text)) and not _has_ticker(text)


def route(text: str, st: Settings, registry: Registry, *, force_full: bool = False) -> Route:
    """Tools for this message. With TOKEN_SAVER off, or `force_full` (a search/data request such
    as a movers list, a preset button or a retry after NEEDS_TOOLS), everything is offered."""
    up = registry.up()
    if force_full or not st.token_saver:
        return Route(up, st.search)
    portfolio = is_portfolio_question(text, st)
    market = portfolio or bool(MARKET_WORDS.search(text)) or _has_ticker(text)
    live = bool(LIVE_WORDS.search(text))
    servers = [s for s in up if (portfolio if s.cfg.get("gate") == "portfolio" else market)]
    search = st.search and (market or live)
    simple = not (servers or search)
    partial = not simple and (len(servers) < len(up) or search != st.search)
    return Route(servers, search, simple=simple, partial=partial)


def wants_tools(answer_text: str) -> bool:
    """True if a no-tools answer is the model asking to be re-run with tools."""
    return answer_text.strip().upper().startswith(NEEDS_TOOLS)
