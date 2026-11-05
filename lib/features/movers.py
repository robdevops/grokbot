"""MOVERS_BOTS: explain another bot's end-of-day "big movers" list, once, without tagging it."""

from __future__ import annotations

import re

from telegram import Message, MessageEntity

from ..tickers import yahoo_symbol

# The bold header, e.g. "≥ 5.0% at close (ASX):" or "≤ -5% at close (NASDAQ, NYSE):"
HEADER = re.compile(r"(?:≥|≤|>=?|<=?)\s*[-−]?\s*\d+(?:\.\d+)?\s*%.*\bclose\b", re.IGNORECASE)
PERCENT = re.compile(r"\d+(?:\.\d+)?\s*%")
EXCHANGES = re.compile(r"\(([A-Z][A-Z ,/]*)\)")  # "(ASX)", "(NASDAQ, NYSE)" in the header
STOCK_LINE = re.compile(r"^\W*([A-Z][A-Z0-9]{0,5})\b.*\d+(?:\.\d+)?\s*%")


def is_movers_list(msg: Message, bots: frozenset[str]) -> bool:
    """A big-movers-at-close list from one of `bots`: a bold header like "≥ 5.0% at close
    (ASX):" followed by stocks with % changes."""
    u = msg.from_user
    if not (bots and u and u.is_bot and (u.username or "").lower() in bots):
        return False
    if msg.text:
        bold = msg.parse_entities([MessageEntity.BOLD])
    else:
        bold = msg.parse_caption_entities([MessageEntity.BOLD])
    if not any(HEADER.search(t) for t in bold.values()):
        return False
    # The header has one percentage; a list needs at least one more.
    return len(PERCENT.findall(msg.text or msg.caption or "")) >= 2


def untag(text: str, bots: frozenset[str]) -> str:
    """Turn @mentions of the movers bots into plain names, so replies never tag them."""
    for name in bots:
        text = re.sub(rf"@({re.escape(name)})\b", r"\1", text, flags=re.IGNORECASE)
    return text


def strip_summary(text: str) -> str:
    """Drop a closing wrap-up paragraph ("Typical small-cap swings on thin catalysts.") from a
    movers explanation: the last paragraph, if it names no ticker while earlier ones do. Stock
    lines always carry a linked ticker (<a ...>) once tickers are linked."""
    paras = re.split(r"\n\s*\n", text.strip())
    if len(paras) > 1 and "<a " not in paras[-1] and any("<a " in p for p in paras[:-1]):
        return "\n\n".join(paras[:-1])
    return text


def listed_codes(listing_html: str) -> dict[str, str]:
    """The listed codes as Yahoo symbols ({"DRO": "DRO.AX"} under an "(ASX)" header), so the explanation links them
    bare. A header naming exchanges with different suffixes, or none we know, gives nothing: a guess could be wrong."""
    lines = re.sub(r"<[^>]+>", "", listing_html).splitlines()
    head = next((ln for ln in lines if HEADER.search(ln)), "")
    found = EXCHANGES.search(head)
    names = [n.strip() for n in re.split(r"[,/]", found.group(1))] if found else []
    symbols = {yahoo_symbol("X", n) for n in names}
    if len(symbols) != 1 or None in symbols:
        return {}
    codes = (m.group(1) for ln in lines if ln is not head and (m := STOCK_LINE.match(ln)))
    return {code: yahoo_symbol(code, names[0]) or code for code in codes}
