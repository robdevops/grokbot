"""Holding news text: the model's answer as bullets, the codes each names, repeats dropped, the search window."""

from __future__ import annotations

import html
import math
import re

from ... import config
from ...holdings import code_and_market
from ...quote_links import QUOTE_PREFIXES, quote_labels
from ...textfmt import ANY_TAG_RE
from ...tickers import yahoo_symbol

WINDOW_DEFAULT = 24  # hours a check covers: one day, or since the last run when that was longer ago
HREF_RE = re.compile(r'<a href="([^"]+)"')


def is_nothing(raw: str) -> bool:
    """True for the model's "nothing to report" answer, however it's dressed up ("NOTHING",
    "• NOTHING", "**Nothing.**", "None") or an empty reply."""
    plain = html.unescape(ANY_TAG_RE.sub("", raw or ""))
    words = re.sub(r"[^A-Za-z ]", " ", plain).split()
    # A real item is longer, even one about a company called "Nothing ...".
    return not words or (words[0].upper() in ("NOTHING", "NONE") and len(words) <= 5)


def as_bullets(text: str) -> str:
    """One "• " bullet per non-empty line, whatever bullet style (if any) the model used."""
    lines = (re.sub(r"^\s*(?:[•\-*–·]|\d+[.)])\s+", "", ln).strip() for ln in text.splitlines())
    return "\n".join(f"• {ln}" for ln in lines if ln)


def known_symbols(holdings: dict[str, str]) -> dict[str, str]:
    """The holdings' codes as Yahoo symbols ({"NXT": "NXT.AX"}), so the digest links them however the model writes them."""
    pairs = (code_and_market(key) for key in holdings)
    return {code: yahoo_symbol(code, market) or code for code, market in pairs}


def news_codes(news: str, holdings: dict[str, str]) -> list[str]:
    """Holding codes that appear as bold tickers or quote links in a news message (as the holdings write them: an HK
    holding "700" is linked as 0700.HK but labelled 700)."""
    held = {_plain_code(k.split(" ")[0]): k.split(" ")[0].upper() for k in holdings}
    found = {m.upper() for m in re.findall(r"<b>([^<]{1,12})</b>", news)}
    found |= {label.upper() for label in quote_labels(news)}
    return sorted({held[c] for c in map(_plain_code, found) if c in held})


def _plain_code(code: str) -> str:
    code = code.upper()
    return code.lstrip("0") or "0" if code.isdigit() else code


def window_hours(now: float, last_run: float | None) -> int:
    """Hours the daily check covers: since the last one (after skipped days, an outage or a restart), whole hours,
    at least a day and at most HOLDING_NEWS_MAX_WINDOW; a day when there was no last run."""
    if not last_run:
        return WINDOW_DEFAULT
    hours = math.ceil((now - last_run) / 3600)
    return max(WINDOW_DEFAULT, min(hours, config.HOLDING_NEWS_MAX_WINDOW))


def source_links(news: str) -> set[str]:
    """The source links of a digest (not the quote links on tickers)."""
    return {html.unescape(u) for u in HREF_RE.findall(news) if not u.startswith(QUOTE_PREFIXES)}


def drop_repeats(news: str, recent: list[str]) -> tuple[str, list[str]]:
    """The digest without the items whose sources were all in a recent digest (the same articles again), and the
    items dropped. An item with no source link, or a new one, stays."""
    seen = set().union(*(source_links(r) for r in recent)) if recent else set()
    kept, dropped = [], []
    for line in news.splitlines():
        links = source_links(line)
        (dropped if links and links <= seen else kept).append(line)
    return "\n".join(kept), dropped
