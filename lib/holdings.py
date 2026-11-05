"""Current Sharesight holdings across named portfolios, shared by holding news and the known-ticker list."""

from __future__ import annotations

import json
import logging
import re

from .mcp.results import table_records

log = logging.getLogger("bot")
KEY_RE = re.compile(r"^(\S+) \((.*)\)$")


async def current_holdings(server, portfolio_names: list[str]) -> dict[str, str]:
    """Current holdings across the named portfolios, as {"CODE (MARKET)": name}."""
    data = json.loads(await server.call("list_portfolios", {}))
    portfolios = {p["name"].lower(): p for p in data.get("portfolios", [])}
    holdings: dict[str, str] = {}
    for name in portfolio_names:
        p = portfolios.get(name)
        if not p:
            log.warning("No Sharesight portfolio called %r (have: %s)", name, ", ".join(sorted(portfolios)))
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
