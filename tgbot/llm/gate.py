"""Decide per request which tools (and which model) a message needs; most chat needs none."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..config import Settings
from ..mcp.server import MCPServer, Registry
from ..tickers import COMBINED_RE, is_ticker

NEEDS_TOOLS = "NEEDS_TOOLS"

MARKET_WORDS = re.compile(
    r"\b(price|prices|stock|stocks|share|shares|quote|earnings|dividend|dividends|market|markets|"
    r"asx|nasdaq|nyse|dow|index|etf|crypto|bitcoin|ethereum|valuation|p/e|pe ratio|analyst|"
    r"target|yield|inflation|rba|fed|rate cut|ipo|buyback|guidance|revenue|profit|short|options|"
    r"chart|rally|crash|bull|bear|sector|gold|oil|trade|buy|sell|ticker|beta|eps)\b", re.I)
PORTFOLIO_WORDS = re.compile(
    r"\b(portfolio|portfolios|holdings?|positions?|performance|gains?|sharesight|"
    r"what do i (own|hold)|my (stocks|shares|investments))\b", re.I)
LIVE_WORDS = re.compile(
    r"\b(news|today|latest|yesterday|tonight|this week|right now|breaking|announce\w*|who won|"
    r"score|weather|happening|headlines?|current\w*|updates?|recent\w*|just (now|in))\b", re.I)


@dataclass(frozen=True)
class Route:
    servers: list[MCPServer] = field(default_factory=list)  # MCP servers to offer
    search: bool = False
    simple: bool = False  # no tools at all: cheap model, and the model may ask for tools


def _has_ticker(text: str) -> bool:
    return any(len(m.group(0)) == 1 or is_ticker(m.group(0)) for m in COMBINED_RE.finditer(text)) \
        or bool(re.search(r"\$[A-Za-z]{1,6}\b", text))


def route(text: str, st: Settings, registry: Registry, *, force_full: bool = False) -> Route:
    """Tools for this message. With TOKEN_SAVER off, or `force_full` (a search/data request such
    as a movers list, a preset button or a retry after NEEDS_TOOLS), everything is offered."""
    up = registry.up()
    if force_full or not st.token_saver:
        return Route(up, st.search)
    portfolio = bool(PORTFOLIO_WORDS.search(text))
    market = portfolio or bool(MARKET_WORDS.search(text)) or _has_ticker(text)
    live = bool(LIVE_WORDS.search(text))
    servers = [s for s in up if (portfolio if s.cfg.get("gate") == "portfolio" else market)]
    search = st.search and (market or live)
    return Route(servers, search, simple=not (servers or search))


def wants_tools(answer_text: str) -> bool:
    """True if a no-tools answer is the model asking to be re-run with tools."""
    return answer_text.strip().upper().startswith(NEEDS_TOOLS)
