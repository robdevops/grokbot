"""/narrative and /events: TickerScout's 10-K/10-Q narrative and 8-K event digest, a section at a time, no model."""

from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .. import config
from ..ask import ask
from ..context import Ctx
from ..llm.gate import Route
from ..prompts import FILING_PROMPT
from ..textfmt import md_to_html, split_html

log = logging.getLogger("bot")

PREFIX = "fl:"  # callback data: fl:<kind code>:<ticker>:<index of the section>
TICKER = re.compile(r"^[A-Z0-9]{1,6}(?:[.-][A-Z]{1,3})?$")
TRUNCATED = re.compile(r"\n?\.\.\.\[truncated; (\d+) chars total\]\s*$")  # what MCPServer appends past its size cap
DATE = re.compile(r"\b(?:(\d{4})-(\d{2})-(\d{2})|([A-Z][a-z]{2,8})\.? (\d{1,2}), (\d{4}))\b")  # 2026-10-22, October 22, 2026
UNIT_START = re.compile(r"^(?:#{2,6}\s|\s*(?:[-*\u2022]\s+)?(?:\*\*)?(?:\d{4}-\d{2}-\d{2}|[A-Z][a-z]{2,8}\.? \d{1,2}, \d{4}))")
ROW = 3  # buttons per row
LIMIT = config.MAX_TG_MESSAGE  # one section is one message, so that a button can edit it in place
_shortened: dict[tuple, str] = {}  # (kind, ticker, section, length of the text) -> the model's short version
_SHORTENED_MAX = 64


@dataclass(frozen=True)
class Kind:
    code: str
    command: str
    tool: str
    noun: str
    sections: tuple[tuple[str, str], ...]  # (button label, `section` argument); the first is the default
    oldest_first: bool  # dated items are shown in ascending order
    via_model: bool  # long prose that rarely fits one message: the model gets the tool itself, no look at the size first


KINDS = {
    "n": Kind("n", "narrative", "get_narrative", "narrative", (
        ("Business", "business"), ("Risk factors", "risk factors"), ("MD&A", "md&a"),
        ("Current quarter", "current quarter"), ("Subsequent events", "subsequent events"), ("Legal", "legal")), False, True),
    "e": Kind("e", "events", "get_events", "events", (
        ("Earnings", "earnings"), ("Management", "management changes"), ("Capital returns", "capital returns"),
        ("Debt & financing", "debt and financing"), ("M&A", "m&a"), ("Governance", "governance"),
        ("Restructuring", "restructuring"), ("Legal", "legal")), True, False),
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


def keyboard(kind: Kind, ticker: str, current: int | None) -> InlineKeyboardMarkup:
    """A button for each section, the one shown shaded (Telegram's "primary" style, sent through api_kwargs)."""
    buttons = [InlineKeyboardButton(label, callback_data=f"{PREFIX}{kind.code}:{ticker}:{i}",
                                    api_kwargs={"style": "primary"} if i == current else None)
               for i, (label, _) in enumerate(kind.sections)]
    return InlineKeyboardMarkup([buttons[i:i + ROW] for i in range(0, len(buttons), ROW)])


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


def cut_note(text: str) -> tuple[str, str]:
    """The text without the "[truncated; N chars total]" line the MCP server appends past its size cap, and a note
    for the reader if it was there."""
    cut = TRUNCATED.search(text)
    if not cut:
        return text, ""
    return text[:cut.start()], (f"\n\n<i>Cut at the size limit: the full text is {int(cut.group(1)):,} characters. "
                               "Pick a narrower section below.</i>")


def to_html(text: str) -> str:
    """The tool's Markdown (headings, bullets, **bold**, *italic*) as Telegram HTML."""
    lines = []
    for line in html.escape(text.strip(), quote=False).split("\n"):
        line = re.sub(r"^\s*[-*]\s+", "\u2022 ", line)
        line = re.sub(r"^#{1,6}\s+(.*)$", r"<b>\1</b>", line)
        lines.append(line)
    body = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<b>\1</b>", "\n".join(lines))
    return re.sub(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])", r"<i>\1</i>", body)


