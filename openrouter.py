#!/usr/bin/env python3
"""
Telegram group bot that answers @mentions with an LLM via OpenRouter, using the
group's chat history as context.

Every message the bot sees is logged to SQLite. When someone @mentions the bot, or
replies to one of its messages, the recent log goes to the model as a transcript, so
it can answer things like "what's with that last message?". Photos in the mention, or
in the message being replied to, are sent along too.

MCP servers listed in mcp_servers.json (e.g. Yahoo Finance, Sharesight) are started by
the bot itself and their tools are offered to the model as function tools. The bot runs
the tool calls locally (up to MAX_TOOL_ROUNDS rounds per answer), so the servers never
need to be reachable from the internet and credentials stay on this machine. Private
chats are answered too, with the reply streamed into a Telegram draft as it's written.

Search: the model is given OpenRouter's web_search tool. OpenRouter sometimes runs it
server-side and sometimes hands the call back, so the bot executes any tool call it
receives by running the query through a cheap ":online" model.

Env vars:
  TELEGRAM_BOT_TOKEN   required
  OPENROUTER_API_KEY   required
  MODEL                default xiaomi/mimo-v2.6-pro (no ":online" suffix)
  SEARCH_MODEL         runs the model's queries, default xiaomi/mimo-v2.6-flash:online
  SEARCH               on | off (default on)
  SEARCH_ENGINE        auto | native | exa (default auto)
  MAX_RESULTS          search results per request, default 20
  SEARCH_TOKENS        cap on each search write-up, default 400 - the search step's
                       generation time is most of the added latency
  REASONING            low | medium | high; empty = model default
  MAX_TOKENS           reply cap, default 4000 (counts reasoning tokens too)
  TEMPERATURE          default 0.6
  HISTORY_LIMIT        messages of context, default 20 (the window is 20-29, see transcript())
  MCP_CONFIG           MCP server config, default mcp_servers.json (missing = no MCP tools)
  MCP_TIMEOUT          seconds per MCP tool call, default 60
  MAX_TOOL_ROUNDS      model <-> tool round trips per answer, default 6
  MAX_TOOL_OUTPUT      chars of one tool result sent to the model, default 50000
  ALERT_CHAT_IDS       comma- or space-separated chat IDs told when an MCP server goes down;
                       empty = no alerts
  MAX_IMAGES           images per request, default 2 (0 disables vision)
  OWNER_USER_ID        your Telegram user ID, for /credits
  QUIET                on | off (default on) - send replies without a notification sound
  EXTRA_TICKERS        extra symbols to always bold, e.g. "BRK.B ^SIL-IV"
  NOT_TICKERS          extra words to never bold, added to the built-in stoplist
  AUTO_BOLD            on | off (default off) - bold ticker-shaped words in code
  AUTO_LINK            on | off (default on) - link ticker-shaped words to Yahoo Finance
                       (ignored when AUTO_BOLD is on; one or the other, not both)
  CRYPTO_TICKERS       symbols needing Yahoo's "-USD" suffix, added to the built-in list
  LINK_PREVIEW         on | off (default off) - Telegram's preview card for the first
                       link, which is large and unhelpful when every ticker is a link
  SPACE_ITEMS          on | off (default on) - blank line between consecutive lines
  BOT_TZ               e.g. Australia/Melbourne; default UTC
  DB_PATH              default chat_log.db (share it between bots in the same group)
"""

import asyncio
import base64
import html
import json
import logging
import os
import random
import re
import sqlite3
import tempfile
import time
from contextlib import AsyncExitStack
from datetime import datetime
from types import SimpleNamespace
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client
from openai import APIStatusError, AsyncOpenAI
from telegram import LinkPreviewOptions, Message, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, NetworkError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------- config ----

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]
MODEL = os.getenv("MODEL", "xiaomi/mimo-v2.6-pro")
SEARCH_MODEL = os.getenv("SEARCH_MODEL", "xiaomi/mimo-v2.6-flash:online").strip()
SEARCH = os.getenv("SEARCH", "on").strip().lower() != "off"
SEARCH_ENGINE = os.getenv("SEARCH_ENGINE", "auto").strip().lower()
MAX_RESULTS = int(os.getenv("MAX_RESULTS", "20"))
SEARCH_TOKENS = int(os.getenv("SEARCH_TOKENS", "400"))
REASONING = os.getenv("REASONING", "").strip().lower()
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "4000"))
TEMPERATURE = float(os.getenv("TEMPERATURE", "0.6"))
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "20"))  # window is 20-29 messages, see transcript()
MAX_IMAGES = int(os.getenv("MAX_IMAGES", "2"))
QUIET = os.getenv("QUIET", "on").strip().lower() != "off"
AUTO_BOLD = os.getenv("AUTO_BOLD", "off").strip().lower() == "on"
AUTO_LINK = os.getenv("AUTO_LINK", "on").strip().lower() != "off"
LINK_PREVIEW = os.getenv("LINK_PREVIEW", "off").strip().lower() == "on"
EXTRA_TICKERS = {t.upper() for t in os.getenv("EXTRA_TICKERS", "").replace(",", " ").split()}
EXTRA_NOT_TICKERS = {t.upper() for t in os.getenv("NOT_TICKERS", "").replace(",", " ").split()}
# Yahoo quotes crypto as BTC-USD, not BTC.
CRYPTO = {"BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "BNB", "LTC", "DOT", "AVAX",
          "LINK", "TRX", "SHIB", "PEPE", "WLFI", "USDT", "USDC"} | {
    t.upper() for t in os.getenv("CRYPTO_TICKERS", "").replace(",", " ").split()}
SPACE_ITEMS = os.getenv("SPACE_ITEMS", "on").strip().lower() != "off"
DB_PATH = os.getenv("DB_PATH", "chat_log.db")
TZ = ZoneInfo(os.getenv("BOT_TZ", "UTC"))
OWNER_ID = int(os.getenv("OWNER_USER_ID", "0"))
# MCP servers (see mcp_servers.json). Missing file = no MCP tools.
MCP_CONFIG = os.getenv("MCP_CONFIG", "mcp_servers.json")
MCP_TIMEOUT = int(os.getenv("MCP_TIMEOUT", "60"))  # seconds per tool call
MAX_TOOL_ROUNDS = int(os.getenv("MAX_TOOL_ROUNDS", "6"))  # model <-> tools round trips per answer
MAX_TOOL_OUTPUT = int(os.getenv("MAX_TOOL_OUTPUT", "50000"))  # chars per tool result sent to the model
# Chats that get a message when an MCP server goes down. Empty = no alerts.
ALERT_CHATS = {
    int(x) for x in os.getenv("ALERT_CHAT_IDS", "").replace(",", " ").split()
}

MAX_TG_MESSAGE = 4000  # Telegram's limit is 4096
OPENROUTER_BASE = "https://openrouter.ai/api/v1"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")

llm = AsyncOpenAI(
    api_key=OPENROUTER_API_KEY,
    base_url=OPENROUTER_BASE,
    timeout=180,
    default_headers={"X-Title": "Telegram group bot"},
)

SEARCH_TOOL = [{
    "type": "openrouter:web_search",
    "parameters": {"engine": SEARCH_ENGINE, "max_total_results": MAX_RESULTS},
}]

