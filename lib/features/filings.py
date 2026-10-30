"""/narrative and /events: TickerScout's 10-K/10-Q narrative and 8-K event digest, a section at a time, no model."""

from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import dataclass

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from ..context import Ctx

log = logging.getLogger("bot")

PREFIX = "fl:"  # callback data: fl:<kind code>:<ticker>:<index of the section>
TICKER = re.compile(r"^[A-Z0-9]{1,6}(?:[.-][A-Z]{1,3})?$")
TRUNCATED = re.compile(r"\n?\.\.\.\[truncated; (\d+) chars total\]\s*$")  # what MCPServer appends past its size cap
ROW = 3  # buttons per row


@dataclass(frozen=True)
class Kind:
    code: str
    command: str
    tool: str
    noun: str
    sections: tuple[tuple[str, str], ...]  # (button label, `section` argument); the first is the default


KINDS = {
    "n": Kind("n", "narrative", "get_narrative", "narrative", (
        ("All", "all"), ("Business", "business"), ("Risk factors", "risk factors"), ("MD&A", "md&a"),
        ("Current quarter", "current quarter"), ("Subsequent events", "subsequent events"), ("Legal", "legal"))),
    "e": Kind("e", "events", "get_events", "events", (
        ("Earnings", "earnings"), ("Management", "management changes"), ("Capital returns", "capital returns"),
        ("Debt & financing", "debt and financing"), ("M&A", "m&a"), ("Governance", "governance"),
        ("Restructuring", "restructuring"), ("Legal", "legal"))),
}


def pick(kind: Kind, words: list[str]) -> tuple[int | None, str]:
    """The section a command names (its index, and the `section` argument): the default for none, a known one by its
    label or the start of its name, else the words as they are, which the tool accepts as a heading."""
    if not words:
        return 0, kind.sections[0][1]
    wanted = " ".join(words).lower()
    for i, (label, value) in enumerate(kind.sections):
        if wanted in (label.lower(), value) or (len(wanted) >= 3 and value.startswith(wanted)):
            return i, value
    return None, " ".join(words)


def keyboard(kind: Kind, ticker: str, current: int | None) -> InlineKeyboardMarkup | None:
    """A button for each section but the one shown."""
    buttons = [InlineKeyboardButton(label, callback_data=f"{PREFIX}{kind.code}:{ticker}:{i}")
               for i, (label, _) in enumerate(kind.sections) if i != current]
    return InlineKeyboardMarkup([buttons[i:i + ROW] for i in range(0, len(buttons), ROW)]) if buttons else None


async def fetch(ctx: Ctx, kind: Kind, ticker: str, section: str) -> str | None:
    """The section's text, or None when the data source is missing or down, or has nothing for this ticker."""
    server = next((s for s in ctx.registry.up() if kind.tool in s.fn_names.values()), None)
    if server is None:
        return None
    out = await server.call(kind.tool, {"ticker": ticker, "section": section})
    if out.startswith(("Tool error", "Error")):
        log.warning("%s %s %s: %s", kind.tool, ticker, section, " ".join(out.split())[:200])
        return None
    try:
        data = json.loads(out)
    except ValueError:
        return out  # markdown, the usual answer
    if isinstance(data, dict) and data.get("available") is False:
        return str(data.get("message") or "") or None
    log.warning("%s %s %s: unexpected JSON result: %s", kind.tool, ticker, section, " ".join(out.split())[:200])
    return None


def to_html(text: str) -> str:
    """The tool's Markdown (headings, bullets, **bold**, *italic*) as Telegram HTML, with a note if the server cut it."""
    note = ""
    if cut := TRUNCATED.search(text):
        text = text[:cut.start()]
        note = f"\n\n<i>Shortened: the full text is {int(cut.group(1)):,} characters. Pick a narrower section below.</i>"
    lines = []
    for line in html.escape(text.strip(), quote=False).split("\n"):
        line = re.sub(r"^\s*[-*]\s+", "• ", line)
        line = re.sub(r"^#{1,6}\s+(.*)$", r"<b>\1</b>", line)
        lines.append(line)
    body = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<b>\1</b>", "\n".join(lines))
    body = re.sub(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])", r"<i>\1</i>", body)
    return body + note
