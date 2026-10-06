#!/usr/bin/env python3
"""
Telegram group bot that answers @mentions with an LLM via OpenRouter, using the
group's chat history as context.

Every message the bot sees is logged to SQLite. When someone @mentions the bot, or
replies to one of its messages, the recent log goes to the model as a transcript, so
it can answer things like "what's with that last message?". Photos in the mention, or
in the message being replied to, are sent along too.

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
  HISTORY_LIMIT        messages of context, default 80
  MAX_IMAGES           images per request, default 2 (0 disables vision)
  ALLOWED_CHAT_IDS     comma- or space-separated group IDs; empty = all chats
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
import re
import sqlite3
import time
from datetime import datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
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
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "80"))
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
# If set, every other chat is ignored entirely: no logging, no replies, no API calls.
ALLOWED_CHATS = {
    int(x) for x in os.getenv("ALLOWED_CHAT_IDS", "").replace(",", " ").split()
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
    rows = db.execute(
        "SELECT message_id, sender, text, ts, reply_to FROM messages "
        "WHERE chat_id = ? ORDER BY message_id DESC LIMIT ?",
        (chat_id, HISTORY_LIMIT),
    ).fetchall()
    lines = []
    for mid, sender, text, ts, reply_to in reversed(rows):  # oldest first
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


async def ask_model(messages: list[dict], search: bool = SEARCH, retried: bool = False) -> str:
    t0 = time.monotonic()
    sent = rough_tokens(messages)
    resp = await llm.chat.completions.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,  # without this, OpenRouter reserves credit for the model's max
        temperature=TEMPERATURE,
        messages=messages,
        **({"tools": SEARCH_TOOL} if search else {}),
        extra_body={
            **({"reasoning": {"effort": REASONING}} if REASONING else {}),
            "usage": {"include": True},  # ask OpenRouter for the real cost
        },
    )
    # OpenRouter reports provider failures inside a normal HTTP 200 body.
    err = (getattr(resp, "model_extra", None) or {}).get("error")
    if err:
        raise RuntimeError(f"provider error {err.get('code', '')}: {err.get('message', err)}")
    if not resp.choices:
        log.warning("Bad payload: %s", resp.model_dump_json()[:800])
        raise RuntimeError("no choices in response")

    choice, u = resp.choices[0], resp.usage
    extra = getattr(choice.message, "model_extra", None) or {}
    cites = extra.get("annotations") or extra.get("citations") or []
    # in >> sent means search results were injected; in ~= sent means nothing was searched.
    log.info("%.1fs tokens sent~%s in=%s out=%s cost=%s cites=%s finish=%s",
             time.monotonic() - t0, sent,
             getattr(u, "prompt_tokens", "?"), getattr(u, "completion_tokens", "?"),
             getattr(u, "cost", "?"), len(cites), choice.finish_reason)

    text = (choice.message.content or "").strip()

    # Searches to run here: either a real tool call handed back by OpenRouter, or the
    # model printing one as text. Either way, run them once and let it answer.
    calls = choice.message.tool_calls or []
    queries = [call_query(c) for c in calls] or [
        q.strip() for q in FAKE_CALL_RE.findall(text) if q.strip()]
    if queries and not retried and SEARCH_MODEL:
        results = await asyncio.gather(*(run_query(q) for q in queries[:5]),
                                       return_exceptions=True)
        followup = list(messages)
        if calls:  # a real call needs the assistant turn and a tool reply per call
            followup.append(choice.message.model_dump(exclude_none=True))
            followup += [
                {"role": "tool", "tool_call_id": c.id,
                 "content": f"Search failed: {r}" if isinstance(r, Exception) else str(r)}
                for c, r in zip(calls, results)
            ]
        else:
            found = "\n\n".join(f'Results for "{q}":\n{r}'
                                for q, r in zip(queries, results)
                                if not isinstance(r, Exception))
            followup.append({"role": "user", "content":
                             f"{found}\n\nAnswer the question now using these results."})
        return await ask_model(followup, search=False, retried=True)

    text = TOOL_SYNTAX_RE.sub("", text).strip()
    if not text:
        if choice.finish_reason == "length":
            raise RuntimeError(f"hit the {MAX_TOKENS}-token cap before writing an answer")
        raise RuntimeError(f"empty reply (finish_reason={choice.finish_reason})")
    return text


# -------------------------------------------------------------- handlers ----

async def keep_typing(bot, chat_id: int) -> None:
    """Telegram's typing indicator lasts ~5s, so resend it until cancelled."""
    while True:
        await bot.send_chat_action(chat_id, ChatAction.TYPING)
        await asyncio.sleep(4)


