"""Reply filter: Yahoo links on marked tickers ($NVDA, NXT.AX, ASX:NXT, Name (NVDA)) and known ones, raw links as [n]."""

from __future__ import annotations

import re
from collections.abc import Mapping
from functools import lru_cache

from .quote_links import quote_url
from .tickers_data import EXCHANGE_PREFIXES, EXCHANGE_SUFFIXES, NOT_TICKERS, WORD_TICKERS

# TICKER_RE, ONE_LETTER_RE and is_ticker read the user's text (llm/gate.py, charts/breakdown.py); replies link
# only the marked forms below and known tickers.
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
# A capitalised word that opens a sentence is not part of the company name ("Today Micron (MU)").
SENTENCE_OPENERS = {"Today", "Also", "Meanwhile", "And", "But", "So", "Then", "Yesterday", "Tonight",
                    "Still", "Plus", "Even", "Now", "Overall", "However", "Elsewhere"}


def is_ticker(word: str) -> bool:
    base, _, suffix = word.partition(".")
    if suffix in EXCHANGE_SUFFIXES and base not in NOT_TICKERS:
        return True  # an exchange suffix settles it, even for numeric codes like 9988.HK
    return not (word in NOT_TICKERS or NOT_TICKER_RE.match(word))


def ticker_label(symbol: str) -> str:
    """What the reader sees: SQX.AX becomes SQX, BRK.B stays BRK.B."""
    base, _, suffix = symbol.partition(".")
    return base if suffix in EXCHANGE_SUFFIXES else symbol


# Known tickers a reply may name bare (code -> Yahoo symbol): the S&P 500, the NASDAQ-100 and US holdings, refreshed
# daily by features/known_tickers.py and read from here, never fetched while answering.
KNOWN: dict[str, str] = {}


def set_known(codes: Mapping[str, str]) -> None:
    KNOWN.clear()
    KNOWN.update(codes)


def yahoo_symbol(code: str, market: str) -> str | None:
    """Yahoo's symbol for a code on an exchange ("NXT", "ASX" -> "NXT.AX"), or None for an exchange we don't know."""
    suffix = EXCHANGE_PREFIXES.get(market.upper())
    if suffix is None:
        return None
    if market.upper() == "TSE" and code.isdigit():
        suffix = "T"  # Tokyo; Toronto's codes are letters
    if suffix == "HK" and code.isdigit():
        code = code.zfill(4)
    return f"{code}.{suffix}" if suffix else code


def _alt(words) -> str:
    return "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))


_CODE = r"(?:[A-Z][A-Z0-9]{0,5}(?:-[A-Z]{1,2})?|\d{3,6})"
_PREFIX = _alt(EXCHANGE_PREFIXES)
_SUFFIX = _alt(EXCHANGE_SUFFIXES)
_WORD = r"[A-Z0-9](?:&amp;|[\w&'’-])*(?:\.(?:&amp;|[\w&'’-])+)*"  # AT&amp;T, S&amp;P
_MARKERS = (
    # "Micron (MU)", "NextDC (ASX:NXT)", "DUG Tech (DUG.AX)", "NXT (ASX)": up to four capitalised words, then a paren
    rf"(?<![\w&'’.;-])(?P<name>(?:(?:{_WORD}|&amp;)[ ]){{0,3}}{_WORD})[ ]\((?P<paren>[A-Z0-9][A-Z0-9.:-]*(?:[ ][A-Z0-9.-]+)?)\)"
    rf"|(?<![\w$.^:-])(?:(?P<exch>{_PREFIX}):[ ]?(?P<ecode>{_CODE})"  # ASX:NXT, NASDAQ: NVDA
    r"|\$(?P<cash>[A-Z][A-Z0-9]{1,5}(?:[.-][A-Z]{1,2}){0,2})"  # $NVDA, $SQX.AX, $BRK-B (never $A, $600, $1.2B)
    rf"|(?P<suff>(?:[A-Z][A-Z0-9]{{1,5}}|\d{{3,6}})(?:-[A-Z]{{1,2}})?\.(?:{_SUFFIX}))"  # NXT.AX, 9988.HK, RCI-B.TO
    r"|\^(?P<idx>[A-Z0-9]{1,8})"  # ^GSPC
    r"|(?P<cry>[A-Z]{2,6})-USD"  # BTC-USD
    r"{known})(?![\w-]|\.\w)"
)


@lru_cache(maxsize=16)
def _link_re(codes: frozenset[str]) -> re.Pattern:
    known = rf"|\b(?P<known>{_alt(codes)})\b" if codes else ""
    return re.compile(_MARKERS.replace("{known}", known))


