"""Chat history rows -> the transcript lines shown to the model."""

from __future__ import annotations

import html
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from .store import HistoryRow
from .textfmt import ANY_TAG_RE

_LINK = re.compile(r'<a href="([^"]*)">(.*?)</a>', re.DOTALL)
_YAHOO = "https://finance.yahoo.com/quote/"


def compact(text: str) -> str:
    """Stored Telegram HTML as cheap plain text: ticker links become the symbol, other links
    "label (url)", tags are dropped and whitespace is collapsed."""
    def link(m: re.Match) -> str:
        url, label = m.group(1), m.group(2)
        return label if url.startswith(_YAHOO) else f"{label} ({url})"

    plain = html.unescape(ANY_TAG_RE.sub("", _LINK.sub(link, text)))
    return " ".join(plain.split())


def format_rows(rows: list[HistoryRow], own_name: str, tz: ZoneInfo, *, line_max: int,
                compact_text: bool, own_line_max: int | None = None) -> str:
    """One line per message, oldest first: `[#id] Wed 14:05 Name (replying to #x): text`.
    The bot's own earlier replies appear as "You". Long lines are cut (the bot's own, usually the
    longest, at `own_line_max` when given)."""
    lines = []
    for r in rows:
        when = datetime.fromtimestamp(r.ts, tz).strftime("%a %H:%M")
        reply = f" (replying to #{r.reply_to})" if r.reply_to else ""
        who = "You" if r.sender == own_name else r.sender
        text = compact(r.text) if compact_text else r.text
        cap = own_line_max if (own_line_max and who == "You") else line_max
        if len(text) > cap:
            text = text[:cap].rstrip() + " …[cut]"
        lines.append(f"[#{r.message_id}] {when} {who}{reply}: {text}")
    return "\n".join(lines)