SYSTEM_PROMPT = """You are {bot_name}, a bot in a serious Telegram group chat about stocks
and investing. You're a normal member of the group, not a character: don't roleplay a
persona, don't give yourself a backstory, and don't invent facts about yourself or about
people in the group. If you don't know something, say so. The current date and time is {now}.

The group covers tickers, earnings, macro, sector moves, crypto and trade ideas, with the
usual off-topic banter in between. Assume that context when something is ambiguous: a bare
ticker or company name is about the stock, and "how's it looking" is about price action.
Be concrete with numbers, levels and dates, name your source and its date for anything
time-sensitive, and flag when a figure may be stale. You're a group member talking markets,
not a licensed adviser - but don't pretend to certainty you don't have either.

You get the recent group history first, oldest first, each line like:
[#message_id] time Sender (replying to #id): text
Lines from "You" are your own earlier replies. Media appears as [photo], [voice] etc.

The FINAL message is the one that mentioned you - that's what you answer. When it says
"the last message", "that" or "above", it means the messages just before it in the history,
not itself. If it was a reply to a specific message, that message is quoted for you. Any
attached images come with it.

{search_note}

Style rules, follow them strictly:
- Talk like a participant in the chat: conversational, concise, a few sentences.
- Hard limit of 80 words for a single-subject answer, unless someone asks for detail.
- Asked about several tickers or items, give each its own short line - ticker, the move,
  then the driver in a dozen words - and budget about 25 words each.
- Put a blank line between every item and every paragraph. Never run lines together.
- Otherwise one to three short paragraphs. Never an essay, never a structured write-up.
- No headings, no section labels, no numbered outlines.
- Write ticker symbols as plain text (NVDA, SQX.AX, BTC). They are turned into links
  automatically, so don't wrap them in tags or markdown yourself.
- Non-US listings need their exchange suffix so the symbol resolves: SQX.AX, ENL.AX for
  the ASX, and likewise .L (London), .TO (Toronto), .HK (Hong Kong). The suffix is
  hidden from the reader, so always include it.
- Formatting is HTML, not markdown. The only tags allowed are <b>, <i>, <code> and
  <a href="URL">text</a>, and every tag must be closed. Never write ** or __ or #.
- Be blunt and direct. Give your actual opinion and concrete numbers when asked.
- When your confidence in an answer is low, say so with a rough percentage.
- No disclaimers, no moralising, no "it's important to note", no "as an AI" talk.
- Swearing and crude humour are fine when the group is doing it.
- Don't invent facts about yourself, the group or its members. Anything you say about
  people must come from the transcript. If you don't know, say you don't know.
- Don't roleplay a character or give yourself a personality bio, even if asked to
  "introduce yourself" - just say plainly what you are and what you can do.
- Refer to people by name, never by message ID.
- The history is the past. Never repeat complaints about tools, searches or errors from
  earlier replies as if they were happening now - describe only what happened in THIS
  reply. If you didn't search this time, don't claim a search failed."""

SEARCH_NOTE = """You have a web search tool. Use it for anything live or recent - prices, moves, news,
earnings, announcements - rather than answering from memory. The question may be as short
as "explain", so work out from the chat what to look up (which ticker, which exchange, what
timeframe) and put that in the query; the search cannot see the chat. Query the subject
matter - tickers, companies, events - never your own name, usernames or @handles from the
chat. Asked about several tickers, breadth beats depth: give every one its own short query
before you go back for detail on any of them, and keep each to about three results. Your
search budget is shared across the whole answer, so never spend it all on the first two
names and leave the rest unsearched. If it does run out, say which names you couldn't
cover rather than guessing at them. Cite the publication and its date - Reuters, Bloomberg,
MarketWatch, the company filing - never the search engine that found it (exa, google, bing
and openrouter are not sources). If sources disagree, go with the more credible and recent
one and note the disagreement in a few words. Say plainly if the results don't answer the
question - never invent numbers."""

NO_SEARCH_NOTE = """You have no web access and no tools. Answer from the chat history and your own knowledge,
say plainly what you can't check, and never output tool-call syntax or JSON as text."""

# -------------------------------------------------------------- database ----

# Bots can share one DB, so allow concurrent writers: WAL lets readers and a writer work
# at once, and the timeout makes a blocked write wait instead of failing.
db = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA busy_timeout=10000")
db.execute(
    """CREATE TABLE IF NOT EXISTS messages (
        chat_id    INTEGER,
        message_id INTEGER,
        sender     TEXT,
        text       TEXT,
        ts         INTEGER,
        reply_to   INTEGER,
        PRIMARY KEY (chat_id, message_id)
    )"""
)
db.commit()

MEDIA_KINDS = ("photo", "video", "animation", "voice", "video_note", "audio",
               "document", "sticker", "poll", "location", "contact")


def sender_name(msg: Message) -> str:
    if msg.from_user:
        u = msg.from_user
        return u.full_name + (f" (@{u.username})" if u.username else "")
    if msg.sender_chat:  # anonymous admins, linked channels
        return msg.sender_chat.title or "Anonymous"
    return "Unknown"


def describe(msg: Message) -> str:
    """Text of a message, with a [media] tag for non-text content.

    Incoming updates carry entities, so text_html rebuilds the formatting another bot
    (or a person) actually used. Message.text would drop it, which is why the other
    bot's replies were logged plain while ours kept their tags.
    """
    try:
        text = msg.text_html or msg.caption_html or ""
    except (TypeError, AttributeError):  # no text at all, e.g. a bare photo
        text = ""
    if not text:
        text = msg.text or msg.caption or ""
    kind = next((k for k in MEDIA_KINDS if getattr(msg, k, None)), None)
    if kind == "sticker" and msg.sticker.emoji:
        kind = f"sticker {msg.sticker.emoji}"
    elif kind == "poll":
        kind = f"poll: {msg.poll.question}"
    return f"[{kind}] {text}".strip() if kind else text


def save(msg: Message, text: str | None = None) -> None:
    """Log a message. A bot's own replies are stored under its real name, not "You",
    so several bots can share one database and tell each other apart.

    `text` overrides the stored body: Telegram strips formatting from a sent message's
    `.text`, so the bot's own replies would come back unstyled and the model, seeing a
    history of plain replies, stops bolding tickers. The reply loop passes
    `Message.text_html`, which rebuilds the HTML that was actually displayed - the same
    syntax the prompt asks for, and the same syntax the other bot logs.
    """
    db.execute(
        "INSERT OR REPLACE INTO messages VALUES (?, ?, ?, ?, ?, ?)",
        (msg.chat_id, msg.message_id, sender_name(msg), text or describe(msg),
         int(msg.date.timestamp()),
         msg.reply_to_message.message_id if msg.reply_to_message else None),
    )
    db.commit()


def stored_text(chat_id: int, message_id: int) -> str | None:
    """The logged body of a message, which for the bot's own replies keeps the markdown
    Telegram strips from `Message.text`."""
    row = db.execute(
        "SELECT text FROM messages WHERE chat_id = ? AND message_id = ?",
        (chat_id, message_id),
    ).fetchone()
    return row[0] if row else None