def _link(symbol: str, label: str, market: str = "") -> str:
    return f'<a href="{quote_url(symbol, market)}">{label}</a>'


def _paren(paren: str, known: Mapping[str, str]) -> str | None:
    """The Yahoo symbol of what "Name (...)" holds: EXCH:CODE, CODE.SUFFIX or a known ticker; None for anything else."""
    exch, colon, code = paren.partition(":")
    if colon:
        code = code.strip()
        return yahoo_symbol(code, exch) if re.fullmatch(_CODE, code) else None
    base, _, suffix = paren.rpartition(".")
    if base and suffix in EXCHANGE_SUFFIXES and re.fullmatch(_CODE, base) and _code_ok(base):
        return paren
    # A bare code says nothing about its exchange ("Goodman Group (GMG)" is GMG.AX), so only a known one is linked.
    return known.get(paren) or known.get(paren.replace(".", "-"))


def _code_ok(base: str) -> bool:
    """A code before an exchange suffix: the suffix settles it (ALL.AX is Aristocrat), but FY2026 and Q3 are not codes."""
    return base.isdigit() or not NOT_TICKER_RE.match(base)


def _wrap(m: re.Match, bold: bool, known: Mapping[str, str], pattern: re.Pattern) -> str:
    if m.group("name"):
        name, paren = m.group("name"), m.group("paren")
        words = name.split(" ")

        def plain() -> str:  # not a ticker in brackets ("NVDA (Q3 FY26)"): the words before it may still hold one
            return pattern.sub(lambda m2: _wrap(m2, bold, known, pattern), name) + m.group(0)[len(name):]

        if paren in EXCHANGE_PREFIXES:  # Sharesight's "NXT (ASX)": link the code, keep the exchange as text
            code = words[-1]
            symbol = yahoo_symbol(code, paren) if re.fullmatch(_CODE, code) else None
            if symbol is None or (not code.isdigit() and not is_ticker(code) and code not in known):
                return plain()
            return " ".join(words[:-1] + [_link(symbol, code, paren)]) + f" ({paren})"
        symbol = _paren(paren, known)
        if symbol is None:
            return plain()
        label = paren.partition(":")[2].strip() or ticker_label(paren)
        skip = 0
        while skip < len(words) - 1 and words[skip] in SENTENCE_OPENERS:
            skip += 1
        lead = " ".join(words[:skip]) + " " if skip else ""
        body = f"{' '.join(words[skip:])} ({_link(symbol, label, paren.partition(':')[0] if ':' in paren else '')})"
        return lead + (body if bold else f"<b>{body}</b>")
    if m.group("exch"):
        code = m.group("ecode")
        symbol = yahoo_symbol(code, m.group("exch"))
        return _link(symbol, code, m.group("exch")) if symbol else m.group(0)
    if m.group("cash"):
        code = m.group("cash")
        base, _, suffix = code.rpartition(".")
        if suffix in EXCHANGE_SUFFIXES and base:
            return _link(code, ticker_label(code)) if _code_ok(base) else m.group(0)  # $ALL.AX
        symbol = known.get(code) or known.get(code.replace(".", "-"))
        if symbol is None and not is_ticker(re.split(r"[.-]", code)[0]):
            return m.group(0)  # $AUD, $US
        return _link(symbol or code.replace(".", "-"), code)
    if m.group("suff"):
        code = m.group("suff")
        base = code.rsplit(".", 1)[0]
        return _link(code, ticker_label(code)) if _code_ok(base) else m.group(0)
    if m.group("idx"):
        return _link("^" + m.group("idx"), "^" + m.group("idx"))
    if m.group("cry"):
        return _link(m.group("cry") + "-USD", m.group("cry"))
    code = m.group("known")
    return _link(known[code], code)


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


def link_tickers(text: str, known: Mapping[str, str] | None = None) -> str:
    """Number raw links, then link the tickers written in a marked form ($NVDA, NXT.AX, ASX:NXT, Name (NVDA), NXT (ASX),
    ^GSPC, BTC-USD) and the bare words that are known tickers (KNOWN, plus `known`: code -> Yahoo symbol), bolding
    "Name (TICKER)" unless it is already bold. Any other capitalised word stays plain, so acronyms are never linked.
    Tags and anything inside <a>, <code> or <pre> are left alone, so a link the model wrote is never doubled."""
    codes = {**KNOWN, **(known or {})}
    bare = frozenset(c for c in codes if len(c) > 1 and not c.isdigit() and c not in NOT_TICKERS | WORD_TICKERS)
    pattern = _link_re(bare)
    return _rewrite(number_links(text), lambda seg, bold: pattern.sub(lambda m: _wrap(m, bold, codes, pattern), seg))
