"""Sharesight portfolios and their current holdings, shared by holding news, the known tickers and the tool gate."""

from __future__ import annotations

import json
import logging
import re

from .mcp.results import table_records

log = logging.getLogger("bot")
KEY_RE = re.compile(r"^(\S+) \((.*)\)$")
# Every portfolio's name, lowercased, as Sharesight lists them: a message naming one is a portfolio question
# (llm/gate.py). Set from the kv cache at startup and by the daily refresh (features/known_tickers.py).
PORTFOLIO_NAMES: set[str] = set()


def set_portfolio_names(names) -> None:
    PORTFOLIO_NAMES.clear()
    PORTFOLIO_NAMES.update(n.lower() for n in names)


async def portfolios(server) -> list[dict]:
    """Sharesight's list_portfolios: [{"id", "name", "consolidated", ...}]."""
    return json.loads(await server.call("list_portfolios", {})).get("portfolios", [])


async def current_holdings(server, portfolio_names: list[str] | None) -> dict[str, str]:
    """Current holdings across the named portfolios (None: every one but the consolidated views, which repeat the
    others), as {"CODE (MARKET)": name}."""
    listed = await portfolios(server)
    portfolios_by_name = {p["name"].lower(): p for p in listed}
    if portfolio_names is None:
        portfolio_names = [p["name"].lower() for p in listed if not p.get("consolidated")]
    holdings: dict[str, str] = {}
    for name in portfolio_names:
        p = portfolios_by_name.get(name)
        if not p:
            log.warning("No Sharesight portfolio called %r (have: %s)", name, ", ".join(sorted(portfolios_by_name)))
            continue
        report = json.loads(await server.call(
            "get_performance_report", {"portfolio_id": p["id"], "include_sales": False})).get("report") or {}
        for h in table_records(report.get("holdings")):
            if h.get("code"):
                holdings[f"{h['code']} ({h.get('market', '?')})"] = h.get("name") or h["code"]
    return holdings


def code_and_market(key: str) -> tuple[str, str]:
    """"NXT (ASX)" -> ("NXT", "ASX")."""
    m = KEY_RE.match(key)
    return (m.group(1), m.group(2)) if m else (key, "")