def date_of(line: str) -> datetime | None:
    """The first date in a line: 2026-10-22, October 22, 2026 or Oct 22, 2026."""
    for m in DATE.finditer(line):
        try:
            if m.group(1):
                return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            for fmt in ("%B %d %Y", "%b %d %Y"):
                try:
                    return datetime.strptime(f"{m.group(4)} {m.group(5)} {m.group(6)}", fmt)
                except ValueError:
                    continue
        except ValueError:
            continue
    return None


def oldest_first(text: str) -> str:
    """The dated items of a digest in ascending order. An item starts at a heading or a bullet that opens with a date,
    and its date is the first in that line; the lines before the first item (the title) stay on top. Text whose items
    aren't all dated is left as it is: reordering what can't be read would scramble it."""
    lines = text.split("\n")
    starts = [i for i, line in enumerate(lines) if UNIT_START.match(line)]
    if len(starts) < 2:
        return text
    units = [lines[a:b] for a, b in zip(starts, starts[1:] + [len(lines)], strict=True)]
    dates = [date_of(unit[0]) for unit in units]
    if not all(dates):
        log.debug("Events: %d items, not all dated; leaving the order alone", len(units))
        return text
    ordered = [unit for _, unit in sorted(zip(dates, units, strict=True), key=lambda pair: pair[0])]
    return "\n".join(lines[:starts[0]] + [line for unit in ordered for line in unit])


def fit(body: str, limit: int) -> str:
    """The text cut to `limit` characters at a line end, with its tags still balanced."""
    chunks = split_html(body, limit)
    return chunks[0] + ("\u2026" if len(chunks) > 1 else "")


async def summarise(ctx: Ctx, kind: Kind, ticker: str, section: str, limit: int, chat_id: int) -> str | None:
    """The model's version of a section, to fit `limit` characters. The model calls the tool itself, so a reply that
    runs out of tokens while thinking over the result is retried by the tool loop. Remembered, so a repeat tap costs
    nothing. None if there is no data source or the model fails."""
    key = (kind.code, ticker, section)
    if key in _shortened:
        return _shortened[key]
    server = next((s for s in ctx.registry.up() if kind.tool in s.fn_names.values()), None)
    if server is None:
        return None
    order = " List dated items in ascending date order, oldest first." if kind.oldest_first else ""
    prompt = FILING_PROMPT.format(tool=kind.tool, ticker=ticker, section=section, limit=limit, order=order)
    try:
        answer = await ask(ctx, [{"type": "text", "text": prompt}], Route([server], False), kind="filing", chat_id=chat_id)
    except Exception as e:
        log.warning("The model couldn't write the %s %s for %s: %s", kind.noun, section, ticker, e)
        return None
    if len(_shortened) >= _SHORTENED_MAX:
        _shortened.clear()
    _shortened[key] = md_to_html(answer.text.strip())
    return _shortened[key]


async def message_for(ctx: Ctx, kind: Kind, ticker: str, section: str, chat_id: int) -> str | None:
    """The section as one message of Telegram HTML, or None if there is none. Narrative goes to the model at once;
    events are fetched and shown as they are unless too long for one message, when the model shortens them. Where
    the model fails, the start of the text is shown."""
    if kind.via_model and (short := await summarise(ctx, kind, ticker, section, LIMIT, chat_id)):
        return fit(short, LIMIT)
    text = await fetch(ctx, kind, ticker, section)
    if not text:
        return None
    text, note = cut_note(text)
    body, room = to_html(oldest_first(text) if kind.oldest_first else text), LIMIT - len(note)
    if len(body) > room and not kind.via_model:
        body = await summarise(ctx, kind, ticker, section, room, chat_id) or body
    return fit(body, room) + note
