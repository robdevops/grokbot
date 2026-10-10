"""Capture real MCP tool output for test fixtures: `python -m tools.capture yahoo|sharesight [--days N]`.

Runs where the MCP servers work, with the service's environment. Prints each read-only tool the bot
offers (its schema, then a real call, raw and as the bot slims it) to stdout. The output holds real
holdings and IDs: redact it before sharing or committing it."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import date, timedelta

from lib import config
from lib.mcp.results import slim_result
from lib.mcp.server import MCPServer, Registry

TICKER_NAMES, TICKER_LISTS = {"ticker", "symbol"}, {"tickers", "symbols"}
# Extra Yahoo cases: a non-US listing, and a symbol that doesn't exist (to capture an error).
YAHOO_EXTRA = [("get_stock_quote", {"ticker": "CBA.AX", "symbol": "CBA.AX"}),
               ("get_stock_quote", {"ticker": "ZZZZNOTREAL", "symbol": "ZZZZNOTREAL"})]


def _kind(prop: dict) -> str:
    kind = prop.get("type")
    if kind is None:  # anyOf: [{"type": "string"}, {"type": "null"}]
        kind = next((o.get("type") for o in prop.get("anyOf", []) if o.get("type") != "null"), "")
    return kind


def fill_args(schema: dict, today: date, days: int = 30, known: dict | None = None) -> dict | None:
    """Arguments for a tool call built from its JSON schema (required parameters only), or None if a
    required parameter can't be guessed. `known` overrides by parameter name."""
    known, args = known or {}, {}
    for name in schema.get("required", []):
        prop = schema.get("properties", {}).get(name, {})
        low, kind = name.lower(), _kind(prop)
        if name in known:
            args[name] = known[name]
        elif "default" in prop:
            args[name] = prop["default"]
        elif prop.get("enum"):
            args[name] = prop["enum"][0]
        elif kind == "string" and low in TICKER_NAMES:
            args[name] = "NVDA"
        elif kind == "array" and low in TICKER_LISTS:
            args[name] = ["NVDA", "MSFT"]
        elif kind == "string" and "start" in low:
            args[name] = (today - timedelta(days=days)).isoformat()
        elif kind == "string" and ("end" in low or "date" in low):
            args[name] = today.isoformat()
        elif kind == "integer" and low in ("count", "limit", "n"):
            args[name] = 5
        elif kind == "boolean":
            args[name] = False
        else:
            return None
    return args


def result_text(result) -> str:
    parts = [c.text if c.type == "text" else f"[{c.type} content omitted]" for c in result.content]
    text = "\n".join(parts) or json.dumps(getattr(result, "structuredContent", None))
    return ("Tool error: " + text) if result.isError else text


async def call(server: MCPServer, tool: str, args: dict) -> str:
    print(f"\n=== {tool} {json.dumps(args)} ===")
    try:
        raw = result_text(await asyncio.wait_for(server.session.call_tool(tool, args), config.MCP_TIMEOUT))
    except Exception as e:
        print(f"CALL FAILED: {type(e).__name__}: {e}")
        return ""
    slim = slim_result(raw, server.cfg.get("slim", server.label), bool(server.cfg.get("current_holdings_only")))
    print(f"--- raw ({len(raw)} chars) ---\n{raw}\n--- slimmed ({len(slim)} chars) ---\n{slim}")
    return raw


async def capture(server: MCPServer, days: int) -> None:
    listed = {t.name: t for t in (await server.session.list_tools()).tools}
    names = sorted(set(server.fn_names.values()))
    print("=== TOOLS (the bot offers these) ===")
    print(json.dumps([{"name": n, "description": listed[n].description, "inputSchema": listed[n].inputSchema,
                       "annotations": listed[n].annotations.model_dump() if listed[n].annotations else None}
                      for n in names], indent=1))
    today = date.today()
    ids: list = []
    if "list_portfolios" in names:  # Sharesight: portfolio IDs come from list_portfolios
        text = await call(server, "list_portfolios", {})
        ids = [p.get("id") for p in json.loads(text).get("portfolios", [])] if text.startswith("{") else []
    plan = [(n, None) for n in names] + [(n, k) for n, k in YAHOO_EXTRA if n in names]
    for name, known in plan:
        if name == "list_portfolios":
            continue
        for pid in (ids if "portfolio_id" in listed[name].inputSchema.get("properties", {}) else [None]):
            extra = dict(known or {}, **({"portfolio_id": pid} if pid is not None else {}))
            args = fill_args(listed[name].inputSchema, today, days, extra)
            if args is None:
                print(f"\n=== {name}: SKIPPED (couldn't guess its required arguments; see the schema above) ===")
                continue
            if server.cfg.get("current_holdings_only") and name == "get_performance_report":
                args["include_sales"] = False  # as the bot sends it
            await call(server, name, args)


async def run(label: str, days: int) -> int:
    registry = Registry.load(config.data_path(os.environ, "MCP_CONFIG", "mcp_servers.json"), timeout=config.MCP_TIMEOUT,
                             max_output=10**7)
    server = registry.servers.get(label)
    if server is None:
        print(f"No MCP server '{label}' in the config (have: {', '.join(registry.servers) or 'none'})", file=sys.stderr)
        return 2
    server.start()
    await server.wait_ready()
    if not server.session:
        print(f"{label} didn't start: {server.error}", file=sys.stderr)
        return 1
    try:
        await capture(server, days)
    finally:
        await server.stop()
    return 0


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Capture real MCP output for test fixtures.")
    parser.add_argument("server", help="server label in mcp_servers.json, e.g. yahoo or sharesight")
    parser.add_argument("--days", type=int, default=30, help="date range for tools that take one")
    args = parser.parse_args(argv)
    sys.exit(asyncio.run(run(args.server, args.days)))


if __name__ == "__main__":
    main()
