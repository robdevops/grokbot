"""Tool-result slimming: Sharesight holdings flattening plus generic rounding/down-sampling."""

from __future__ import annotations

import json
import re

DATE_KEYS = ("date", "Date", "timestamp", "time", "datetime")
MAX_SERIES = 60  # points of a price/value series kept in a result
SIG_DIGITS = 6

def is_open(h) -> bool:
    """False for a Sharesight holding that's sold down to nothing or delisted."""
    if not isinstance(h, dict):
        return True
    if h.get("valid_position") is False:
        return False
    inst = h.get("instrument")
    if isinstance(inst, dict) and inst.get("expired"):
        return False
    q = h.get("quantity")
    return not (isinstance(q, (int, float)) and q == 0)


# Sharesight holdings are huge (each repeats the whole portfolio, currency
# objects, logos...). Keep only what's useful, flattened, so a big portfolio
# fits in one tool result.
HOLDING_KEEP = (
    "quantity", "value", "instrument_price", "average_purchase_price",
    "capital_gain", "capital_gain_percent", "payout_gain", "payout_gain_percent",
    "currency_gain", "total_gain", "total_gain_percent", "inception_date",
    "group_name", "cost_base", "values_over_time",
)


# Sharesight share-class tags: "Crowdstrike Holdings Inc - Ordinary Shares - Class A"
SHARE_CLASS = re.compile(
    r"(?:\s+-\s+(?:ordinary shares|class [a-z]|common stock|adr|ads|depositary receipts?))+\s*$",
    re.IGNORECASE,
)
# Trailing legal suffixes: "Arm Holdings plc." -> "Arm Holdings"
LEGAL_SUFFIX = re.compile(
    r"(?:[\s,.\-]+(?:limited|ltd|incorporated|inc|corporation|corp|co|plc|sponsored adr|adr|ads)\.?)+\s*$",
    re.IGNORECASE,
)


TRAILING_HOLDINGS = re.compile(r"\s+holdings?$", re.IGNORECASE)
TRAILING_TECH = re.compile(r"\s+technolog(?:y|ies)$", re.IGNORECASE)


def clean_name(name, code: str | None = None):
    if not isinstance(name, str):
        return name
    name = SHARE_CLASS.sub("", name).strip() or name
    name = LEGAL_SUFFIX.sub("", name).strip() or name
    name = TRAILING_HOLDINGS.sub("", name) or name
    short = TRAILING_TECH.sub("", name)
    if short != name:
        # "Micron Technology" -> "Micron", but "DUG Technology" -> "DUG Tech":
        # keep "Tech" when what's left is just the ticker or a tiny word.
        bare = not short or len(short) <= 3 or (code and short.lower() == code.lower())
        name = f"{short} Tech" if bare else short
    return name


TYPE_SHORT = {"Exchange Traded Fund": "ETF", "Depository Receipt": "ADR"}


def slim_holding(h):
    if not isinstance(h, dict):
        return h
    inst = h.get("instrument") if isinstance(h.get("instrument"), dict) else {}
    out = {
        "code": inst.get("code") or h.get("symbol"),
        "market": inst.get("market_code"),
        "name": clean_name(inst.get("name"), inst.get("code") or h.get("symbol")),
        "currency": inst.get("currency_code"),
        "type": inst.get("friendly_instrument_description"),
        "sector": inst.get("sector_classification_name"),
    }
    out.update({k: h.get(k) for k in HOLDING_KEEP})
    if out.get("type") == "Ordinary Shares":    # the default; only say when it's something else
        del out["type"]
    out["type"] = TYPE_SHORT.get(out.get("type"), out.get("type"))
    if out.get("group_name") in ("All Holdings", out.get("market")):  # ungrouped, or just the market again
        del out["group_name"]
    return {k: v for k, v in out.items() if v not in (None, [], {})}


def as_table(rows: list) -> dict | list:
    """A list of same-shaped records as {"columns": [...], "rows": [[...]]}, so
    each field name appears once instead of once per holding."""
    if not rows or not all(isinstance(r, dict) for r in rows):
        return rows
    columns = list(dict.fromkeys(k for r in rows for k in r))
    return {"columns": columns, "rows": [[r.get(c) for c in columns] for r in rows]}


REPORT_DROP = ("id", "portfolio_tz_name", "include_sales")


PORTFOLIO_KEEP = ("id", "name", "consolidated", "currency_code", "country_code",
                  "inception_date", "owner_name")


def slim_portfolio(p):
    return {k: p[k] for k in PORTFOLIO_KEEP if k in p} if isinstance(p, dict) else p


