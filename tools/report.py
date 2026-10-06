"""`make prompt-report`: where a request's input tokens go (estimated at 4 characters per token)."""

from __future__ import annotations

import json
from zoneinfo import ZoneInfo

from lib.history import format_rows
from lib.llm.gate import Route
from lib.mcp.schema import ToolDef, compact_description, compact_schema
from lib.prompts import chat_prompt, system_prompt
from lib.store import HistoryRow

LINK = '<a href="https://finance.yahoo.com/quote/{t}"><b>{t}</b></a>'
SAMPLE_TOOL = {
    "name": "get_historical_prices",
    "description": ("Get historical OHLCV prices for a stock.\n\nReturns: a JSON list of daily bars with "
                    "date, open, high, low, close and volume.\n\nExample: get_historical_prices('AAPL', "
                    "period='1mo', interval='1d')"),
    "schema": {"title": "Args", "type": "object", "properties": {
        "symbol": {"title": "Symbol", "type": "string", "description": "Ticker symbol. Use the Yahoo suffix for non-US stocks."},
        "period": {"title": "Period", "type": "string", "default": "1mo",
                   "description": "Look-back window. One of 1d, 5d, 1mo, 3mo, 6mo, 1y, 5y, max."},
        "interval": {"title": "Interval", "type": "string", "default": "1d", "description": "Bar size."}},
        "required": ["symbol"]},
}


def tokens(text: str) -> int:
    return len(text) // 4


def sample_rows() -> list[HistoryRow]:
    """A 25-message group chat of the kind the bot sees, with the bot's answers carrying links."""
    rows = []
    for i in range(1, 26):
        if i % 5 == 0:
            body = ("<b>Markets</b>\n" + "\n".join(
                f"{n} ({LINK.format(t=t)}) up {i}.{k}% on results, guidance raised and a buyback "
                f"announced, with analysts lifting targets" for k, (n, t) in
                enumerate([("Nvidia", "NVDA"), ("Pro Medicus", "PME.AX"), ("Arm", "ARM")])))
            rows.append(HistoryRow(i, "Stock (@stockbot)", body, i * 60, i - 1))
        else:
            rows.append(HistoryRow(i, "Rob (@rob)", f"what do you reckon about the market today, message {i}?", i * 60, None))
    return rows


def report() -> str:
    tz, lines = ZoneInfo("UTC"), []
    for saver in (False, True):
        rows = sample_rows()
        history = format_rows(rows, "Stock (@stockbot)", tz, line_max=240 if saver else 400, compact_text=saver,
                              own_line_max=160 if saver else None)
        sys_full = system_prompt("Stock", Route([], True), "the web", saver=saver)
        tool = ToolDef("yahoo__get_historical_prices",
                       compact_description(SAMPLE_TOOL["description"], 220 if saver else 600),
                       compact_schema(SAMPLE_TOOL["schema"]))
        tail = chat_prompt(transcript="", sender="Rob", private=False, reply_quote=None, msg_id=26,
                           now="Tuesday 06 October 2026, 10:00 UTC", saver=saver)
        lines.append(f"TOKEN_SAVER={'on' if saver else 'off'}: system ~{tokens(sys_full)}, "
                     f"history (25 msgs) ~{tokens(history)}, tail ~{tokens(tail)}, "
                     f"one tool definition ~{tokens(json.dumps(vars(tool)))}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(report())