def transcript(chat_id: int, own_name: str) -> str:
    """Recent messages, oldest first, as lines for the prompt.

    A plain "last N messages" window drops its oldest line every time a message arrives,
    which changes the start of the prompt and defeats provider prompt caching (it caches
    the unchanged start). Instead the window's start only moves every HISTORY_LIMIT/2
    messages: it holds between HISTORY_LIMIT and 1.5 x HISTORY_LIMIT messages (20-29 by
    default) and in between only grows at the end."""
    total = db.execute("SELECT COUNT(*) FROM messages WHERE chat_id = ?", (chat_id,)).fetchone()[0]
    step = max(1, HISTORY_LIMIT // 2)
    start = max(0, (total - HISTORY_LIMIT) // step * step)
    rows = db.execute(
        "SELECT message_id, sender, text, ts, reply_to FROM messages "
        "WHERE chat_id = ? ORDER BY message_id LIMIT -1 OFFSET ?",
        (chat_id, start),
    ).fetchall()
    lines = []
    for mid, sender, text, ts, reply_to in rows:  # oldest first
        when = datetime.fromtimestamp(ts, TZ).strftime("%a %H:%M")
        reply = f" (replying to #{reply_to})" if reply_to else ""
        who = "You" if sender == own_name else sender
        lines.append(f"[#{mid}] {when} {who}{reply}: {text}")
    return "\n".join(lines)


# ---------------------------------------------------------------- images ----

def photo_file_id(msg: Message) -> str | None:
    if msg.photo:
        return msg.photo[-1].file_id  # last entry is the largest size
    doc = msg.document
    if doc and (doc.mime_type or "") in ("image/jpeg", "image/png"):
        return doc.file_id
    return None


async def image_part(bot, file_id: str) -> dict:
    f = await bot.get_file(file_id)
    data = bytes(await f.download_as_bytearray())
    mime = "image/png" if data[:4] == b"\x89PNG" else "image/jpeg"
    url = f"data:{mime};base64,{base64.b64encode(data).decode()}"
    return {"type": "image_url", "image_url": {"url": url}}


# ------------------------------------------------------------ formatting ----

MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
ITEM_RE = re.compile(r"^\s*[-*\u2022\u29c1]\s+", re.M)
TAG_RE = re.compile(r"&lt;(/?)(b|strong|i|em|u|s|code|pre)&gt;", re.I)
A_OPEN_RE = re.compile(r'&lt;a\s+href=[\"\']?([^\"\'&\s]+)[\"\']?\s*&gt;', re.I)
A_CLOSE_RE = re.compile(r"&lt;/a&gt;", re.I)
TAG_COUNT_RE = re.compile(r"<(/?)(b|i|code|a)\b[^>]*>")


# Words shaped like tickers that aren't tickers.
NOT_TICKERS = {
    "AI", "AM", "PM", "AND", "THE", "NOT", "BUT", "FOR", "ALL", "NEW", "OLD", "OK",
    "US", "USA", "UK", "EU", "AU", "CN", "UTC", "AEST", "AEDT", "GMT",
    "CEO", "CFO", "COO", "CTO", "IPO", "ETF", "ETN", "REIT", "SPAC", "LLC", "INC",
    "GDP", "CPI", "PPI", "FED", "FOMC", "ECB", "RBA", "BOJ", "SEC", "ASIC", "ATO",
    "ASX", "NYSE", "LSE", "TSX", "OTC", "CBOE", "CME",
    "EPS", "PE", "PEG", "DCF", "FCF", "EBIT", "ROE", "ROI", "ROIC", "TAM", "YOY",
    "YTD", "LTM", "TTM", "FY", "HY", "QOQ", "MOM", "EOD", "ATH", "ATL", "MA", "RSI",
    "USD", "AUD", "EUR", "GBP", "JPY", "CNY",
    "NOTE", "EDIT", "TLDR", "FYI", "IMO", "IMHO", "AKA", "ETA", "VS", "PS",
    "VR", "AR", "XR", "API", "GPU", "CPU", "TPU", "HBM", "DRAM", "NAND", "EUV", "OS",
    "U.S", "U.K", "P.A", "E.G", "I.E",
} | EXTRA_NOT_TICKERS
TICKER_RE = re.compile(
    r"\b([A-Z0-9]{1,6}\.[A-Z]{1,2}|[A-Z][A-Z0-9]{1,5})\b")  # 9988.HK, SQX.AX, ACMR
# Single-letter tickers (U, F, X, T) only count next to a move or a price, so "vitamin C"
# and "A big move" are left alone.
ONE_LETTER_RE = re.compile(r"\b([A-Z])\b(?=\s*[-+\u2014:]?\s*(?:[-+]?\d|\$))")
# Period and unit shorthand: FY26, Q3, H1, CY25, 2X, 10K, 200D
NOT_TICKER_RE = re.compile(r"^(?:FY|CY|HY|H|Q|FQ)\d+$|^\d|^[A-Z]\d+$")
SEGMENT_RE = re.compile(r"(<[^>]+>)")


def is_ticker(word: str) -> bool:
    if word in EXTRA_TICKERS:
        return True
    base, _, suffix = word.partition(".")
    if suffix in EXCHANGE_SUFFIXES and base not in NOT_TICKERS:
        return True  # an exchange suffix settles it, even for numeric codes like 9988.HK
    if word in NOT_TICKERS or NOT_TICKER_RE.match(word):
        return False
    return True


# Yahoo needs the exchange suffix in the URL, but it's noise in the chat: link SQX.AX,
# show SQX. Share classes like BRK.B are not suffixes, so they stay as written.
EXCHANGE_SUFFIXES = {"AX", "L", "TO", "V", "NZ", "HK", "SS", "SZ", "T", "KS", "DE",
                     "PA", "AS", "MI", "MC", "ST", "OL", "SI", "BO", "NS", "SA", "MX"}


def yahoo_url(symbol: str) -> str:
    sym = f"{symbol}-USD" if symbol in CRYPTO else symbol
    return "https://finance.yahoo.com/quote/" + quote(sym, safe="")


def ticker_label(symbol: str) -> str:
    """What the reader sees: SQX.AX becomes SQX, BRK.B stays BRK.B."""
    base, _, suffix = symbol.partition(".")
    return base if suffix in EXCHANGE_SUFFIXES else symbol


def mark_tickers(out: str, wrap) -> str:
    """Apply `wrap` to ticker-shaped words, outside tags and outside existing markup.

    The model is asked to format tickers itself, but it imitates its own plain history
    and drifts back, so do it deterministically instead.
    """
    pieces, depth = [], 0
    for seg in SEGMENT_RE.split(out):
        if seg.startswith("<"):
            tag = seg.lower()
            if tag.startswith(("<b", "<a", "<code")) and not tag.startswith("</"):
                depth += 1
            elif tag.startswith(("</b", "</a", "</code")):
                depth = max(0, depth - 1)
            pieces.append(seg)
            continue
        if depth:  # already inside bold, a link or code
            pieces.append(seg)
            continue
        seg = TICKER_RE.sub(
            lambda m: wrap(m.group(1)) if is_ticker(m.group(1)) else m.group(1), seg)
        seg = ONE_LETTER_RE.sub(lambda m: wrap(m.group(1)), seg)
        pieces.append(seg)
    return "".join(pieces)


def bold_tickers(out: str) -> str:
    """Kept for when bolding is wanted instead of linking: AUTO_BOLD=on."""
    return mark_tickers(out, lambda w: f"<b>{w}</b>")


def link_tickers(out: str) -> str:
    return mark_tickers(out, lambda w: f'<a href="{yahoo_url(w)}">{ticker_label(w)}</a>')


def to_html(text: str) -> str:
    """Escape everything, then re-allow the tags Telegram accepts.

    The model is asked for HTML, but anything it writes could contain a stray < or &,
    so the safe order is escape-then-restore rather than trusting its output. Markdown
    is still converted as a fallback, because models drift back to it.
    """
    out = html.escape(text, quote=False)
    out = TAG_RE.sub(r"<\1\2>", out)                 # <b> <i> <code> and friends
    out = A_OPEN_RE.sub(r'<a href="\1">', out)
    out = A_CLOSE_RE.sub("</a>", out)

    # markdown fallbacks
    out = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", out)
    out = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", out)
    out = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<i>\1</i>", out)
    out = MD_LINK_RE.sub(r'<a href="\2">\1</a>', out)

    # an unclosed tag makes Telegram reject the whole message: drop the markup instead
    opens = sum(1 for m in TAG_COUNT_RE.finditer(out) if not m.group(1))
    closes = sum(1 for m in TAG_COUNT_RE.finditer(out) if m.group(1))
    if opens != closes:
        log.warning("Unbalanced tags in reply, sending it unformatted")
        out = TAG_COUNT_RE.sub("", out)

    if AUTO_BOLD:
        out = bold_tickers(out)
    elif AUTO_LINK:
        out = link_tickers(out)
    out = ITEM_RE.sub("\u2022 ", out)          # any list marker becomes a plain bullet
    if SPACE_ITEMS:                            # one line per ticker reads as a wall
        out = re.sub(r"\n(?=\S)", "\n\n", out)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def split_message(text: str, size: int = MAX_TG_MESSAGE) -> list[str]:
    chunks = []
    while len(text) > size:
        cut = text.rfind("\n", 0, size)
        cut = cut if cut > 0 else size
        chunks.append(text[:cut])
        text = text[cut:].lstrip()
    return chunks + [text] if text else chunks


# ------------------------------------------------------------------- MCP ----

# Tools whose names start with these are treated as read-only when the server
# doesn't annotate them. Anything else (create_*, delete_*, update_* ...) is
# left out unless you list it in "allowed_tools", so nobody in the group can
# talk the bot into changing your Sharesight data.
READ_ONLY_PREFIXES = ("get", "list", "search", "fetch", "find", "lookup", "show", "read")


ENV_REF = re.compile(r"\$\{(\w+)\}|\$(\w+)")


def _expand(d: dict | None, label: str = "") -> dict | None:
    """Expand ${VAR} references so secrets can stay in the environment, not the JSON.

    Unset variables become empty strings (os.path.expandvars would leave the
    literal "${VAR}", which a server may accept as a real value and only fail
    on much later), and are logged, so the server fails at startup with its
    own "missing credentials" error."""
    if not d:
        return None
    missing: set[str] = set()

    def sub(m: re.Match) -> str:
        name = m.group(1) or m.group(2)
        if name not in os.environ:
            missing.add(name)
        return os.environ.get(name, "")

    out = {k: ENV_REF.sub(sub, str(v)) for k, v in d.items()}
    if missing:
        log.warning("MCP %s: environment variable(s) not set: %s", label, ", ".join(sorted(missing)))
    return out


# Tool definitions are re-sent on every round of every request, so their size
# adds up fast. Trim what the model doesn't need to pick and call a tool well.
HIDDEN_PARAMS = {"response_format"} # optional params the model shouldn't bother with
DESCRIPTION_SKIP = re.compile(r"^(returns?|example|args|arguments|note|raises)\b", re.IGNORECASE)


def compact_description(text: str) -> str:
    """Keep the paragraphs saying what a tool does and when to use it; drop ones
    about return formats, examples and the like, and squash whitespace."""
    paras = [" ".join(p.split()) for p in re.split(r"\n\s*\n", text) if p.strip()]
    kept = [p for i, p in enumerate(paras) if i == 0 or not DESCRIPTION_SKIP.match(p)]
    return " ".join(kept)[:600]


def compact_schema(schema):
    """Strip a JSON schema's auto-generated titles, hide HIDDEN_PARAMS, and cut
    each parameter description to its first sentence."""
    if isinstance(schema, list):
        return [compact_schema(x) for x in schema]
    if not isinstance(schema, dict):
        return schema
    out = {}
    required = set(schema.get("required", []))
    for key, value in schema.items():
        if key == "title":
            continue
        if key == "properties" and isinstance(value, dict):
            value = {k: v for k, v in value.items() if k not in HIDDEN_PARAMS or k in required}
        if key == "description" and isinstance(value, str):
            value = re.split(r"(?<=[.;])\s", " ".join(value.split()), maxsplit=1)[0][:160]
        out[key] = compact_schema(value)
    if "$defs" in out:  # drop definitions nothing refers to any more
        used = json.dumps({k: v for k, v in out.items() if k != "$defs"})
        out["$defs"] = {k: v for k, v in out["$defs"].items() if f"#/$defs/{k}" in used}
        if not out["$defs"]:
            del out["$defs"]
    return out


def _looks_read_only(tool) -> bool:
    hint = getattr(tool.annotations, "readOnlyHint", None) if tool.annotations else None
    if hint is not None:
        return hint
    return tool.name.lower().startswith(READ_ONLY_PREFIXES)


class MCPServer:
    """One MCP server, kept connected for the life of the bot.

    The connection lives in its own task because the MCP SDK's context
    managers must be entered and exited in the same task."""

    def __init__(self, label: str, cfg: dict):
        self.label = re.sub(r"[^A-Za-z0-9_-]", "_", label)
        self.cfg = cfg
        self.description = cfg.get("description", "")
        # Telegram user IDs allowed to trigger this server's tools; empty = everyone.
        self.allowed_users = {int(u) for u in cfg.get("allowed_users", [])}
        self.session: ClientSession | None = None
        self.error: str | None = None   # why the server is down, if it is
        self.bot = None # for posting alerts to ALERT_CHATS
        self.tools: list[dict] = [] # chat-completions function tool specs
        self.fn_names: dict[str, str] = {}  # function name sent to the model -> MCP tool name
        # Cap simultaneous calls: a 20-stock request fired all at once gets
        # rate-limited by Yahoo.
        self._sem = asyncio.Semaphore(int(cfg.get("max_concurrent", 4)))
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    def permits(self, user_id: int | None) -> bool:
        return not self.allowed_users or user_id in self.allowed_users

    async def start(self, bot) -> None:
        self.bot = bot
        if self._task and not self._task.done():  # already started: don't spawn a second copy
            log.info("MCP %s is already running", self.label)
            await self._ready.wait()
            return
        self._ready.clear()
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name=f"mcp-{self.label}")
        try:
            # Generous: the first npx run downloads the package.
            await asyncio.wait_for(self._ready.wait(), 180)
        except asyncio.TimeoutError:
            log.warning("MCP %s is slow to start; its tools appear once it's up", self.label)

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task

    async def _run(self) -> None:
        # The server's stderr goes to a temp file, so that when it dies we can
        # report what it said (e.g. "Permission denied"), not just "Connection closed".
        errlog = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
        try:
            async with AsyncExitStack() as stack:
                if "url" in self.cfg:  # remote server over streamable HTTP
                    read, write, _ = await stack.enter_async_context(
                        streamablehttp_client(self.cfg["url"], headers=_expand(self.cfg.get("headers"), self.label))
                    )
                else:  # local server over stdio
                    params = StdioServerParameters(
                        command=self.cfg["command"],
                        args=self.cfg.get("args", []),
                        env=_expand(self.cfg.get("env"), self.label),
                    )
                    read, write = await stack.enter_async_context(stdio_client(params, errlog=errlog))
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                await self._load_tools(session)
                self.session = session
                self.error = None
                self._ready.set()
                await self._stop.wait()
        except Exception as e:
            self.error = describe_failure(e, errlog)
            log.error("MCP server %s failed: %s", self.label, self.error, exc_info=True)
            if not self._stop.is_set():
                await self._alert()
        finally:
            self.session = None
            errlog.close()
            self._ready.set()

    async def _alert(self) -> None:
        if not self.bot:
            return
        text = (
            f"⚠️ The <b>{html.escape(self.label)}</b> data source is down. "
            f"I can't use it until the bot is restarted.\n"
            f"<pre>{html.escape(self.error or 'unknown error')}</pre>"
        )
        for chat_id in ALERT_CHATS:
            try:
                sent = await self.bot.send_message(
                    chat_id, text, parse_mode=ParseMode.HTML, disable_notification=True
                )
                save(sent)  # so it shows up in the transcript the model sees
            except Exception:
                log.exception("Couldn't send MCP alert to chat %s", chat_id)

    async def _load_tools(self, session: ClientSession) -> None:
        listed = (await session.list_tools()).tools
        allow = set(self.cfg.get("allowed_tools", []))
        blocked = set(self.cfg.get("blocked_tools", []))
        for t in listed:
            if t.name in blocked or not (t.name in allow if allow else _looks_read_only(t)):
                continue
            fn = f"{self.label}__{t.name}"[:64]
            self.fn_names[fn] = t.name
            self.tools.append({
                "type": "function",
                "function": {
                    "name": fn,
                    "description": compact_description(t.description or ""),
                    "parameters": compact_schema(t.inputSchema or {"type": "object", "properties": {}}),
                },
            })
        enabled = set(self.fn_names.values())
        log.info("MCP %s: enabled %s", self.label, sorted(enabled))
        skipped = sorted(t.name for t in listed if t.name not in enabled)
        if skipped:
            log.info("MCP %s: skipped (not read-only or not allowed) %s", self.label, skipped)

    async def call(self, tool: str, args: dict) -> str:
        if not self.session:
            return f"Error: the {self.label} data source isn't connected right now."
        holdings_only = self.cfg.get("current_holdings_only")
        if holdings_only and tool == "get_performance_report":
            args["include_sales"] = False  # Sharesight's own switch for sold holdings
        async with self._sem:
            result = await asyncio.wait_for(self.session.call_tool(tool, args), MCP_TIMEOUT)
        parts = [c.text if c.type == "text" else f"[{c.type} content omitted]" for c in result.content]
        if not parts and getattr(result, "structuredContent", None):
            parts.append(json.dumps(result.structuredContent))
        out = "\n".join(parts) or "(empty result)"
        if result.isError:
            out = "Tool error: " + out
        else:
            out = tidy_json(out, drop_closed=bool(holdings_only))
        if len(out) > MAX_TOOL_OUTPUT:
            log.warning("MCP %s.%s result truncated (%d chars)", self.label, tool, len(out))
            out = out[:MAX_TOOL_OUTPUT] + f"\n...[truncated; {len(out)} chars total]"
        return out


def _leaf_errors(e: BaseException) -> list[BaseException]:
    """The real errors inside the nested ExceptionGroups the MCP SDK raises."""
    if isinstance(e, BaseExceptionGroup):
        return [leaf for sub in e.exceptions for leaf in _leaf_errors(sub)]
    return [e]


def describe_failure(e: BaseException, errlog) -> str:
    parts = [f"{type(x).__name__}: {x}" for x in _leaf_errors(e)]
    try:
        errlog.flush()
        errlog.seek(0)
        tail = errlog.read()[-800:].strip()
        if tail:
            parts.append(tail)
    except Exception:
        pass
    return "\n".join(parts)[:1500]


def _is_open(h) -> bool:
    """False for a Sharesight holding that's sold down to nothing or delisted."""
    if not isinstance(h, dict):
        return True
    if h.get("valid_position") is False:
        return False
    inst = h.get("instrument")
    if isinstance(inst, dict) and inst.get("expired"):
        return False
    q = h.get("quantity")
    return not (isinstance(q, (int, float)) and q == 0)


# Sharesight holdings are huge (each repeats the whole portfolio, currency
# objects, logos...). Keep only what's useful, flattened, so a big portfolio
# fits in one tool result.
HOLDING_KEEP = (
    "id", "quantity", "value", "instrument_price", "average_purchase_price",
    "capital_gain", "capital_gain_percent", "payout_gain", "payout_gain_percent",
    "currency_gain", "total_gain", "total_gain_percent", "inception_date",
    "group_name", "cost_base", "values_over_time",
)


# Trailing legal suffixes / share-class tags: "Arm Holdings plc. - ADR" -> "Arm Holdings"
# Sharesight share-class tags: "Crowdstrike Holdings Inc - Ordinary Shares - Class A"
SHARE_CLASS = re.compile(
    r"(?:\s+-\s+(?:ordinary shares|class [a-z]|common stock|adr|ads|depositary receipts?))+\s*$",
    re.IGNORECASE,
)
LEGAL_SUFFIX = re.compile(
    r"(?:[\s,.\-]+(?:limited|ltd|incorporated|inc|corporation|corp|co|plc|sponsored adr|adr|ads)\.?)+\s*$",
    re.IGNORECASE,
)


TRAILING_HOLDINGS = re.compile(r"\s+holdings?$", re.IGNORECASE)
TRAILING_TECH = re.compile(r"\s+technolog(?:y|ies)$", re.IGNORECASE)


def clean_name(name, code: str | None = None):
    if not isinstance(name, str):
        return name
    name = SHARE_CLASS.sub("", name).strip() or name
    name = LEGAL_SUFFIX.sub("", name).strip() or name
    name = TRAILING_HOLDINGS.sub("", name) or name
    short = TRAILING_TECH.sub("", name)
    if short != name:
        # "Micron Technology" -> "Micron", but "DUG Technology" -> "DUG Tech":
        # keep "Tech" when what's left is just the ticker or a tiny word.
        bare = not short or len(short) <= 3 or (code and short.lower() == code.lower())
        name = f"{short} Tech" if bare else short
    return name


def _slim(h):
    if not isinstance(h, dict):
        return h
    inst = h.get("instrument") if isinstance(h.get("instrument"), dict) else {}
    out = {
        "code": inst.get("code") or h.get("symbol"),
        "market": inst.get("market_code"),
        "name": clean_name(inst.get("name"), inst.get("code") or h.get("symbol")),
        "currency": inst.get("currency_code"),
        "type": inst.get("friendly_instrument_description"),
        "sector": inst.get("sector_classification_name"),
    }
    out.update({k: h.get(k) for k in HOLDING_KEEP})
    if out.get("type") == "Ordinary Shares":    # the default; only say when it's something else
        del out["type"]
    if out.get("group_name") == "All Holdings": # ungrouped report
        del out["group_name"]
    return {k: v for k, v in out.items() if v not in (None, [], {})}


def _as_table(rows: list) -> dict | list:
    """A list of same-shaped records as {"columns": [...], "rows": [[...]]}, so
    each field name appears once instead of once per holding."""
    if not rows or not all(isinstance(r, dict) for r in rows):
        return rows
    columns = list(dict.fromkeys(k for r in rows for k in r))
    return {"columns": columns, "rows": [[r.get(c) for c in columns] for r in rows]}


def table_records(x) -> list[dict]:
    """Undo _as_table (also accepts a plain list of records)."""
    if isinstance(x, dict) and "columns" in x:
        return [dict(zip(x["columns"], row)) for row in x.get("rows", [])]
    return x if isinstance(x, list) else []


REPORT_DROP = ("id", "portfolio_tz_name", "include_sales")


PORTFOLIO_KEEP = ("id", "name", "consolidated", "currency_code", "country_code",
                  "inception_date", "owner_name")


def _slim_portfolio(p):
    return {k: p[k] for k in PORTFOLIO_KEEP if k in p} if isinstance(p, dict) else p


def tidy_json(text: str, drop_closed: bool = False) -> str:
    """Compact a JSON tool result (pretty-printing wastes a lot of the size budget)
    and, for Sharesight, remove closed / zero-unit holdings."""
    try:
        data = json.loads(text)
    except ValueError:
        return text
    if isinstance(data, dict):
        # Holding lists are top-level, except in get_performance_report's {"report": {...}}.
        for parent in (data, data.get("report")):
            if not isinstance(parent, dict):
                continue
            for key in ("holdings", "combined_holdings"):
                items = parent.get(key)
                if isinstance(items, list):
                    parent[key] = _as_table([_slim(h) for h in items if not drop_closed or _is_open(h)])
        if isinstance(data.get("portfolios"), list):
            data["portfolios"] = [_slim_portfolio(p) for p in data["portfolios"]]
        if isinstance(data.get("portfolio"), dict):
            data["portfolio"] = _slim_portfolio(data["portfolio"])
        for key in ("api_transaction", "links"):    # API housekeeping
            data.pop(key, None)
        report = data.get("report")
        if isinstance(report, dict):
            if isinstance(report.get("currency"), dict):
                report["currency"] = report["currency"].get("code")
            for key in REPORT_DROP:
                report.pop(key, None)
            if report.get("grouping") == "ungrouped":
                report.pop("grouping")
                report.pop("sub_totals", None)  # one group, same as the report totals
            if isinstance(report.get("cash_accounts"), list):
                report["cash_accounts"] = [
                    {
                        "name": c.get("name"),
                        "value": c.get("value"),
                        "currency": (c.get("currency") or {}).get("code"),
                    }
                    for c in report["cash_accounts"] if isinstance(c, dict) and c.get("value")
                ]
        one = data.get("holding")
        if isinstance(one, dict):
            if drop_closed and not _is_open(one):
                data = {"note": "This holding is closed (sold, or zero units). Treat it as not held."}
            else:
                data["holding"] = _slim(one)
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False)


