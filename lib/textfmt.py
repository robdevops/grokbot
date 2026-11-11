"""Model text -> Telegram HTML: Markdown fallback, disclaimer stripping, tag-safe splitting."""

from __future__ import annotations

import html
import re
from bisect import bisect_right
from itertools import accumulate

from .config import MAX_TG_MESSAGE

# [label](url), where label may itself be bracketed, as in [[1]](url) citations
MD_LINK = re.compile(r"\[(\[[^\]]*\]|[^\[\]]+)\]\((https?://[^\s)]+)\)")
MD_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*|__(?=\S)(.+?)(?<=\S)__")
MD_CODE = re.compile(r"`([^`\n]+)`")

# "Not advice." / "NFA" / "(DYOR)" tacked onto the end of a reply
TRAILING_DISCLAIMER = re.compile(
    r"(?:\s*[\(\[]?\s*(?:this is )?(?:not (?:financial |investment )?advice|nfa|dyor)\b[.!]?\s*[\)\]]?[.!]?)+\s*$",
    re.IGNORECASE,
)

A_TAG_RE = re.compile(r'<a href="([^"]*)">(.*?)</a>')
ANY_TAG_RE = re.compile(r"<[^>]+>")
HTML_TAG_RE = re.compile(r"<(/?)([a-z][a-z0-9-]*)\b[^>]*>", re.I)
# Telegram's limits, in characters of parsed text (`visible_len`)
TG_TEXT_LIMIT = 4096
TG_CAPTION_LIMIT = 1024
ENTITY_RE = re.compile(r"&(?:#\d+|#x[0-9a-f]+|[a-z]+);", re.I)

# Telegram's own tags, plus one half-written tag at the end of the text. A bare "<" in prose
# ("<5% from ATH") is not a tag and must survive.
_TG_TAGS = r"(?:b|strong|i|em|u|s|code|pre|a|blockquote|tg-spoiler)"
TG_TAG_RE = re.compile(rf"</?{_TG_TAGS}\b[^>]*>|</?{_TG_TAGS}\b[^>]*$", re.I)

# Some models print a tool call as text. A closed block is removed whole; an unclosed one only
# to the end of its paragraph, so a stray tag early in a reply can't delete the answer after it.
TOOL_SYNTAX_RE = re.compile(
    r"<tool_call>.*?</tool_call>|<tool_call>.*?(?:\n\s*\n|\Z)"
    r"|<function=.*?</function>|<function=.*?(?:\n\s*\n|\Z)|<\|?tool_calls?\|?>",
    re.DOTALL | re.IGNORECASE,
)


def strip_disclaimer(text: str) -> str:
    """Models add these despite the prompt, and copy their own earlier ones from the transcript."""
    return TRAILING_DISCLAIMER.sub("", text).rstrip()


def md_to_html(text: str) -> str:
    """Convert Markdown that slips into replies (links, **bold**, `code`) into Telegram HTML."""
    def link(m: re.Match) -> str:
        return f'<a href="{html.escape(m.group(2), quote=True)}">{m.group(1)}</a>'

    text = MD_LINK.sub(link, text)
    text = MD_BOLD.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", text)
    return MD_CODE.sub(r"<code>\1</code>", text)


def plain_text(text: str, limit: int | None = None) -> str:
    """Telegram HTML as plain text: links become "label (url)", other tags are dropped. Past `limit` characters
    (Telegram's count) the links lose their URLs, so text that fitted as HTML still fits as plain text."""
    plain = html.unescape(ANY_TAG_RE.sub("", A_TAG_RE.sub(r"\2 (\1)", text)))
    if limit is not None and _units(plain) > limit:
        return html.unescape(ANY_TAG_RE.sub("", text))
    return plain


def _units(text: str) -> int:
    """Length as Telegram counts it: UTF-16 code units, so an emoji is 2."""
    return len(text.encode("utf-16-le")) // 2


def _visible_upto(text: str) -> list[int]:
    """Characters Telegram counts in text[:i], for every i: tags count nothing, an entity counts as the character it
    stands for."""
    widths = [2 if ord(c) > 0xFFFF else 1 for c in text]
    for m in ENTITY_RE.finditer(text):
        widths[m.start():m.end()] = [_units(html.unescape(m.group()))] + [0] * (m.end() - m.start() - 1)
    for m in HTML_TAG_RE.finditer(text):  # after entities: an &amp; inside an href counts nothing
        widths[m.start():m.end()] = [0] * (m.end() - m.start())
    return list(accumulate(widths, initial=0))


def visible_len(text: str) -> int:
    """How long Telegram takes this HTML to be (its message and caption limits count the parsed text, not the
    markup): links count only their label, however long the URL."""
    return _visible_upto(text)[-1]


def is_parse_error(e: Exception) -> bool:
    return "parse entities" in str(e).lower()


def split_html(text: str, size: int = MAX_TG_MESSAGE) -> list[str]:
    """Split Telegram HTML into messages of at most `size` characters as Telegram counts them (`visible_len`: the
    markup and link URLs don't count, so a reply full of links is not split early).

    Cuts at a newline where it can, never inside a tag or an entity, and keeps every message
    well-formed: tags still open at a cut are closed there and re-opened at the start of the
    next message, so links and bold survive a split."""
    chunks: list[str] = []
    stack: list[tuple[str, str]] = []  # (name, opening tag) still open after the last chunk
    while True:
        reopen = "".join(tag for _, tag in stack)
        upto = _visible_upto(text)
        if upto[-1] <= size:
            break
        room = max(bisect_right(upto, size) - 1, 1)  # the longest prefix that fits
        cut = text.rfind("\n", 0, room)
        if cut <= 0:
            cut = room
        lt = text.rfind("<", 0, cut)
        if lt > text.rfind(">", 0, cut) and re.match(r"</?[a-zA-Z]", text[lt:lt + 3]):
            cut = lt  # inside a tag (a bare "<" in prose is not one)
        amp = text.rfind("&", max(0, cut - 10), cut)
        if amp != -1 and ";" not in text[amp:cut]:
            cut = amp  # inside an entity
        cut = max(cut, 1)
        head, text = reopen + text[:cut], text[cut:].lstrip()
        stack = _open_tags(head)
        chunks.append(head + "".join(f"</{n}>" for n, _ in reversed(stack)))
    if text.strip():
        chunks.append(reopen + text)
    return chunks


def _open_tags(fragment: str) -> list[tuple[str, str]]:
    stack: list[tuple[str, str]] = []
    for m in HTML_TAG_RE.finditer(fragment):
        name = m.group(2).lower()
        if not m.group(1):
            stack.append((name, m.group(0)))
            continue
        for i in range(len(stack) - 1, -1, -1):
            if stack[i][0] == name:
                del stack[i]
                break
    return stack
