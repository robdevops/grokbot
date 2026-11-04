"""Putting [n] source links after the sentences of an answer that a web search backed."""

from __future__ import annotations

import html
import logging
import re

from .base import Source

log = logging.getLogger("bot")

NUMBER_RE = re.compile(r"\d[\d.,]*\d|\d")
WORD_RE = re.compile(r"[^\W\d_]{6,}")
SENTENCE_END_RE = re.compile(r"(?<=[.!?])(?=\s)|\n+")
NUMBER_WEIGHT = 3.0  # a figure shared with a source says more than a word does
MIN_SCORE = 4.0  # below this a sentence is not taken to come from the source
MARKER_RE = re.compile(r'\[<a href="([^"]*)">\d+</a>\]')


def mark_citations(text: str, cites: list[tuple[int, str]]) -> str:
    """Put "[n]" after each cited sentence, n a link to the page (the same page keeps its number). `cites` are
    (end position in `text`, url); a position inside a tag or a link is skipped, and one right before the
    sentence's closing punctuation moves past it."""
    numbers: dict[str, int] = {}
    at: dict[int, list[int]] = {}
    for end, url in sorted(cites):
        pos = min(end, len(text))
        while pos < len(text) and text[pos] in ".!?":
            pos += 1
        before = text[:pos]
        if (pos <= 0 or before.rfind("<") > before.rfind(">") or before.count("<a ") != before.count("</a>")
                or before.rfind("](") > before.rfind(")")):
            continue
        n = numbers.setdefault(url, len(numbers) + 1)
        if n not in at.setdefault(pos, []):
            at[pos].append(n)
    urls = {n: url for url, n in numbers.items()}
    for pos in sorted(at, reverse=True):
        marks = "".join(f'[<a href="{html.escape(urls[n], quote=True)}">{n}</a>]' for n in at[pos])
        text = text[:pos] + marks + text[pos:]
    return text


def normalise_markers(text: str, sources: list[Source]) -> str:
    """The model's own [n] links kept only where they point at a page the search really returned, numbered 1, 2, ...
    in the order they appear (a link to a page it made up is dropped, marker and all)."""
    known = {s.url.rstrip("/"): s.url for s in sources}
    numbers: dict[str, int] = {}

    def fix(m: re.Match) -> str:
        url = known.get(html.unescape(m.group(1)).rstrip("/"))
        if url is None:
            return ""
        n = numbers.setdefault(url, len(numbers) + 1)
        return f'[<a href="{html.escape(url, quote=True)}">{n}</a>]'

    return MARKER_RE.sub(fix, text)


def _tokens(text: str) -> tuple[set[str], set[str]]:
    plain = re.sub(r"<[^>]+>", " ", text)
    return ({n.replace(",", "") for n in NUMBER_RE.findall(plain) if len(n) > 1},
            {w.lower() for w in WORD_RE.findall(plain)})


def mark_by_overlap(text: str, sources: list[Source]) -> str:
    """Put "[n]" after the sentence of `text` that each source's snippet fits best, when it fits well enough.

    OpenRouter says which pages a search used but not which sentence each backs (its positions are 0), so the
    link goes where the figures and the distinctive words of the sentence are also in the page's snippet.
    Words every snippet shares count for less. A source that fits no sentence gets no marker."""
    if MARKER_RE.search(text) or not sources:
        return text
    snippets = [_tokens(s.content) for s in sources if s.content]
    if not snippets:
        return text
    spread: dict[str, int] = {}
    for _, words in snippets:
        for w in words:
            spread[w] = spread.get(w, 0) + 1
    ends, start = [], 0
    for m in SENTENCE_END_RE.finditer(text):
        ends.append((start, m.start()))
        start = m.end()
    ends.append((start, len(text)))
    cites = []
    for source in sources:
        numbers, words = _tokens(source.content)
        best, best_end = 0.0, None
        for lo, hi in ends:
            sentence_numbers, sentence_words = _tokens(text[lo:hi])
            score = NUMBER_WEIGHT * len(sentence_numbers & numbers)
            score += sum(1 - 0.6 * (spread[w] - 1) / max(1, len(snippets) - 1) for w in sentence_words & words)
            if score > best:
                best, best_end = score, hi
        if best_end is not None and best >= MIN_SCORE:
            cites.append((best_end, source.url))
            log.debug("Citation %s fits the sentence ending at %d (score %.1f)", source.url, best_end, best)
        else:
            log.debug("Citation %s fits no sentence (best score %.1f)", source.url, best)
    return mark_citations(text, cites)