def load_mcp_servers() -> dict[str, MCPServer]:
    if not os.path.exists(MCP_CONFIG):
        return {}
    with open(MCP_CONFIG) as f:
        cfg = json.load(f)
    cfg = cfg.get("mcpServers", cfg)  # accept Claude Desktop-style files too
    servers = [MCPServer(label, c) for label, c in cfg.items() if not c.get("disabled")]
    return {s.label: s for s in servers}


MCP_SERVERS = load_mcp_servers()


def resolve_tool(fn_name: str, user_id: int | None) -> tuple[MCPServer, str] | None:
    for s in MCP_SERVERS.values():
        tool = s.fn_names.get(fn_name)
        if tool:
            return (s, tool) if s.permits(user_id) else None
    return None


# Tool arguments not worth logging: flags and report options.
NOISY_ARGS = {"consolidated", "report_combined", "grouping", "include_limited",
              "include_sales", "response_format"}


def brief_args(args: dict) -> str:
    """Tool arguments for the log, short: "portfolio_id=684141 2026-09-04..2026-10-04"."""
    parts = []
    for k, v in args.items():
        if k in NOISY_ARGS or k in ("start_date", "end_date") or v is None or isinstance(v, bool):
            continue
        if isinstance(v, float) and v.is_integer():
            v = int(v)
        elif isinstance(v, list):
            v = ",".join(str(x) for x in v)
        parts.append(f"{k}={v}")
    if args.get("start_date") or args.get("end_date"):
        parts.append(f"{args.get('start_date', '')}..{args.get('end_date', '')}")
    return " ".join(parts)