async def send_reply(msg: Message, text: str) -> None:
    for chunk in split_message(text):
        body = to_html(chunk)
        try:
            sent = await msg.reply_text(
                body, parse_mode=ParseMode.HTML, do_quote=True,
                disable_notification=QUIET,
                link_preview_options=LinkPreviewOptions(is_disabled=not LINK_PREVIEW))
        except BadRequest:  # malformed tags: unformatted beats no reply at all
            log.warning("HTML parse failed, sending plain")
            body = chunk
            sent = await msg.reply_text(chunk, do_quote=True, disable_notification=QUIET)
        # Log the HTML that was sent. Telegram strips formatting from Message.text, and
        # Message.text_html only rebuilds it when the response carries entities - which it
        # doesn't always - so prefer our own copy and fall back to the rebuilt one.
        logged = sent.text_html or ""
        save(sent, text=body if "<" in body else (logged or body))


def build_messages(bot, msg: Message, reply_target: Message | None,
                   replied_to_bot: bool, content: list[dict], own_name: str) -> list[dict]:
    history = f"Recent group chat history:\n{transcript(msg.chat_id, own_name)}"
    if reply_target:
        who = "You" if replied_to_bot else sender_name(reply_target)
        # Prefer the logged copy: it keeps the formatting Telegram strips from sent
        # messages, and this quote is the example the model is likeliest to copy.
        body = stored_text(msg.chat_id, reply_target.message_id) or describe(reply_target)
        history += (f"\n\nThe incoming question is a reply to this message:\n"
                    f"[#{reply_target.message_id}] {who}: {body}")
    history += f"\n\nThe question below was asked by {sender_name(msg)}."

    system = SYSTEM_PROMPT.format(
        bot_name=bot.first_name,
        now=datetime.now(TZ).strftime("%A %d %B %Y, %H:%M %Z"),
        search_note=SEARCH_NOTE if SEARCH else NO_SEARCH_NOTE,
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
    if ALLOWED_CHATS and msg.chat_id not in ALLOWED_CHATS:
        return

    save(msg)  # log everything, including edits (they overwrite the original)
    if update.edited_message:
        return  # don't answer edited messages
    if msg.from_user and msg.from_user.is_bot:
        return  # log other bots as context, but never answer them (loop guard)

    bot = context.bot
    text = msg.text or msg.caption or ""
    reply_target = msg.reply_to_message
    replied_to_bot = bool(reply_target and reply_target.from_user
                          and reply_target.from_user.id == bot.id)
    if not (f"@{bot.username}".lower() in text.lower() or replied_to_bot):
        return

    log.info("Triggered in chat %s (%s) by %s", msg.chat_id, msg.chat.title, sender_name(msg))
    # Show the indicator before any work: downloading images and reading the history
    # happen before the model call, and the group sees nothing until this starts. Send
    # the first one directly rather than via the task, which wouldn't run until the
    # next await point.
    await bot.send_chat_action(msg.chat_id, ChatAction.TYPING)
    typing = asyncio.create_task(keep_typing(bot, msg.chat_id))
    try:
        answer = await answer_question(bot, msg, text, reply_target, replied_to_bot)
    finally:
        typing.cancel()

    await send_reply(msg, answer)


async def answer_question(bot, msg: Message, text: str, reply_target: Message | None,
                          replied_to_bot: bool) -> str:
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

    try:
        try:
            return await ask_model(messages)
        except (APIStatusError, RuntimeError):
            if not SEARCH:
                raise
            log.warning("Retrying without search")
            return await ask_model(messages + [{"role": "user", "content": NO_SEARCH_NOTE}],
                                   search=False, retried=True)
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


def main() -> None:
    if MODEL.endswith(":online"):
        log.warning('MODEL ends with ":online" - that plugin AND the search tool both run, '
                    "which doubles search cost and latency. Drop the suffix.")
    app = Application.builder().token(TELEGRAM_TOKEN).concurrent_updates(True).build()
    app.add_handler(CommandHandler("credits", credits))  # must come before the catch-all
    app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS
            & (filters.UpdateType.MESSAGE | filters.UpdateType.EDITED_MESSAGE)
            & ~filters.StatusUpdate.ALL,
            on_message,
        )
    )
    app.add_error_handler(on_error)
    log.info("Starting %s (search: %s, reasoning: %s)", MODEL,
             SEARCH_ENGINE if SEARCH else "off", REASONING or "default")
    app.run_polling(allowed_updates=["message", "edited_message"])


if __name__ == "__main__":
    main()
