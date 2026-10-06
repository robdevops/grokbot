"""Model text -> Telegram HTML: Markdown fallback, disclaimer stripping, tag-safe splitting."""

from __future__ import annotations

import html
import re

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


def plain_text(text: str) -> str:
    """Telegram HTML as plain text: links become "label (url)", other tags are dropped."""
    return html.unescape(ANY_TAG_RE.sub("", A_TAG_RE.sub(r"\2 (\1)", text)))


def is_parse_error(e: Exception) -> bool:
    return "parse entities" in str(e).lower()


def split_html(text: str, size: int = MAX_TG_MESSAGE - 96) -> list[str]:
    """Split Telegram HTML into messages of at most `size` characters.

    Cuts at a newline where it can, never inside a tag or an entity, and keeps every message
    well-formed: tags still open at a cut are closed there and re-opened at the start of the
    next message, so links and bold survive a split."""
    chunks: list[str] = []
    stack: list[tuple[str, str]] = []  # (name, opening tag) still open after the last chunk
    while True:
        reopen = "".join(tag for _, tag in stack)
        if len(reopen) + len(text) <= size:
            break
        room = max(size - len(reopen) - sum(len(n) + 3 for n, _ in stack) - 32, 200)
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