async def run_tool(call, user_id: int | None) -> str:
    target = resolve_tool(call.name, user_id)
    if not target:
        return f"Error: tool {call.name} isn't available for this user."
    server, tool = target
    try:
        args = json.loads(call.arguments or "{}")
        log.info("MCP %s.%s %s", server.label, tool, brief_args(args))
        return await server.call(tool, args)
    except asyncio.TimeoutError:
        return f"Error: {tool} timed out after {MCP_TIMEOUT}s."
    except Exception as e:
        log.exception("MCP call %s.%s failed", server.label, tool)
        return f"Error: {type(e).__name__}: {e}"


def tools_prompt(servers: list["MCPServer"], down: list["MCPServer"]) -> str:
    """Extra instructions describing the MCP data sources for this request,
    including ones that are down, so the model reports the error instead of
    claiming it has no access."""
    text = ""
    if servers:
        lines = "\n".join(f"- {s.label}: {s.description}" for s in servers)
        text += (
            "\nYou also have data tools. Their function names start with the source name:\n"
            f"{lines}\n"
            "You can call many tools at once in a single step; looking up 20 or 30 stocks is "
            "normal work, not too many. "
            "Prefer these over web search for quotes, price history, fundamentals and portfolio "
            "questions, and don't guess a number you could look up. Summarise what the tools "
            "return; never paste raw tool output into the chat. If a tool returns an error, "
            "say which tool failed and quote the error briefly.\n"
        )
    if down:
        lines = "\n".join(f"- {s.label} ({s.description})\n  Error: {s.error}" for s in down)
        text += (
            "\nThese data sources are configured but currently DOWN:\n"
            f"{lines}\n"
            "If a question needs one of them, say it's down and quote the error briefly. "
            "Don't claim you have no access to it.\n"
        )
    return text