def tidy_sharesight(text: str, drop_closed: bool = False) -> str:
    """Compact a JSON tool result (pretty-printing wastes a lot of the size budget)
    and, for Sharesight, remove closed / zero-unit holdings."""
    try:
        data = json.loads(text)
    except ValueError:
        return text
    if isinstance(data, dict):
        # Holding lists are top-level, except in get_performance_report's {"report": {...}}.
        for parent in (data, data.get("report")):
            if not isinstance(parent, dict):
                continue
            for key in ("holdings", "combined_holdings"):
                items = parent.get(key)
                if isinstance(items, list):
                    parent[key] = as_table([slim_holding(h) for h in items if not drop_closed or is_open(h)])
        if isinstance(data.get("portfolios"), list):
            data["portfolios"] = [slim_portfolio(p) for p in data["portfolios"]]
        if isinstance(data.get("portfolio"), dict):
            data["portfolio"] = slim_portfolio(data["portfolio"])
        for key in ("api_transaction", "links"):    # API housekeeping
            data.pop(key, None)
        report = data.get("report")
        if isinstance(report, dict):
            if isinstance(report.get("currency"), dict):
                report["currency"] = report["currency"].get("code")
            for key in REPORT_DROP:
                report.pop(key, None)
            if report.get("grouping") == "ungrouped":
                report.pop("grouping")
                report.pop("sub_totals", None)  # one group, same as the report totals
            if isinstance(report.get("sub_totals"), list):
                report["sub_totals"] = [{k: v for k, v in s.items() if k != "group_id"}
                                        for s in report["sub_totals"] if isinstance(s, dict)]
            if isinstance(report.get("cash_accounts"), list):
                report["cash_accounts"] = [
                    {
                        "name": c.get("name"),
                        "value": c.get("value"),
                        "currency": (c.get("currency") or {}).get("code"),
                    }
                    for c in report["cash_accounts"] if isinstance(c, dict) and c.get("value")
                ]
        one = data.get("holding")
        if isinstance(one, dict):
            if drop_closed and not is_open(one):
                data = {"note": "This holding is closed (sold, or zero units). Treat it as not held."}
            else:
                data["holding"] = slim_holding(one)
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False)



def _round(x):
    return float(f"{x:.{SIG_DIGITS}g}") if isinstance(x, float) else x


def _is_series(items: list) -> bool:
    if len(items) <= MAX_SERIES:
        return False
    first = items[0]
    return isinstance(first, (int, float)) or (
        isinstance(first, dict) and any(k in first for k in DATE_KEYS))


def _downsample(items: list) -> list:
    """Evenly spaced points, always keeping the first and last."""
    step = len(items) / MAX_SERIES
    picked = [items[int(i * step)] for i in range(MAX_SERIES - 1)]
    return picked + [items[-1]]


def diet(node):
    """Round floats and thin out long time series, recursively."""
    if isinstance(node, float):
        return _round(node)
    if isinstance(node, list):
        if _is_series(node):
            node = _downsample(node)
        return [diet(x) for x in node]
    if isinstance(node, dict):
        out = {k: diet(v) for k, v in node.items()}
        rows = out.get("rows")  # a table from as_table(): thin it when it is a dated series
        cols = out.get("columns")
        if isinstance(rows, list) and isinstance(cols, list) and len(rows) > MAX_SERIES \
                and any(c in DATE_KEYS for c in cols):
            out["rows"] = _downsample(rows)
        return out
    return node


TABLE_NUMBER = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?|[+-]?(?:nan|inf)", re.IGNORECASE)
TABLE_RULE = re.compile(r":?-+:?")
UNITS = ((1e12, "T"), (1e9, "B"), (1e6, "M"))


def _cell(cell: str) -> str:
    """One markdown table cell: numbers to 6 significant digits (1.20067e+11 -> 120.067B), NaN to "-",
    and a midnight timestamp to its date."""
    cell = cell.strip()
    if cell.endswith(" 00:00:00"):
        return cell[:-9]
    if not TABLE_NUMBER.fullmatch(cell):
        return cell
    value = float(cell)
    if value != value:
        return "-"
    for limit, unit in UNITS:
        if abs(value) >= limit:
            return f"{value / limit:.{SIG_DIGITS}g}{unit}"
    return f"{value:.{SIG_DIGITS}g}"


def squeeze_tables(text: str) -> str:
    """Compact the padded markdown tables some servers return (Yahoo's statements are mostly spaces,
    separator dashes and 12-digit numbers): drop the padding and the separator row, shorten numbers."""
    lines = []
    for line in text.split("\n"):
        if not line.startswith("|"):
            lines.append(line)
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if all(TABLE_RULE.fullmatch(c) for c in cells if c) and any(cells):
            continue
        lines.append("|" + "|".join(_cell(c) for c in cells) + "|")
    return "\n".join(lines)


def slim_result(text: str, kind: str | None, drop_closed: bool = False) -> str:
    """Compact a tool result. JSON: server-specific flattening, then rounding/down-sampling. Markdown
    tables are squeezed; any other text is returned unchanged."""
    try:
        data = json.loads(text)
    except ValueError:
        return squeeze_tables(text) if "\n|" in text else text
    if kind == "sharesight":
        data = json.loads(tidy_sharesight(json.dumps(data), drop_closed))
    return json.dumps(diet(data), separators=(",", ":"), ensure_ascii=False)


def table_records(x) -> list[dict]:
    """Undo as_table() (also accepts a plain list of records)."""
    if isinstance(x, dict) and "columns" in x:
        return [dict(zip(x["columns"], row, strict=False)) for row in x.get("rows", [])]
    return x if isinstance(x, list) else []
