"""Link ticker symbols in a reply to Yahoo Finance (link only; the label hides the exchange suffix)."""

from __future__ import annotations

import re
from urllib.parse import quote

from .tickers_data import CRYPTO, EXCHANGE_SUFFIXES, NOT_TICKERS

TICKER_RE = re.compile(r"\b([A-Z0-9]{1,6}\.[A-Z]{1,2}|[A-Z][A-Z0-9]{1,5})\b")  # 9988.HK, SQX.AX, ACMR
# Single-letter tickers (U, F, X, T) only count next to a move or a price, so "vitamin C"
# and "A big move" are left alone.
ONE_LETTER_RE = re.compile(r"\b([A-Z])\b(?=\s*[-+—:]?\s*(?:[-+]?\d|\$))")
# Period and unit shorthand: FY26, Q3, H1, CY25, 2X, 10K, 200D
NOT_TICKER_RE = re.compile(r"^(?:FY|CY|HY|H|Q|FQ)\d+$|^\d|^[A-Z]\d+$")
SEGMENT_RE = re.compile(r"(<[^>]+>)")
# One pass for both shapes, so text the first pattern wrapped is never rescanned.
COMBINED_RE = re.compile(f"{TICKER_RE.pattern}|{ONE_LETTER_RE.pattern}")
PROTECTED_TAG_RE = re.compile(r"<(/?)(a|code|pre)\b", re.I)


def is_ticker(word: str) -> bool:
    base, _, suffix = word.partition(".")
    if suffix in EXCHANGE_SUFFIXES and base not in NOT_TICKERS:
        return True  # an exchange suffix settles it, even for numeric codes like 9988.HK
    return not (word in NOT_TICKERS or NOT_TICKER_RE.match(word))


def yahoo_url(symbol: str) -> str:
    sym = f"{symbol}-USD" if symbol in CRYPTO else symbol
    return "https://finance.yahoo.com/quote/" + quote(sym, safe="")


def ticker_label(symbol: str) -> str:
    """What the reader sees: SQX.AX becomes SQX, BRK.B stays BRK.B."""
    base, _, suffix = symbol.partition(".")
    return base if suffix in EXCHANGE_SUFFIXES else symbol


def link_tickers(text: str) -> str:
    """Wrap ticker-shaped words in Yahoo links, leaving tags and anything inside <a>, <code>
    or <pre> alone (so a link the model already wrote is never doubled)."""
    def wrap(m: re.Match) -> str:
        word = m.group(0)
        if len(word) > 1 and not is_ticker(word):
            return word
        return f'<a href="{yahoo_url(word)}">{ticker_label(word)}</a>'

    pieces, depth = [], 0
    for seg in SEGMENT_RE.split(text):
        if seg.startswith("<"):
            m = PROTECTED_TAG_RE.match(seg)
            if m:
                depth = max(0, depth - 1) if m.group(1) else depth + 1
            pieces.append(seg)
        else:
            pieces.append(seg if depth else COMBINED_RE.sub(wrap, seg))
    return "".join(pieces)