# -------------------------------------------------------------- llm calls ---

# MiMo sometimes prints a tool call as plain text instead of calling the tool. The
# queries are usually fine, so pull them out and run them rather than binning them.
FAKE_CALL_RE = re.compile(
    r"<parameter=query>(.*?)(?:</parameter>|</tool_call>|<|$)", re.DOTALL | re.IGNORECASE)
TOOL_SYNTAX_RE = re.compile(
    r"<tool_call>.*?(?:</tool_call>|$)|<function=.*?(?:</function>|$)|<\|?tool_calls?\|?>",
    re.DOTALL | re.IGNORECASE)


async def run_query(query: str) -> str:
    """Run one search through a cheap ':online' model and return what it found."""
    log.info("Search: %s", query)
    resp = await llm.chat.completions.create(
        model=SEARCH_MODEL,
        max_tokens=SEARCH_TOKENS,
        temperature=0.2,
        messages=[
            {"role": "system", "content": (
                "Answer from web search results only, as terse bullet points: the figure "
                "or fact, then source and date in brackets. No sentences, no preamble, no "
                "advice, no repetition. If the results don't cover it, reply exactly: "
                "NOT FOUND."
            )},
            {"role": "user", "content": query},
        ],
    )
    return (resp.choices[0].message.content or "").strip() or "No results found."


def call_query(call) -> str:
    """Pull the search string out of a tool call, whatever the model named the field."""
    try:
        args = json.loads(call.function.arguments or "{}")
    except ValueError:
        return ""
    for key in ("query", "q", "search_query", "keywords", "input"):
        if args.get(key):
            return str(args[key])
    return ""


def rough_tokens(messages: list[dict]) -> int:
    """Crude size of what we sent, to compare against the reported input tokens."""
    total = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            total += len(c)
        elif isinstance(c, list):
            total += sum(len(p.get("text", "")) for p in c if isinstance(p, dict))
    return total // 4


def merge_reasoning(details: list[dict], new: list) -> None:
    """Append streamed reasoning_details, joining consecutive text fragments of one block."""
    for d in new:
        d = dict(d)
        last = details[-1] if details else None
        if (last and d.get("text") and last.get("text") is not None
                and last.get("type") == d.get("type") and last.get("index") == d.get("index")):
            last["text"] += d["text"]
            if d.get("signature"):
                last["signature"] = d["signature"]
        else:
            details.append(d)


