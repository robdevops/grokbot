"""Reply filter: Yahoo links on tickers (the label hides the exchange suffix), bold "Name (TICKER)", raw links as [n]."""

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
BOLD_TAG_RE = re.compile(r"<(/?)(b|strong)\b", re.I)
URL_RE = re.compile(r'https?://[^\s<>"]+')
_CITE = r'<a href="[^"]*">\[\d+\]</a>'
CITE_ONLY_RUN_RE = re.compile(rf"\s*\n\s*({_CITE}(?:\s+{_CITE})*)[ \t]*(?=\n|$)")  # lines holding only citations
RAW_ANCHOR_RE =re.compile(r'<a href="(https?://[^"]+)">\s*https?://[^<]*</a>')
# "Micron (MU)": up to four capitalised words (or a number) right before a parenthesised ticker.
_WORD = r"[A-Z0-9][\w&'’-]*(?:\.[\w&'’-]+)*"
NAME_TICKER_RE = re.compile(
    rf"(?<![\w&'’.-])((?:{_WORD}[ ]){{0,3}}{_WORD})[ ]\(({TICKER_RE.pattern[2:-2]})\)")
# A capitalised word that opens a sentence is not part of the company name ("Today Micron (MU)").
SENTENCE_OPENERS = {"Today", "Also", "Meanwhile", "And", "But", "So", "Then", "Yesterday", "Tonight",
                    "Still", "Plus", "Even", "Now", "Overall", "However", "Elsewhere"}


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


def bold_names(text: str) -> str:
    """Bold "Name (TICKER)" (name and parenthesised ticker, one <b>), e.g. "Micron (MU)"."""
    def wrap(m: re.Match) -> str:
        name, symbol = m.group(1), m.group(2)
        if len(symbol) > 1 and not is_ticker(symbol):
            return m.group(0)
        words = name.split(" ")
        skip = 0
        while skip < len(words) - 1 and words[skip] in SENTENCE_OPENERS:
            skip += 1
        lead = " ".join(words[:skip]) + " " if skip else ""
        return f"{lead}<b>{' '.join(words[skip:])} ({symbol})</b>"

    return NAME_TICKER_RE.sub(wrap, text)


def _rewrite(text: str, fn) -> str:
    """Apply fn(plain_text, inside_bold) to the text between tags, leaving tags and everything
    inside <a>, <code> or <pre> alone."""
    pieces, depth, bold = [], 0, 0
    for seg in SEGMENT_RE.split(text):
        if seg.startswith("<"):
            m = PROTECTED_TAG_RE.match(seg)
            if m:
                depth = max(0, depth - 1) if m.group(1) else depth + 1
            b = BOLD_TAG_RE.match(seg)
            if b:
                bold = max(0, bold - 1) if b.group(1) else bold + 1
            pieces.append(seg)
        else:
            pieces.append(seg if depth else fn(seg, bool(bold)))
    return "".join(pieces)


def number_links(text: str) -> str:
    """Put each raw link (a bare URL, or an <a> whose label is a URL) behind a citation number:
    <a href="url">[1]</a>. The same URL keeps its number; numbering runs through the whole text."""
    numbers: dict[str, int] = {}

    def cite(m: re.Match) -> str:
        url, tail = m.group(0), ""
        while url and (url[-1] in ".,;:!?" or (url[-1] == ")" and url.count(")") > url.count("("))):
            url, tail = url[:-1], url[-1] + tail
        n = numbers.setdefault(url, len(numbers) + 1)
        return f'<a href="{url}">[{n}]</a>{tail}'

    text = RAW_ANCHOR_RE.sub(lambda m: m.group(1), text)
    text = _rewrite(text, lambda seg, bold: URL_RE.sub(cite, seg))
    # Links the model put on their own lines join the text before them: "volume.[1][2]"
    return CITE_ONLY_RUN_RE.sub(lambda m: re.sub(r"(?<=</a>)\s+", "", m.group(1)), text)


def link_tickers(text: str) -> str:
    """Number raw links, wrap ticker-shaped words in Yahoo links and bold "Name (TICKER)", leaving
    tags and anything inside <a>, <code> or <pre> alone (so a link the model already wrote is
    never doubled), and adding no bold inside text that is already bold."""
    def wrap(m: re.Match) -> str:
        word = m.group(0)
        if len(word) > 1 and not is_ticker(word):
            return word
        return f'<a href="{yahoo_url(word)}">{ticker_label(word)}</a>'

    return _rewrite(number_links(text), lambda seg, bold: COMBINED_RE.sub(wrap, seg if bold else bold_names(seg)))