async def complete(messages: list[dict], tools: list[dict], on_text=None, tool_choice: str | None = None):
    """One chat-completions call. With on_text, stream it, calling on_text(text so far)
    as text arrives. Returns text, any tool calls the model handed back, the assistant
    message to replay into the next round, and the finish reason, usage and citations."""
    kwargs = dict(
        model=MODEL,
        max_tokens=MAX_TOKENS,  # without this, OpenRouter reserves credit for the model's max
        temperature=TEMPERATURE,
        messages=messages,
        extra_body={
            **({"reasoning": {"effort": REASONING}} if REASONING else {}),
            "usage": {"include": True},  # ask OpenRouter for the real cost
        },
    )
    if tools:
        kwargs["tools"] = tools
        if tool_choice:
            kwargs["tool_choice"] = tool_choice
    if on_text is None:
        resp = await llm.chat.completions.create(**kwargs)
        # OpenRouter reports provider failures inside a normal HTTP 200 body.
        err = (getattr(resp, "model_extra", None) or {}).get("error")
        if err:
            raise RuntimeError(f"provider error {err.get('code', '')}: {err.get('message', err)}")
        if not resp.choices:
            log.warning("Bad payload: %s", resp.model_dump_json()[:800])
            raise RuntimeError("no choices in response")
        choice = resp.choices[0]
        extra = getattr(choice.message, "model_extra", None) or {}
        return SimpleNamespace(
            text=(choice.message.content or "").strip(),
            calls=[SimpleNamespace(id=c.id, name=c.function.name, arguments=c.function.arguments)
                   for c in choice.message.tool_calls or []],
            assistant=choice.message.model_dump(exclude_none=True),
            finish=choice.finish_reason, usage=resp.usage,
            cites=extra.get("annotations") or extra.get("citations") or [],
        )

    stream = await llm.chat.completions.create(
        stream=True, stream_options={"include_usage": True}, **kwargs)
    text, finish, usage, cites = "", None, None, []
    calls: dict[int, dict] = {}
    details: list[dict] = []
    async for chunk in stream:
        err = (getattr(chunk, "model_extra", None) or {}).get("error")
        if err:
            raise RuntimeError(f"provider error {err.get('code', '')}: {err.get('message', err)}")
        if chunk.usage:
            usage = chunk.usage
        if not chunk.choices:
            continue
        choice = chunk.choices[0]
        delta = choice.delta
        if delta.content:
            text += delta.content
            on_text(text)
        for c in delta.tool_calls or []:
            slot = calls.setdefault(c.index, {"id": "", "name": "", "arguments": ""})
            slot["id"] = c.id or slot["id"]
            if c.function:
                slot["name"] += c.function.name or ""
                slot["arguments"] += c.function.arguments or ""
        extra = getattr(delta, "model_extra", None) or {}
        merge_reasoning(details, extra.get("reasoning_details") or [])
        cites += extra.get("annotations") or []
        finish = choice.finish_reason or finish
    done = [SimpleNamespace(**calls[i]) for i in sorted(calls)]
    assistant = {"role": "assistant", "content": text or None}
    if done:
        assistant["tool_calls"] = [
            {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
            for c in done
        ]
    if details:
        assistant["reasoning_details"] = details
    return SimpleNamespace(text=text.strip(), calls=done, assistant=assistant,
                           finish=finish, usage=usage, cites=cites)


def active_servers(user_id: int | None) -> tuple[list[MCPServer], list[MCPServer]]:
    """MCP servers this user may use that are up, and ones that are configured but down."""
    up = [s for s in MCP_SERVERS.values() if s.session and s.permits(user_id)]
    down = [s for s in MCP_SERVERS.values() if s.error and s.permits(user_id)]
    return up, down


async def ask_model(messages: list[dict], user_id: int | None = None, on_text=None,
                    tools_on: bool = True) -> str:
    """Ask the model, running the tool calls it makes (MCP data tools here, searches via
    SEARCH_MODEL) for up to MAX_TOOL_ROUNDS rounds, until it produces an answer.
    With on_text, replies are streamed and on_text gets the text so far."""
    t0 = time.monotonic()
    search_on = tools_on and SEARCH
    servers = active_servers(user_id)[0] if tools_on else []
    conv = list(messages)
    rounds = tool_calls = 0
    tok = {"in": 0, "out": 0, "cost": 0.0}
    r = None
    while True:
        tools = (SEARCH_TOOL if search_on else []) + [t for s in servers for t in s.tools]
        final = rounds >= MAX_TOOL_ROUNDS
        r = await complete(conv, tools, on_text, tool_choice="none" if final else None)
        rounds += 1
        u = r.usage
        tok["in"] += getattr(u, "prompt_tokens", 0) or 0
        tok["out"] += getattr(u, "completion_tokens", 0) or 0
        tok["cost"] += getattr(u, "cost", 0) or 0
        # in >> sent means search results were injected; in ~= sent means nothing was searched.
        log.info("Round %d: tokens sent~%s in=%s out=%s cost=%s cites=%s finish=%s",
                 rounds, rough_tokens(conv), getattr(u, "prompt_tokens", "?"),
                 getattr(u, "completion_tokens", "?"), getattr(u, "cost", "?"),
                 len(r.cites), r.finish)
        if final:
            log.warning("Hit MAX_TOOL_ROUNDS (%d); made the model answer with what it has",
                        MAX_TOOL_ROUNDS)
            break
        if r.calls:
            tool_calls += len(r.calls)
            outputs = await asyncio.gather(*(run_call(c, user_id) for c in
                                             _cap_searches(r.calls)))
            conv.append(r.assistant)
            conv += [{"role": "tool", "tool_call_id": c.id, "content": out}
                     for c, out in zip(r.calls, outputs)]
            continue
        # MiMo sometimes prints a search call as text instead of making it: run the query
        # and hand the results back, once.
        fake = [q.strip() for q in FAKE_CALL_RE.findall(r.text) if q.strip()]
        if fake and search_on and SEARCH_MODEL:
            results = await asyncio.gather(*(run_query(q) for q in fake[:5]),
                                           return_exceptions=True)
            found = "\n\n".join(f'Results for "{q}":\n{x}' for q, x in zip(fake, results)
                                if not isinstance(x, Exception))
            conv.append({"role": "user", "content":
                         f"{found}\n\nAnswer the question now using these results."})
            search_on = False
            continue
        break
    log.info("%.1fs: %d rounds, %d tool calls | in %d out %d cost %.4f", time.monotonic() - t0,
             rounds, tool_calls, tok["in"], tok["out"], tok["cost"])
    text = TOOL_SYNTAX_RE.sub("", r.text).strip()
    if not text:
        if r.finish == "length":
            raise RuntimeError(f"hit the {MAX_TOKENS}-token cap before writing an answer")
        raise RuntimeError(f"empty reply (finish_reason={r.finish})")
    return text


def is_mcp_tool(name: str) -> bool:
    return any(name in s.fn_names for s in MCP_SERVERS.values())


MAX_SEARCHES = 5  # search calls run per round; the rest are told to try later


def _cap_searches(calls: list) -> list:
    """Mark search calls beyond MAX_SEARCHES so run_call declines them (every call
    still needs a tool reply)."""
    seen = 0
    for c in calls:
        if is_mcp_tool(c.name):
            continue
        seen += 1
        c.skip = seen > MAX_SEARCHES
    return calls


async def run_call(call, user_id: int | None) -> str:
    """Run one tool call the model handed back: an MCP data tool, or else a web search."""
    if is_mcp_tool(call.name):
        return await run_tool(call, user_id)
    if getattr(call, "skip", False):
        return "Skipped: too many searches at once. Use what you already have."
    query = call_query(SimpleNamespace(function=call))
    if not query or not SEARCH_MODEL:
        return "Search isn't available."
    try:
        return await run_query(query)
    except Exception as e:
        return f"Search failed: {e}"


# -------------------------------------------------------------- handlers ----

async def keep_typing(bot, chat_id: int) -> None:
    """Telegram's typing indicator lasts ~5s, so resend it until cancelled."""
    while True:
        await bot.send_chat_action(chat_id, ChatAction.TYPING)
        await asyncio.sleep(4)


async def start_typing(bot, chat_id: int) -> asyncio.Task:
    """Send the typing indicator right now, then keep it going in the background.
    (A task alone would only send its first one at the next await, which can be
    after the prompt has been built.)"""
    try:
        await bot.send_chat_action(chat_id, ChatAction.TYPING)
    except Exception as e:
        log.warning("Typing indicator failed: %s", e)

    async def resend() -> None:
        await asyncio.sleep(4)
        await keep_typing(bot, chat_id)

    return asyncio.create_task(resend())


class Draft:
    """Streams a reply into a Telegram message draft (private chats only).

    Starts with an empty draft, which Telegram shows as "Thinking...", then shows the
    model's text as it arrives. Drafts vanish after 30 s without an update, so it's
    re-sent at least every KEEPALIVE seconds, e.g. while tools run. The finished reply
    is sent as a normal message."""

    INTERVAL = 1.0  # seconds between updates while text is arriving
    KEEPALIVE = 20  # re-send before Telegram's 30 s draft timeout

    def __init__(self, bot, chat_id: int):
        self.bot = bot
        self.chat_id = chat_id
        self.draft_id = random.randint(1, 2**31 - 1)
        self.text = ""  # the model's latest raw text (possibly half-written HTML)
        self._shown: str | None = None
        self._last = 0.0
        self._task: asyncio.Task | None = None
        self._typing: asyncio.Task | None = None

    async def start(self) -> None:
        # Typing indicator first (until text starts arriving), then the draft.
        self._typing = await start_typing(self.bot, self.chat_id)
        await self._send("")  # "Thinking..."
        self._task = asyncio.create_task(self._loop())

    def update(self, text: str) -> None:
        self.text = text

    def stop(self) -> None:
        for task in (self._task, self._typing):
            if task:
                task.cancel()

    def _render(self) -> str:
        # Half-written HTML would be rejected, so drafts are plain text; the final
        # message gets the real formatting.
        plain = html.unescape(re.sub(r"<[^>]*>?", "", TOOL_SYNTAX_RE.sub("", self.text))).strip()
        if len(plain) > MAX_TG_MESSAGE:
            plain = "..." + plain[-(MAX_TG_MESSAGE - 3):]
        return plain

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.INTERVAL)
            text = self._render()
            if text and self._typing:
                self._typing.cancel()
                self._typing = None
            if text != self._shown or time.monotonic() - self._last >= self.KEEPALIVE:
                await self._send(text)

    async def _send(self, text: str) -> None:
        try:
            if hasattr(self.bot, "send_message_draft"):
                await self.bot.send_message_draft(self.chat_id, self.draft_id, text)
            else:  # python-telegram-bot versions from before drafts existed
                await self.bot.do_api_request(
                    "sendMessageDraft",
                    api_kwargs={"chat_id": self.chat_id, "draft_id": self.draft_id, "text": text},
                )
        except Exception as e:
            log.warning("sendMessageDraft failed: %s", e)
        self._shown, self._last = text, time.monotonic()


async def send_reply(msg: Message, text: str, quote: bool = True) -> None:
    for chunk in split_message(text):
        body = to_html(chunk)
        try:
            sent = await msg.reply_text(
                body, parse_mode=ParseMode.HTML, do_quote=quote,
                disable_notification=QUIET,
                link_preview_options=LinkPreviewOptions(is_disabled=not LINK_PREVIEW))
        except BadRequest:  # malformed tags: unformatted beats no reply at all
            log.warning("HTML parse failed, sending plain")
            body = chunk
            sent = await msg.reply_text(chunk, do_quote=quote, disable_notification=QUIET)
        # Log the HTML that was sent. Telegram strips formatting from Message.text, and
        # Message.text_html only rebuilds it when the response carries entities - which it
        # doesn't always - so prefer our own copy and fall back to the rebuilt one.
        logged = sent.text_html or ""
        save(sent, text=body if "<" in body else (logged or body))


def build_messages(bot, msg: Message, reply_target: Message | None,
                   replied_to_bot: bool, content: list[dict], own_name: str,
                   tools_on: bool = True) -> list[dict]:
    private = msg.chat.type == "private"
    history = (f"Recent history of this private one-to-one chat with {sender_name(msg)}, "
               "not the group:" if private else "Recent group chat history:")
    history += f"\n{transcript(msg.chat_id, own_name)}"
    if reply_target:
        who = "You" if replied_to_bot else sender_name(reply_target)
        # Prefer the logged copy: it keeps the formatting Telegram strips from sent
        # messages, and this quote is the example the model is likeliest to copy.
        body = stored_text(msg.chat_id, reply_target.message_id) or describe(reply_target)
        history += (f"\n\nThe incoming question is a reply to this message:\n"
                    f"[#{reply_target.message_id}] {who}: {body}")
    history += f"\n\nThe question below was asked by {sender_name(msg)}."

    user_id = msg.from_user.id if msg.from_user else None
    servers, down = active_servers(user_id) if tools_on else ([], [])
    if tools_on and (SEARCH or servers or down):
        note = (SEARCH_NOTE if SEARCH else "") + tools_prompt(servers, down)
    else:
        note = NO_SEARCH_NOTE
    system = SYSTEM_PROMPT.format(
        bot_name=bot.first_name,
        now=datetime.now(TZ).strftime("%A %d %B %Y, %H:%M %Z"),
        search_note=note,
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": history},
        {"role": "assistant", "content": "Understood, I've read the chat history."},
        {"role": "user", "content": content},  # the question, kept separate and last
    ]


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None:
        return

    save(msg)  # log everything, including edits (they overwrite the original)
    if update.edited_message:
        return  # don't answer edited messages
    if msg.from_user and msg.from_user.is_bot:
        return  # log other bots as context, but never answer them (loop guard)

    bot = context.bot
    text = msg.text or msg.caption or ""
    private = msg.chat.type == "private"
    reply_target = msg.reply_to_message
    replied_to_bot = bool(reply_target and reply_target.from_user
                          and reply_target.from_user.id == bot.id)
    if not (private or f"@{bot.username}".lower() in text.lower() or replied_to_bot):
        return

    log.info("Triggered in chat %s (%s) by %s", msg.chat_id, msg.chat.title, sender_name(msg))
    # Show we're working before any work: downloading images and reading the history
    # happen before the model call, and the chat sees nothing until this starts.
    # Private chats stream the reply into a draft (plus typing); groups get the typing
    # indicator. The first indicator is sent directly rather than via a task, which
    # wouldn't run until the next await point.
    draft = Draft(bot, msg.chat_id) if private else None
    if draft:
        await draft.start()
    else:
        await bot.send_chat_action(msg.chat_id, ChatAction.TYPING)
        typing = asyncio.create_task(keep_typing(bot, msg.chat_id))
    try:
        answer = await answer_question(bot, msg, text, reply_target, replied_to_bot,
                                       on_text=draft.update if draft else None)
    finally:
        if draft:
            draft.stop()
        else:
            typing.cancel()

    await send_reply(msg, answer, quote=not private)


async def answer_question(bot, msg: Message, text: str, reply_target: Message | None,
                          replied_to_bot: bool, on_text=None) -> str:
    content: list[dict] = [{"type": "text", "text": text}]
    for candidate in (reply_target, msg):  # replied-to photo first, then the mention
        if candidate and len(content) <= MAX_IMAGES:
            fid = photo_file_id(candidate)
            if fid:
                try:
                    content.append(await image_part(bot, fid))
                except Exception:
                    log.exception("Couldn't download image")
    if len(content) > 1:
        log.info("Attached %d image(s)", len(content) - 1)

    own_name = bot.first_name + (f" (@{bot.username})" if bot.username else "")
    messages = build_messages(bot, msg, reply_target, replied_to_bot, content, own_name)
    user_id = msg.from_user.id if msg.from_user else None

    try:
        try:
            return await ask_model(messages, user_id, on_text)
        except (APIStatusError, RuntimeError):
            if not (SEARCH or MCP_SERVERS):
                raise
            log.warning("Retrying without tools")
            return await ask_model(messages + [{"role": "user", "content": NO_SEARCH_NOTE}],
                                   user_id, on_text, tools_on=False)
    except APIStatusError as e:
        log.exception("LLM request failed")
        try:
            detail = e.response.json()["error"]["message"]
        except Exception:
            detail = str(e)
        return f"\u26a0\ufe0f Model error {e.status_code}: {detail[:500]}"
    except Exception as e:
        log.exception("LLM request failed")
        return f"\u26a0\ufe0f Couldn't get a reply: {type(e).__name__}: {e}"


async def credits(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/credits - owner only: how much OpenRouter credit is left."""
    if not update.effective_user or update.effective_user.id != OWNER_ID:
        return
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(f"{OPENROUTER_BASE}/credits",
                                 headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"})
            r.raise_for_status()
        d = r.json()["data"]
        total, used = float(d["total_credits"]), float(d["total_usage"])
        text = f"OpenRouter: ${total - used:.2f} left (${used:.2f} used of ${total:.2f})"
    except Exception as e:
        log.exception("Credit check failed")
        text = f"Couldn't fetch credits: {e}"
    await update.effective_message.reply_text(text, disable_notification=QUIET)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, NetworkError):
        log.warning("Telegram network hiccup (retrying automatically): %s", context.error)
        return
    log.error("Unhandled error", exc_info=context.error)


async def post_init(app: Application) -> None:
    if MCP_SERVERS:
        await asyncio.gather(*(s.start(app.bot) for s in MCP_SERVERS.values()))


async def post_shutdown(app: Application) -> None:
    await asyncio.gather(*(s.stop() for s in MCP_SERVERS.values()), return_exceptions=True)


def main() -> None:
    if MODEL.endswith(":online"):
        log.warning('MODEL ends with ":online" - that plugin AND the search tool both run, '
                    "which doubles search cost and latency. Drop the suffix.")
    app = (Application.builder().token(TELEGRAM_TOKEN).concurrent_updates(True)
           .post_init(post_init).post_shutdown(post_shutdown).build())
    app.add_handler(CommandHandler("credits", credits))  # must come before the catch-all
    app.add_handler(
        MessageHandler(
            (filters.ChatType.GROUPS | filters.ChatType.PRIVATE)
            & (filters.UpdateType.MESSAGE | filters.UpdateType.EDITED_MESSAGE)
            & ~filters.StatusUpdate.ALL,
            on_message,
        )
    )
    app.add_error_handler(on_error)
    log.info("Starting %s (search: %s, reasoning: %s, MCP servers: %s)", MODEL,
             SEARCH_ENGINE if SEARCH else "off", REASONING or "default",
             ", ".join(MCP_SERVERS) or "none")
    app.run_polling(allowed_updates=["message", "edited_message"])


if __name__ == "__main__":
    main()
