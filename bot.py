#!/usr/bin/env python3
"""
Telegram group bot that answers @mentions with an LLM, using the group's recent
chat history as context. The LLM comes from xAI (Grok) or OpenRouter, whichever
API key is set.

Every message the bot sees is logged to SQLite. When someone @mentions the bot
(or replies to one of its messages), the recent log is sent to the model as a
transcript so it can answer questions like "what's with that last message?".

The database can be shared by several bots in the same group: each bot's
replies are stored under its real name and relabelled "You" only when that
bot builds its own transcript.

MCP servers listed in mcp_servers.json (e.g. Yahoo Finance, Sharesight) are
started by the bot itself and their tools are offered to the model as function
tools. The bot runs the tool calls locally, so the servers never need to be
reachable from the internet and credentials stay on this machine.

Setup:
  1. Create a bot with @BotFather, then /setprivacy -> Disable.
	 (Remove and re-add the bot to any group it's already in.)
  2. pip install -r requirements.txt   (plus Node.js for npx-based MCP servers)
  3. export TELEGRAM_BOT_TOKEN=...
     and exactly one of:  XAI_API_KEY=...  (xAI)   OPENROUTER_API_KEY=...  (OpenRouter)
     (the bot refuses to start if both are set, or neither)
     Optional: MODEL (default grok-4.7 on xAI, xiaomi/mimo-v2.6-pro on OpenRouter),
     REASONING (low / medium / high)
  4. Optional: edit mcp_servers.json
  5. python bot.py [label]
     The label is ignored; it only shows in ps/top, to tell instances apart when
     several run side by side with different environments.

OpenRouter only: SEARCH (on/off), SEARCH_MODEL (runs the model's web searches, default
xiaomi/mimo-v2.6-flash:online), SEARCH_ENGINE (auto/native/exa), MAX_RESULTS,
SEARCH_TOKENS, MAX_TOKENS, TEMPERATURE, and OWNER_USER_ID (enables /credits).
xAI only: SEARCH_TOOLS (default "web_search,x_search"; empty disables search).
"""

import argparse
import asyncio
import base64
import hashlib
import html
import json
import logging
import os
import random
import re
import sqlite3
import sys
import tempfile
import textwrap
import threading
import time
from contextlib import AsyncExitStack
from datetime import datetime, timedelta
from urllib.parse import quote
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client
from openai import APIStatusError, AsyncOpenAI, BadRequestError
from telegram import (
	InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Message, MessageEntity,
	ReplyKeyboardMarkup, Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
	Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, TypeHandler,
	filters,
)
from telegram.error import BadRequest, Forbidden, NetworkError

# ---------------------------------------------------------------- config ----

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
XAI_API_KEY = os.getenv("XAI_API_KEY", "").strip()
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
if XAI_API_KEY and OPENROUTER_API_KEY:
	sys.exit("Both XAI_API_KEY and OPENROUTER_API_KEY are set; set only one, to choose the backend.")
if not (XAI_API_KEY or OPENROUTER_API_KEY):
	sys.exit("Set XAI_API_KEY (xAI) or OPENROUTER_API_KEY (OpenRouter) to choose the backend.")
BACKEND = "openrouter" if OPENROUTER_API_KEY else "xai"
MODEL = os.getenv("MODEL", "").strip() or ("xiaomi/mimo-v2.6-pro" if BACKEND == "openrouter" else "grok-4.7")
REASONING = os.getenv("REASONING", "").strip().lower()  # low / medium / high; empty = model default
MAX_IMAGES = int(os.getenv("MAX_IMAGES", "2"))
# Longer transcript lines (usually the bot's own earlier answers) are cut to this.
TRANSCRIPT_LINE_MAX = int(os.getenv("TRANSCRIPT_LINE_MAX", "400"))
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "20"))  # messages of context (window is 20-29, see recent_history)
DB_PATH = os.getenv("DB_PATH", "chat_log.db")
TZ = ZoneInfo(os.getenv("BOT_TZ", "UTC"))  # e.g. "Europe/London"
# xAI only: Grok's server-side search tools. Set SEARCH_TOOLS="" to disable.
SEARCH_TOOLS = [
	{"type": t} for t in os.getenv("SEARCH_TOOLS", "web_search,x_search").replace(",", " ").split()
] if BACKEND == "xai" else []
# OpenRouter only: its web_search tool, and the model that runs the searches it hands back.
SEARCH = os.getenv("SEARCH", "on").strip().lower() != "off"
SEARCH_MODEL = os.getenv("SEARCH_MODEL", "xiaomi/mimo-v2.6-flash:online").strip()
SEARCH_ENGINE = os.getenv("SEARCH_ENGINE", "auto").strip().lower()
MAX_RESULTS = int(os.getenv("MAX_RESULTS", "20"))
SEARCH_TOKENS = int(os.getenv("SEARCH_TOKENS", "400"))  # cap on each search write-up
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "4000"))  # reply cap; counts reasoning tokens too
TEMPERATURE = float(os.getenv("TEMPERATURE", "0.6"))
OWNER_ID = int(os.getenv("OWNER_USER_ID", "0"))  # who may use /credits
SEARCH_ON = bool(SEARCH_TOOLS) if BACKEND == "xai" else SEARCH
SEARCH_WHAT = "the web and X (Twitter)" if BACKEND == "xai" else "the web"
OPENROUTER_BASE = "https://openrouter.ai/api/v1"

# MCP servers (see mcp_servers.json). Missing file = no MCP tools.
MCP_CONFIG = os.getenv("MCP_CONFIG", "mcp_servers.json")
MCP_TIMEOUT = int(os.getenv("MCP_TIMEOUT", "60"))  # seconds per tool call
MAX_TOOL_ROUNDS = int(os.getenv("MAX_TOOL_ROUNDS", "6"))  # model <-> tools round trips per answer
MAX_TOOL_OUTPUT = int(os.getenv("MAX_TOOL_OUTPUT", "50000"))  # chars per tool result sent to the model
# Chats that get a message when an MCP server goes down. Empty = no alerts.
ALERT_CHATS = {
	int(x) for x in os.getenv("ALERT_CHAT_IDS", "").replace(",", " ").split()
}

# Optional features, all off by default. Each is switched on by its own env flag:
#   MOVERS_EXPLAIN=on             auto-explain another bot's end-of-day "big movers" lists
#   TELEGRAM_DM_BUTTONS=on        preset-prompt buttons in private chats
#   SHARESIGHT_HOLDING_NEWS=on    daily Sharesight holding-news digest (with mute/undo buttons)
#   LOG_STOPPED_GENERATION=on     log when a user presses Telegram's stop button on a draft


def _flag(name: str) -> bool:
	return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


MOVERS_EXPLAIN = _flag("MOVERS_EXPLAIN")
# Bots whose "big movers" lists get the explanation (usernames, comma-separated). Only
# used when MOVERS_EXPLAIN is on.
MOVERS_BOTS = {
	u.lower().lstrip("@") for u in os.getenv("MOVERS_BOTS", "finbotibot").replace(",", " ").split()
} if MOVERS_EXPLAIN else set()
# The bold header, e.g. "≥ 5.0% at close (ASX):" or "≤ -5% at close (NASDAQ, NYSE):"
MOVERS_HEADER = re.compile(r"(?:≥|≤|>=?|<=?)\s*[-−]?\s*\d+(?:\.\d+)?\s*%.*\bclose\b", re.IGNORECASE)
PERCENT = re.compile(r"\d+(?:\.\d+)?\s*%")


# Preset-prompt buttons under the message box in private chats (TELEGRAM_DM_BUTTONS).
DM_BUTTONS = _flag("TELEGRAM_DM_BUTTONS")
DM_PRESETS = {	# button label -> what it asks for; laid out 2 per row
	"News": "today's top headlines",
	"News (AU)": "today's top headlines in Australia",
	"Finance": "today's market news, US and Australia",
	"AI": "today's AI news",
	"Sci-fi": "top trending sci-fi series or movies",
	"SpaceX & Tesla": "latest milestones or announcements from Musk's companies",
}
DM_KEYBOARD = ReplyKeyboardMarkup(
	[list(DM_PRESETS)[i:i + 2] for i in range(0, len(DM_PRESETS), 2)],
	resize_keyboard=True, is_persistent=True,
)

# Daily check for major news about Sharesight holdings (SHARESIGHT_HOLDING_NEWS).
HOLDING_NEWS = _flag("SHARESIGHT_HOLDING_NEWS")
HOLDING_NEWS_TIME = os.getenv("SHARESIGHT_HOLDING_NEWS_TIME", "08:00")	# HH:MM in BOT_TZ
# Sharesight portfolio name -> Telegram username to notify, comma-separated.
HOLDING_NEWS_RECIPIENTS = {
	name.strip().lower(): user.strip().lstrip("@").lower()
	for name, user in (
		pair.split(":", 1) for pair in os.getenv(
			"SHARESIGHT_HOLDING_NEWS_RECIPIENTS",
			"Sue:svs_fluffyegg,SueSMSF:svs_fluffyegg,Rob:rob_llama,RobSMSF:rob_llama",
		).split(",") if ":" in pair
	)
}

# Log (but don't act on) Telegram's stop button on a streamed draft.
LOG_STOPPED_GENERATION = _flag("LOG_STOPPED_GENERATION")

MAX_TG_MESSAGE = 4096

# Under systemd, journald already stamps every line with the time, so don't repeat it.
logging.basicConfig(
	level=logging.INFO,
	format="%(levelname)s %(message)s" if os.getenv("JOURNAL_STREAM")
	else "%(asctime)s %(levelname)s %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")

# Searches (especially X search) can take a while, so allow a generous timeout.
if BACKEND == "xai":
	llm = AsyncOpenAI(api_key=XAI_API_KEY, base_url="https://api.x.ai/v1", timeout=180)
else:
	llm = AsyncOpenAI(
		api_key=OPENROUTER_API_KEY, base_url=OPENROUTER_BASE, timeout=180,
		default_headers={"X-Title": "Telegram group bot"},
	)

SYSTEM_PROMPT = """You are {bot_name}, a bot taking part in a serious Telegram group chat about stocks and investing.

You'll get a transcript of recent group messages, oldest first. Each line looks like:
[#message_id] time Sender (replying to #id): text

The LAST line is the message where someone mentioned you. That's what you're responding to.
When they say "the last message", "that", "above" and so on, they usually mean the message(s)
just before theirs, not their own. If their message was a reply to a specific message, that
message is shown separately after the transcript.

Lines from "You" are your own earlier replies. Other bots may also be in the chat; their
lines appear under their own names, and they are not you. Media appears as [photo],
[voice] etc.; you can't see its contents, only any caption.

You can search {search_what}. Use search whenever a question involves news, prices,
markets, current events, or what people are saying, instead of saying you lack live data.
If you use a source, you may mention it briefly or include one link, but keep it light.

Each reply you write is final: you can't come back later with more. Never say you're
checking, will check, or ask people to stand by. If you need data, call the tool first,
then reply with the result. Finish the whole task in one go: if it needs many lookups,
make them all (several tool calls at once where you can) instead of stopping partway to
ask whether to continue. Only ask a question when the request is genuinely ambiguous.

Reply like a group chat participant: conversational and concise, usually a few sentences.
Refer to people by name, not message ID.

Format with Telegram HTML, never Markdown (no **, #, or backticks). Allowed tags only:
<b>bold</b>, <i>italic</i>, <u>underline</u>, <s>strike</s>, <code>code</code>,
<a href="https://...">link</a>, <blockquote>quote</blockquote>, <tg-spoiler>spoiler</tg-spoiler>.
No headers, list tags, <br> or <p>; use plain newlines. Escape any literal &, < or > in
your text as &amp; &lt; &gt;. Always put every stock ticker in bold, every time, e.g. <b>NVDA</b>.
When listing stocks, write each one as: Name (TICKER) metric, with the bold ticker linked
to its Yahoo Finance page, one stock per line. Show the ticker as the exchange writes it, but
link using Yahoo's symbol with its suffix, e.g.
Apple (<a href="https://finance.yahoo.com/quote/AAPL"><b>AAPL</b></a>) 1.09
Pro Medicus (<a href="https://finance.yahoo.com/quote/PME.AX"><b>PME</b></a>) 0.58
SK Hynix (<a href="https://finance.yahoo.com/quote/000660.KS"><b>000660</b></a>) 2.39
Shorten company names by dropping legal suffixes, share-class tags and "Holdings": Limited,
Ltd, Inc, Corp, Corporation, Co, plc, ADR, Holdings. E.g. "Arm Holdings plc. - ADR" is Arm,
"CrowdStrike Holdings Inc" is CrowdStrike,
"SK Telecom Co Ltd" is SK Telecom, "Pro Medicus Limited" is Pro Medicus.
Also drop a trailing Technology/Technologies, or shorten it to Tech where the name would be
odd without it: Micron Technology is Micron, Palantir Technologies is Palantir, but
DUG Technology is DUG Tech.
Use other formatting sparingly.

Be blunt and direct. Give your actual opinion and concrete numbers when asked.
If confidence in an answer is low, provide a confidence rating.
No disclaimers of any kind (never 'Not advice', 'NFA', 'DYOR' or similar), no moralizing,
no 'it's important to note'. Everyone here knows you're a bot, not an adviser.
Swearing and crude humor are fine if the group is doing it.
"""


def tools_prompt(servers: list["MCPServer"], down: list["MCPServer"]) -> str:
	"""Extra instructions describing the MCP data sources for this request,
	including ones that are down, so Grok reports the error instead of
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

# -------------------------------------------------------------- database ----

# The DB may be shared with other bot processes: WAL mode lets readers and a
# writer work at the same time, and the timeout waits out brief write locks.
class LockedDB:
	"""One SQLite connection shared by the event loop and worker threads (asyncio.to_thread
	keeps a blocked write from freezing the loop). Calls are serialised by a lock and
	results are fetched under it, so callers can use .execute(...).fetchone() as usual."""

	class Rows(list):
		def fetchone(self):
			return self[0] if self else None

		def fetchall(self):
			return list(self)

	def __init__(self, conn: sqlite3.Connection):
		self._conn = conn
		self._lock = threading.RLock()

	def execute(self, sql: str, params=()) -> "LockedDB.Rows":
		with self._lock:
			return self.Rows(self._conn.execute(sql, params).fetchall())

	def commit(self) -> None:
		with self._lock:
			self._conn.commit()


db = LockedDB(sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30))
db.execute("PRAGMA journal_mode=WAL")
db.execute(
	"""CREATE TABLE IF NOT EXISTS messages (
		chat_id    INTEGER,
		message_id INTEGER,
		sender	   TEXT,
		text	   TEXT,
		ts		   INTEGER,
		reply_to   INTEGER,
		PRIMARY KEY (chat_id, message_id)
	)"""
)
# Telegram usernames -> user IDs, so the bot can DM people by username.
db.execute("CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY, user_id INTEGER)")
# Holding-news notifications already sent, so the same story isn't repeated.
db.execute("CREATE TABLE IF NOT EXISTS holding_news (username TEXT, ts INTEGER, text TEXT)")
# Drop "• NOTHING" alerts sent by an earlier version, so they aren't fed back to Grok.
db.execute("DELETE FROM holding_news WHERE length(text) < 40 AND upper(text) LIKE '%NOTHING%'")
# Companies (or "*" for everything) each person has unsubscribed from.
db.execute(
	"CREATE TABLE IF NOT EXISTS holding_news_mutes (username TEXT, code TEXT, PRIMARY KEY (username, code))"
)
# Which companies each holding-news message covered, for its Unsubscribe menu.
db.execute(
	"CREATE TABLE IF NOT EXISTS holding_news_msgs (chat_id INTEGER, message_id INTEGER, codes TEXT, "
	"PRIMARY KEY (chat_id, message_id))"
)
db.commit()


def remember_user(msg: Message) -> None:
	u = msg.from_user
	if u and u.username and not u.is_bot:
		db.execute("INSERT OR REPLACE INTO users VALUES (?, ?)", (u.username.lower(), u.id))
		db.commit()


def user_id_for(username: str) -> int | None:
	row = db.execute("SELECT user_id FROM users WHERE username = ?", (username.lower(),)).fetchone()
	return row[0] if row else None


def now_str() -> str:
	return datetime.now(TZ).strftime("%A %d %B %Y, %H:%M %Z")


def sender_name(msg: Message) -> str:
	if msg.from_user:
		u = msg.from_user
		return u.full_name + (f" (@{u.username})" if u.username else "")
	if msg.sender_chat:  # anonymous admins, linked channels
		return msg.sender_chat.title or "Anonymous"
	return "Unknown"


MEDIA_KINDS = ("photo", "video", "animation", "voice", "video_note", "audio",
			   "document", "sticker", "poll", "location", "contact")


def describe(msg: Message) -> str:
	"""Text of a message as Telegram HTML, with a [media] tag for non-text content.

	Uses text_html / caption_html (rebuilt from Telegram's formatting entities)
	rather than the stripped text, so every bot sharing the DB writes the same
	thing for the same message, whichever of them logs it last."""
	if msg.text:
		text = msg.text_html
	elif msg.caption:
		text = msg.caption_html
	else:
		text = ""
	kind = next((k for k in MEDIA_KINDS if getattr(msg, k, None)), None)
	if kind == "sticker" and msg.sticker.emoji:
		kind = f"sticker {msg.sticker.emoji}"
	elif kind == "poll":
		kind = f"poll: {msg.poll.question}"
	return f"[{kind}] {text}".strip() if kind else text


def save(msg: Message) -> None:
	db.execute(
		"INSERT OR REPLACE INTO messages VALUES (?, ?, ?, ?, ?, ?)",
		(
			msg.chat_id,
			msg.message_id,
			sender_name(msg),
			describe(msg),
			int(msg.date.timestamp()),
			msg.reply_to_message.message_id if msg.reply_to_message else None,
		),
	)
	db.commit()


def recent_history(chat_id: int, limit: int) -> list[tuple]:
	"""Recent messages, oldest first, for the transcript sent to Grok.

	A plain "last N messages" window drops its oldest line every time a
	message arrives, which changes the start of the transcript and so defeats
	Grok's prompt cache (it caches the unchanged start of a prompt). Instead
	the window's start only moves every limit/2 messages: the transcript holds
	between limit and 1.5 x limit messages and in between only grows at the
	end, so everything before the new messages stays cached."""
	total = db.execute("SELECT COUNT(*) FROM messages WHERE chat_id = ?", (chat_id,)).fetchone()[0]
	step = max(1, limit // 2)
	start = max(0, (total - limit) // step * step)
	return db.execute(
		"SELECT message_id, sender, text, ts, reply_to FROM messages "
		"WHERE chat_id = ? ORDER BY message_id LIMIT -1 OFFSET ?",
		(chat_id, start),
	).fetchall()


def format_row(row: tuple, self_name: str) -> str:
	mid, sender, text, ts, reply_to = row
	when = datetime.fromtimestamp(ts, TZ).strftime("%a %H:%M")
	reply = f" (replying to #{reply_to})" if reply_to else ""
	who = "You" if sender == self_name else sender
	if len(text) > TRANSCRIPT_LINE_MAX:
		text = text[:TRANSCRIPT_LINE_MAX].rstrip() + " …[cut]"
	return f"[#{mid}] {when} {who}{reply}: {text}"


HTML_TAG_RE = re.compile(r"<(/?)([a-z][a-z0-9-]*)\b[^>]*>", re.I)


def split_html(text: str, size: int = MAX_TG_MESSAGE - 96) -> list[str]:
	"""Split Telegram HTML into messages of at most `size` characters.

	Cuts at a newline where it can, never inside a tag or an entity, and keeps every
	message well-formed: tags still open at a cut are closed there and re-opened at the
	start of the next message, so links and bold survive a split."""
	chunks, reopen = [], ""  # reopen: opening tags carried over from the previous chunk
	while len(reopen) + len(text) > size:
		room = size - len(reopen) - 32  # headroom for the closing tags
		cut = text.rfind("\n", 0, room)
		if cut <= 0:
			cut = room
		if text.rfind("<", 0, cut) > text.rfind(">", 0, cut):  # inside a tag
			cut = text.rfind("<", 0, cut)
		amp = text.rfind("&", max(0, cut - 10), cut)
		if amp != -1 and ";" not in text[amp:cut]:  # inside an entity
			cut = amp
		cut = max(cut, 1)
		head, text = reopen + text[:cut], text[cut:].lstrip()
		stack = []  # (name, opening tag) still open at the end of head
		for m in HTML_TAG_RE.finditer(head):
			name = m.group(2).lower()
			if not m.group(1):
				stack.append((name, m.group(0)))
			else:
				for i in range(len(stack) - 1, -1, -1):
					if stack[i][0] == name:
						del stack[i]
						break
		chunks.append(head + "".join(f"</{n}>" for n, _ in reversed(stack)))
		reopen = "".join(tag for _, tag in stack)
	if text.strip():
		chunks.append(reopen + text)
	return chunks


# [label](url), where label may itself be bracketed, as in Grok's [[1]](url) citations
MD_LINK = re.compile(r"\[(\[[^\]]*\]|[^\[\]]+)\]\((https?://[^\s)]+)\)")
MD_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*|__(?=\S)(.+?)(?<=\S)__")
MD_CODE = re.compile(r"`([^`\n]+)`")


# "Not advice." / "NFA" / "(DYOR)" tacked onto the end of a reply
TRAILING_DISCLAIMER = re.compile(
	r"(?:\s*[\(\[]?\s*(?:this is )?(?:not (?:financial |investment )?advice|nfa|dyor)\b[.!]?\s*[\)\]]?[.!]?)+\s*$",
	re.IGNORECASE,
)


def strip_summary(text: str) -> str:
	"""Drop a closing wrap-up paragraph ("Typical small-cap swings on thin
	catalysts.") from a movers explanation: the last paragraph, if it names no
	ticker while earlier ones do. Stock lines always carry a bold ticker."""
	paras = re.split(r"\n\s*\n", text.strip())
	if len(paras) > 1 and "<b>" not in paras[-1] and any("<b>" in p for p in paras[:-1]):
		return "\n\n".join(paras[:-1])
	return text


def strip_disclaimer(text: str) -> str:
	"""Grok adds these despite the prompt, and copies its own earlier ones from the transcript."""
	return TRAILING_DISCLAIMER.sub("", text).rstrip()


def md_to_html(text: str) -> str:
	"""Convert Markdown that Grok slips into its replies (links, **bold**, `code`)
	into Telegram HTML. The prompt asks for HTML, but models don't always comply."""
	def link(m: re.Match) -> str:
		label, url = m.group(1), m.group(2)
		return f'<a href="{html.escape(url, quote=True)}">{label}</a>'
	text = MD_LINK.sub(link, text)
	text = MD_BOLD.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", text)
	return MD_CODE.sub(r"<code>\1</code>", text)

# ---------------------------------------------------------------- tickers --

# Optional deterministic ticker formatting (off by default: the model is told to bold and
# link tickers itself). AUTO_LINK links ticker-shaped words to Yahoo Finance; AUTO_BOLD
# bolds them instead (and wins if both are on). Text already inside <a>, <b> or <code>
# is left alone, so the model's own links are never doubled.
AUTO_LINK = _flag("AUTO_LINK")
AUTO_BOLD = _flag("AUTO_BOLD")
EXTRA_TICKERS = {t.upper() for t in os.getenv("EXTRA_TICKERS", "").replace(",", " ").split()}
EXTRA_NOT_TICKERS = {t.upper() for t in os.getenv("NOT_TICKERS", "").replace(",", " ").split()}
# Yahoo quotes crypto as BTC-USD, not BTC.
CRYPTO = {"BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "BNB", "LTC", "DOT", "AVAX",
		  "LINK", "TRX", "SHIB", "PEPE", "WLFI", "USDT", "USDC"} | {
	t.upper() for t in os.getenv("CRYPTO_TICKERS", "").replace(",", " ").split()}

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
# One pass for both shapes: a second pass over text the first one had already wrapped
# could match inside the inserted <a href> markup.
COMBINED_TICKER_RE = re.compile(f"{TICKER_RE.pattern}|{ONE_LETTER_RE.pattern}")


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
		seg = COMBINED_TICKER_RE.sub(
			lambda m: wrap(m.group(0)) if len(m.group(0)) == 1 or is_ticker(m.group(0))
			else m.group(0), seg)
		pieces.append(seg)
	return "".join(pieces)


def bold_tickers(out: str) -> str:
	"""Kept for when bolding is wanted instead of linking: AUTO_BOLD=on."""
	return mark_tickers(out, lambda w: f"<b>{w}</b>")


def link_tickers(out: str) -> str:
	return mark_tickers(out, lambda w: f'<a href="{yahoo_url(w)}">{ticker_label(w)}</a>')


def auto_mark_tickers(text: str) -> str:
	"""Apply AUTO_BOLD / AUTO_LINK to a finished reply (neither on: unchanged)."""
	if AUTO_BOLD:
		return bold_tickers(text)
	return link_tickers(text) if AUTO_LINK else text


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
# adds up fast. Trim what Grok doesn't need to pick and call a tool well.
HIDDEN_PARAMS = {"response_format"}	# optional params Grok shouldn't bother with
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
	if "$defs" in out:	# drop definitions nothing refers to any more
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


def function_tool(name: str, description: str, parameters: dict) -> dict:
	"""A function tool in the request format of the chosen backend: flat for xAI's
	Responses API, nested under "function" for OpenRouter's chat completions."""
	fn = {"name": name, "description": description, "parameters": parameters}
	return {"type": "function", **fn} if BACKEND == "xai" else {"type": "function", "function": fn}


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
		self.error: str | None = None	# why the server is down, if it is
		self.bot = None	# for posting alerts to ALERT_CHATS
		self.tools: list[dict] = []	# Responses API function tool specs
		self.fn_names: dict[str, str] = {}	# function name sent to Grok -> MCP tool name
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
		if self._task and not self._task.done():	# already started: don't spawn a second copy
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
				await asyncio.to_thread(save, sent)  # so it shows up in the transcript the model sees
			except Exception:
				log.exception("Couldn't send MCP alert to chat %s", chat_id)

	async def _load_tools(self, session: ClientSession) -> None:
		listed = (await session.list_tools()).tools
		self.tools, self.fn_names = [], {}	# a restart must not append every tool twice
		allow = set(self.cfg.get("allowed_tools", []))
		blocked = set(self.cfg.get("blocked_tools", []))
		for t in listed:
			if t.name in blocked or not (t.name in allow if allow else _looks_read_only(t)):
				continue
			fn = f"{self.label}__{t.name}"[:64]
			self.fn_names[fn] = t.name
			self.tools.append(function_tool(
				fn, compact_description(t.description or ""),
				compact_schema(t.inputSchema or {"type": "object", "properties": {}}),
			))
		enabled = set(self.fn_names.values())
		skipped = sorted(t.name for t in listed if t.name not in enabled)
		log_names(f"MCP {self.label}: {len(enabled)} tools:", sorted(enabled))
		if skipped:
			log_names(f"MCP {self.label}: {len(skipped)} skipped:", skipped)

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


def log_names(head: str, names: list[str], width: int = 80) -> None:
	"""Log a list of tool names on lines short enough not to wrap in a terminal or journal."""
	lines = textwrap.wrap(", ".join(n.removeprefix("get_") for n in names), width - len(head))
	for i, line in enumerate(lines):
		log.info("%s %s", head if i == 0 else " " * len(head), line)


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


# Sharesight share-class tags: "Crowdstrike Holdings Inc - Ordinary Shares - Class A"
SHARE_CLASS = re.compile(
	r"(?:\s+-\s+(?:ordinary shares|class [a-z]|common stock|adr|ads|depositary receipts?))+\s*$",
	re.IGNORECASE,
)
# Trailing legal suffixes: "Arm Holdings plc." -> "Arm Holdings"
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
	if out.get("type") == "Ordinary Shares":	# the default; only say when it's something else
		del out["type"]
	if out.get("group_name") == "All Holdings":	# ungrouped report
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
		for key in ("api_transaction", "links"):	# API housekeeping
			data.pop(key, None)
		report = data.get("report")
		if isinstance(report, dict):
			if isinstance(report.get("currency"), dict):
				report["currency"] = report["currency"].get("code")
			for key in REPORT_DROP:
				report.pop(key, None)
			if report.get("grouping") == "ungrouped":
				report.pop("grouping")
				report.pop("sub_totals", None)	# one group, same as the report totals
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


# Output items replayed into the next round: Grok's tool calls, its reasoning
# (needed for it to continue its line of thought) and any text it wrote.
# Server-side search calls aren't replayed; their results show in its text.
REPLAY_TYPES = ("function_call", "reasoning", "message")
STATELESS_ROUNDS = True	# switched off if xAI rejects a replayed conversation


def _as_input(item) -> dict:
	return item.model_dump(exclude_none=True) if hasattr(item, "model_dump") else dict(item)


# Diagnostic: log short hashes of each prompt part, to check which parts stay
# identical between requests (anything that changes early defeats the cache).
LOG_PROMPT_FINGERPRINT = _flag("LOG_PROMPT_FINGERPRINT")


def _fp(text: str) -> str:
	return hashlib.sha1(text.encode()).hexdigest()[:8]


def token_usage(responses) -> dict[str, int]:
	"""Token totals across a request's rounds. Each follow-up round re-reads the
	whole conversation, which is what prompt caching (the "cached" count) saves."""
	tok = {"in": 0, "cached": 0, "out": 0, "reasoning": 0}
	for r in responses:
		u = getattr(r, "usage", None)
		if not u:
			continue
		tok["in"] += getattr(u, "input_tokens", 0) or 0
		tok["out"] += getattr(u, "output_tokens", 0) or 0
		tok["cached"] += getattr(getattr(u, "input_tokens_details", None), "cached_tokens", 0) or 0
		tok["reasoning"] += getattr(getattr(u, "output_tokens_details", None), "reasoning_tokens", 0) or 0
	return tok


# ------------------------------------------------------------------ xAI ----

async def xai_create(on_text=None, **kwargs):
	"""One Responses API call. With on_text, stream it, calling on_text(text so far)
	as text arrives, and return the final response object."""
	if on_text is None:
		return await llm.responses.create(**kwargs)
	stream = await llm.responses.create(stream=True, **kwargs)
	text, final = "", None
	async for event in stream:
		if event.type == "response.output_text.delta":
			text += event.delta
			on_text(text)
		elif event.type in ("response.completed", "response.incomplete", "response.failed"):
			final = event.response
	if final is None:
		raise RuntimeError("The stream ended without a final response")
	if final.status == "failed":
		raise RuntimeError(f"xAI response failed: {final.error}")
	return final


async def ask_xai(
	content: list[dict], system: str, servers: list["MCPServer"], must_search: bool,
	on_text=None, cache_key: str | None = None, bot_name: str = "", user_id: int | None = None,
) -> str:
	"""Ask Grok through xAI's Responses API, running any MCP tool calls it makes, until
	it produces an answer. With on_text, responses are streamed and on_text gets the
	text so far. With must_search, Grok only gets web/X search and has to use it at
	least once."""
	tools = list(SEARCH_TOOLS) if must_search else SEARCH_TOOLS + [t for s in servers for t in s.tools]
	kwargs = {
		"model": MODEL,
		**({"tools": tools} if tools else {}),
		**({"reasoning": {"effort": REASONING}} if REASONING else {}),
	}
	if cache_key:
		# Send a conversation's requests to the same xAI server, where its cached
		# prompt lives: prompt_cache_key for the Responses API, plus the
		# equivalent x-grok-conv-id header.
		cache_key = f"{bot_name}-{cache_key}"
		kwargs["extra_body"] = {"prompt_cache_key": cache_key}
		kwargs["extra_headers"] = {"x-grok-conv-id": cache_key}
	# The system prompt goes in as a message rather than `instructions`: xAI
	# rejects `instructions` alongside previous_response_id, and as a message
	# it's stored with the conversation, so follow-up rounds keep it.
	first_input = [
		{"role": "system", "content": system},
		{"role": "user", "content": content},
	]
	if LOG_PROMPT_FINGERPRINT:
		user_text = content[0].get("text", "") if content else ""
		log.info(
			"Prompt fingerprint: system=%s tools=%s user-start=%s (%d chars)",
			_fp(system), _fp(json.dumps(tools, sort_keys=True)), _fp(user_text[:500]), len(user_text),
		)
	if must_search:
		try:
			resp = await xai_create(on_text, input=first_input, tool_choice="required", **kwargs)
		except BadRequestError as e:
			log.warning("xAI rejected tool_choice=required (%s); retrying without it", e)
			resp = await xai_create(on_text, input=first_input, **kwargs)
	else:
		resp = await xai_create(on_text, input=first_input, **kwargs)
	responses = [resp]	# every round, for the search and token totals
	tool_calls = 0
	# Follow-up rounds re-send the whole conversation, which starts with exactly
	# what the previous round sent, so it's served from xAI's prompt cache.
	# (Continuing with previous_response_id instead was barely cached.)
	conversation = list(first_input)
	for round_no in range(MAX_TOOL_ROUNDS):
		calls = [item for item in resp.output if item.type == "function_call"]
		if not calls:
			break
		tool_calls += len(calls)
		outputs = await asyncio.gather(*(run_tool(c, user_id) for c in calls))
		# On the last round, make Grok answer with what it has rather than call more tools.
		last = round_no == MAX_TOOL_ROUNDS - 1
		if last:
			log.warning("Hit MAX_TOOL_ROUNDS (%d); making Grok answer with what it has", MAX_TOOL_ROUNDS)
		results = [
			{"type": "function_call_output", "call_id": c.call_id, "output": out}
			for c, out in zip(calls, outputs)
		]
		extra = {"tool_choice": "none"} if last else {}
		global STATELESS_ROUNDS
		if STATELESS_ROUNDS:
			conversation += [_as_input(item) for item in resp.output if item.type in REPLAY_TYPES]
			conversation += results
			try:
				resp = await xai_create(on_text, input=conversation, **kwargs, **extra)
			except BadRequestError as e:
				log.warning("xAI rejected the replayed conversation (%s); "
							"using previous_response_id from now on", e)
				STATELESS_ROUNDS = False
		if not STATELESS_ROUNDS:
			resp = await xai_create(
				on_text, previous_response_id=resp.id, input=results, **kwargs, **extra
			)
		responses.append(resp)
	searches = sum(
		1 for r in responses for item in r.output if item.type.endswith("_search_call")
	)
	tok = token_usage(responses)
	if len(responses) > 1:	# per round, to see where caching does or doesn't kick in
		per_round = []
		for r in responses:
			t = token_usage([r])
			per_round.append(f"{t['in']}/{t['cached']}/{t['out']}")
		log.info("Rounds in/cached/out: %s", " | ".join(per_round))
	log.info(
		"Grok: %d rounds, %d searches, %d tools | in %d (%.0f%% cached), out %d (%d reasoning)",
		len(responses), searches, tool_calls, tok["in"],
		100 * tok["cached"] / tok["in"] if tok["in"] else 0, tok["out"], tok["reasoning"],
	)
	return (resp.output_text or "").strip()



# ------------------------------------------------------------ openrouter ----

# MiMo sometimes prints a tool call as plain text instead of calling the tool. The
# queries are usually fine, so pull them out and run them rather than binning them.
FAKE_CALL_RE = re.compile(
	r"<parameter=query>(.*?)(?:</parameter>|</tool_call>|<|$)", re.DOTALL | re.IGNORECASE)
TOOL_SYNTAX_RE = re.compile(
	r"<tool_call>.*?(?:</tool_call>|$)|<function=.*?(?:</function>|$)|<\|?tool_calls?\|?>",
	re.DOTALL | re.IGNORECASE)

SEARCH_TOOL = [{
	"type": "openrouter:web_search",
	"parameters": {"engine": SEARCH_ENGINE, "max_total_results": MAX_RESULTS},
}]

NO_TOOLS_NOTE = (
	"\n\nYou have no web access and no tools this time. Answer from the chat history and "
	"your own knowledge, say plainly what you can't check, and never output tool-call "
	"syntax or JSON as text."
)


def chat_content(parts: list[dict]) -> list[dict]:
	"""Message content in chat-completions format; callers build it in Responses format."""
	out = []
	for p in parts:
		if p["type"] == "input_text":
			out.append({"type": "text", "text": p["text"]})
		elif p["type"] == "input_image":
			out.append({"type": "image_url", "image_url": {"url": p["image_url"]}})
	return out


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


def call_query(arguments: str) -> str:
	"""Pull the search string out of a tool call's JSON arguments, whatever the model
	named the field."""
	try:
		args = json.loads(arguments or "{}")
	except ValueError:
		return ""
	for key in ("query", "q", "search_query", "keywords", "input"):
		if args.get(key):
			return str(args[key])
	return ""


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


async def complete(messages: list[dict], tools: list[dict], on_text=None,
				   tool_choice: str | None = None, session: str | None = None):
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
			# Sticky routing: keep a chat's requests on the provider endpoint that holds
			# its cached prompt (session_id, sent as a header too), with prompt_cache_key
			# as the weaker fallback some providers read.
			**({"session_id": session, "prompt_cache_key": session} if session else {}),
		},
		**({"extra_headers": {"x-session-id": session}} if session else {}),
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


def usage_numbers(u) -> tuple[int, int, int, float]:
	"""(input, cached input, output tokens, cost) from a usage object; zeros if absent."""
	cached = getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0)
	return (getattr(u, "prompt_tokens", 0) or 0, cached or 0,
			getattr(u, "completion_tokens", 0) or 0, getattr(u, "cost", 0) or 0)


MAX_SEARCHES = 5  # search calls run per round; the rest are told to try later


def is_mcp_tool(name: str) -> bool:
	return any(name in s.fn_names for s in MCP_SERVERS.values())


async def run_search(arguments: str) -> str:
	query = call_query(arguments)
	if not query or not SEARCH_MODEL:
		return "Search isn't available."
	try:
		return await run_query(query)
	except Exception as e:
		return f"Search failed: {e}"


async def run_calls(calls: list, user_id: int | None) -> list[str]:
	"""Run the tool calls the model handed back: MCP data tools, or else web searches
	(all of them concurrently). Every call needs a reply, so searches past MAX_SEARCHES
	get a note instead of being dropped."""
	searches = 0
	jobs = []
	for c in calls:
		if is_mcp_tool(c.name):
			jobs.append(run_tool(c, user_id))
			continue
		searches += 1
		if searches > MAX_SEARCHES:
			jobs.append(asyncio.sleep(0, "Skipped: too many searches at once. Use what you already have."))
		else:
			jobs.append(run_search(c.arguments))
	return list(await asyncio.gather(*jobs))


async def ask_openrouter(
	content: list[dict], system: str, servers: list["MCPServer"], user_id: int | None,
	on_text=None, session: str | None = None, tools_on: bool = True,
) -> str:
	"""Ask the model, running the tool calls it makes (MCP data tools here, searches via
	SEARCH_MODEL) for up to MAX_TOOL_ROUNDS rounds, until it produces an answer.
	With on_text, replies are streamed and on_text gets the text so far."""
	t0 = time.monotonic()
	search_on = tools_on and SEARCH
	servers = servers if tools_on else []
	conv = [{"role": "system", "content": system}, {"role": "user", "content": chat_content(content)}]
	rounds = tool_calls = 0
	tok = {"in": 0, "cached": 0, "out": 0, "cost": 0.0}
	r = None
	while True:
		tools = (SEARCH_TOOL if search_on else []) + [t for s in servers for t in s.tools]
		final = rounds >= MAX_TOOL_ROUNDS
		r = await complete(conv, tools, on_text, tool_choice="none" if final else None,
						   session=session)
		rounds += 1
		n_in, n_cached, n_out, cost = usage_numbers(r.usage)
		tok["in"] += n_in
		tok["cached"] += n_cached
		tok["out"] += n_out
		tok["cost"] += cost
		log.info("Round %d: in %d (%d cached) out %d, %s", rounds, n_in, n_cached, n_out, r.finish)
		if final:
			log.warning("Hit MAX_TOOL_ROUNDS (%d); made the model answer with what it has",
						MAX_TOOL_ROUNDS)
			break
		if r.calls:
			tool_calls += len(r.calls)
			outputs = await run_calls(r.calls, user_id)
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
	log.info("%.1fs, %d rounds, %d tools: in %d (%.0f%% cached) out %d, $%.4f",
			 time.monotonic() - t0, rounds, tool_calls, tok["in"],
			 100 * tok["cached"] / tok["in"] if tok["in"] else 0, tok["out"], tok["cost"])
	text = TOOL_SYNTAX_RE.sub("", r.text).strip()
	if not text:
		if r.finish == "length":
			raise RuntimeError(f"hit the {MAX_TOKENS}-token cap before writing an answer")
		raise RuntimeError(f"empty reply (finish_reason={r.finish})")
	return text


async def ask_llm(
	content: list[dict], user_id: int | None, bot_name: str, on_text=None,
	must_search: bool = False, cache_key: str | None = None,
) -> str:
	"""Ask the configured backend (xAI or OpenRouter), running any MCP tool calls the
	model makes, until it produces an answer. With on_text, the reply is streamed and
	on_text gets the text so far. With must_search, the model only gets web search."""
	must_search = must_search and SEARCH_ON
	if must_search:
		servers, down = [], []
	else:
		servers = [s for s in MCP_SERVERS.values() if s.session and s.permits(user_id)]
		down = [s for s in MCP_SERVERS.values() if s.error and s.permits(user_id)]
	system = SYSTEM_PROMPT.format(bot_name=bot_name, search_what=SEARCH_WHAT) + tools_prompt(servers, down)
	if BACKEND == "xai":
		return await ask_xai(content, system, servers, must_search, on_text, cache_key, bot_name, user_id)
	# Sticky routing: keep a chat's requests on the provider that holds its cached prompt.
	session = f"{bot_name}-{cache_key}" if cache_key else None
	try:
		return await ask_openrouter(content, system, servers, user_id, on_text, session)
	except (APIStatusError, RuntimeError):
		if not (SEARCH_ON or servers):
			raise
		log.warning("Retrying without tools", exc_info=True)
		return await ask_openrouter(content, system + NO_TOOLS_NOTE, [], user_id, on_text,
									session, tools_on=False)


# ---------------------------------------------------------------- drafts ----

# Telegram's own tags, plus one half-written tag at the end of the text. A bare "<" in
# Grok's prose ("<5% from ATH") is not a tag and must survive.
TG_TAG_NAMES = r"(?:b|strong|i|em|u|s|code|pre|a|blockquote|tg-spoiler)"
TG_TAG_RE = re.compile(rf"</?{TG_TAG_NAMES}\b[^>]*>|</?{TG_TAG_NAMES}\b[^>]*$", re.I)


class Draft:
	"""Streams a reply into a Telegram message draft (private chats only).

	Starts with an empty draft, which Telegram shows as "Thinking...", then
	shows Grok's text as it arrives. Drafts vanish after 30 s without an
	update, so it's re-sent at least every KEEPALIVE seconds, e.g. while
	tools run. The finished reply is sent as a normal message."""

	INTERVAL = 1.0	# seconds between updates while text is arriving
	KEEPALIVE = 20	# re-send before Telegram's 30 s draft timeout

	def __init__(self, bot, chat_id: int):
		self.bot = bot
		self.chat_id = chat_id
		self.draft_id = random.randint(1, 2**31 - 1)
		self.text = ""	# Grok's latest raw text (Telegram HTML, possibly half-written)
		self._shown: str | None = None
		self._last = 0.0
		self._task: asyncio.Task | None = None
		self._typing: asyncio.Task | None = None

	async def start(self) -> None:
		# Typing indicator first (until text starts arriving), then the draft.
		self._typing = await start_typing(self.bot, self.chat_id)
		await self._send("")	# "Thinking..."
		self._task = asyncio.create_task(self._loop())

	def update(self, text: str) -> None:
		self.text = text

	def stop(self) -> None:
		for task in (self._task, self._typing):
			if task:
				task.cancel()

	def _render(self) -> str:
		# Half-written HTML would be rejected, so drafts are plain text; the
		# final message gets the real formatting.
		plain = html.unescape(TG_TAG_RE.sub("", TOOL_SYNTAX_RE.sub("", self.text))).strip()
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
			else:	# python-telegram-bot versions from before drafts existed
				await self.bot.do_api_request(
					"sendMessageDraft",
					api_kwargs={"chat_id": self.chat_id, "draft_id": self.draft_id, "text": text},
				)
		except Exception as e:
			log.warning("sendMessageDraft failed: %s", e)
		self._shown, self._last = text, time.monotonic()


# ---------------------------------------------------------- holding news ----

HOLDING_NEWS_PROMPT = """This is an automated daily check, not a chat message.

Holdings:
{holdings}

Search {search_what} for MAJOR news about these companies published in the past 24 hours.
The bar is high. Only material, price-moving events count: results or earnings, guidance
changes, takeover or merger news, capital raisings, major contract wins or losses, regulatory
or clinical-trial decisions, trading halts or suspensions, CEO or CFO departures, dividend
cuts or suspensions, delistings, serious legal action. Ignore routine filings (director share
dealings, buy-back updates, AGM notices, presentations with nothing new), general market or
sector moves, analyst price-target changes, opinion pieces, and anything older than 24 hours
or that you can't date. When in doubt, leave it out. Most days the answer is nothing.

If nothing qualifies, reply with exactly the word NOTHING, with no bullet or anything else.
Otherwise reply with only the qualifying items as bullet points, one item per line, no intro
and no blank lines between them. Start each line with "• ":
• Name (linked bold TICKER): what happened, with one source link.
{already}
It's now {now}."""


async def current_holdings(server: "MCPServer", portfolio_names: list[str]) -> dict[str, str]:
	"""Current holdings across the named portfolios, as {"CODE (MARKET)": name}."""
	data = json.loads(await server.call("list_portfolios", {}))
	portfolios = {p["name"].lower(): p for p in data.get("portfolios", [])}
	holdings: dict[str, str] = {}
	for name in portfolio_names:
		p = portfolios.get(name)
		if not p:
			log.warning(
				"Holding news: no Sharesight portfolio called %r (have: %s)",
				name, ", ".join(sorted(portfolios)),
			)
			continue
		report = json.loads(await server.call(
			"get_performance_report", {"portfolio_id": p["id"], "include_sales": False}
		)).get("report") or {}
		for h in table_records(report.get("holdings")):
			if h.get("code"):
				holdings[f"{h['code']} ({h.get('market', '?')})"] = h.get("name") or h["code"]
	return holdings


async def holding_news_text(bot, username: str, holdings: dict[str, str]) -> str | None:
	"""Ask Grok for major news about these holdings; None if there's nothing."""
	recent = db.execute(
		"SELECT text FROM holding_news WHERE username = ? AND ts > ? ORDER BY ts",
		(username, int((datetime.now() - timedelta(days=3)).timestamp())),
	).fetchall()
	already = (
		"\nAlready reported in the last few days; don't repeat these unless there's a "
		"genuinely new development:\n" + "\n".join(r[0] for r in recent) + "\n"
	) if recent else ""
	prompt = HOLDING_NEWS_PROMPT.format(
		now=now_str(), search_what=SEARCH_WHAT,
		holdings="\n".join(f"- {k}: {v}" for k, v in sorted(holdings.items())),
		already=already,
	)
	raw = strip_disclaimer(await ask_llm(
		[{"type": "input_text", "text": prompt}], None, bot.first_name, must_search=True,
		cache_key=f"holding-news-{username}",
	))
	if is_nothing(raw):
		return None
	return as_bullets(md_to_html(raw))


def is_nothing(raw: str) -> bool:
	"""True for Grok's "nothing to report" answer, however it's dressed up
	("NOTHING", "• NOTHING", "**Nothing.**", "None") or an empty reply."""
	plain = html.unescape(re.sub(r"<[^>]+>", "", raw or ""))
	words = re.sub(r"[^A-Za-z ]", " ", plain).split()
	# A real item is longer, even one about a company called "Nothing ...".
	return not words or (words[0].upper() in ("NOTHING", "NONE") and len(words) <= 5)


def as_bullets(text: str) -> str:
	"""One "• " bullet per non-empty line, whatever bullet style (if any) Grok used."""
	lines = (re.sub(r"^\s*(?:[•\-*–·]|\d+[.)])\s+", "", l).strip() for l in text.splitlines())
	return "\n".join(f"• {l}" for l in lines if l)


async def send_html(bot, chat_id: int, text: str, reply_markup=None) -> Message:
	"""Send Telegram HTML to a chat, falling back to plain text if it's rejected."""
	try:
		return await bot.send_message(
			chat_id, text, parse_mode=ParseMode.HTML, link_preview_options=NO_PREVIEW,
			reply_markup=reply_markup,
		)
	except BadRequest as e:
		if "parse entities" not in str(e).lower():
			raise
		plain = re.sub(r'<a href="([^"]*)">(.*?)</a>', r"\2 (\1)", text)
		plain = html.unescape(re.sub(r"<[^>]+>", "", plain))
		return await bot.send_message(
			chat_id, plain, link_preview_options=NO_PREVIEW, reply_markup=reply_markup
		)


def muted_codes(username: str) -> set[str]:
	rows = db.execute("SELECT code FROM holding_news_mutes WHERE username = ?", (username,))
	return {r[0] for r in rows}


def set_muted(username: str, code: str, muted: bool) -> None:
	if muted:
		db.execute("INSERT OR IGNORE INTO holding_news_mutes VALUES (?, ?)", (username, code))
	else:
		db.execute("DELETE FROM holding_news_mutes WHERE username = ? AND code = ?", (username, code))
	db.commit()


def news_codes(news: str, holdings: dict[str, str]) -> list[str]:
	"""Holding codes that appear as bold tickers or Yahoo links in a news message."""
	held = {k.split(" ")[0].upper() for k in holdings}
	found = {m.upper() for m in re.findall(r"<b>([^<]{1,12})</b>", news)}
	found |= {m.split(".")[0].upper() for m in re.findall(r"finance\.yahoo\.com/quote/([\w.\-^]+)", news)}
	return sorted(held & found)


UNSUBSCRIBE_BUTTON = InlineKeyboardMarkup(
	[[InlineKeyboardButton("🔕 Unsubscribe…", callback_data="hn:menu")]]
)


def unsubscribe_menu(codes: list[str]) -> InlineKeyboardMarkup:
	rows = [
		[InlineKeyboardButton(code, callback_data=f"hn:mute:{code}") for code in codes[i:i + 3]]
		for i in range(0, len(codes), 3)
	]
	rows.append([InlineKeyboardButton("All holding news", callback_data="hn:mute:*")])
	rows.append([InlineKeyboardButton("Cancel", callback_data="hn:cancel")])
	return InlineKeyboardMarkup(rows)


def undo_button(code: str) -> InlineKeyboardMarkup:
	what = "all holding news" if code == "*" else f"{code} news"
	return InlineKeyboardMarkup(
		[[InlineKeyboardButton(f"↩️ Undo (unsubscribed from {what})", callback_data=f"hn:undo:{code}")]]
	)


async def send_holding_news(bot, chat_id: int, news: str, codes: list[str]) -> None:
	"""Send a holding-news message with its Unsubscribe button on the last part."""
	chunks = split_html(f"📰 <b>Holding news</b> (past 24h)\n\n{news}")
	for i, chunk in enumerate(chunks):
		last = i == len(chunks) - 1
		sent = await send_html(bot, chat_id, chunk, reply_markup=UNSUBSCRIBE_BUTTON if last else None)
		save(sent)
		if last:
			db.execute("INSERT OR REPLACE INTO holding_news_msgs VALUES (?, ?, ?)",
					   (chat_id, sent.message_id, ",".join(codes)))
			db.commit()


async def on_holding_news_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
	"""The Unsubscribe button and its menu under holding-news messages."""
	query = update.callback_query
	username = (query.from_user.username or "").lower()
	if username not in HOLDING_NEWS_RECIPIENTS.values():
		await query.answer("You're not set up for holding news.")
		return
	_, action, *rest = query.data.split(":", 2)
	code = rest[0] if rest else ""
	msg = query.message
	if action == "menu":
		row = db.execute(
			"SELECT codes FROM holding_news_msgs WHERE chat_id = ? AND message_id = ?",
			(msg.chat_id, msg.message_id),
		).fetchone()
		codes = [c for c in (row[0] if row else "").split(",") if c]
		await query.answer()
		await query.edit_message_reply_markup(unsubscribe_menu(codes))
	elif action == "cancel":
		await query.answer()
		await query.edit_message_reply_markup(UNSUBSCRIBE_BUTTON)
	elif action == "mute":
		set_muted(username, code, True)
		log.info("@%s unsubscribed from holding news: %s", username, code)
		await query.answer("Unsubscribed from " + ("all holding news" if code == "*" else f"{code} news"))
		await query.edit_message_reply_markup(undo_button(code))
	elif action == "undo":
		set_muted(username, code, False)
		log.info("@%s resubscribed to holding news: %s", username, code)
		await query.answer("Resubscribed")
		await query.edit_message_reply_markup(UNSUBSCRIBE_BUTTON)


async def run_holding_news(bot, only_username: str | None = None) -> dict[str, tuple[str, list[str]] | None]:
	"""Check each recipient's holdings; DM anyone with major news.
	Returns {username: (news text, codes it covers) or None}."""
	server = MCP_SERVERS.get("sharesight")
	if not (server and server.session):
		log.warning("Holding news: Sharesight isn't connected, skipping")
		return {}
	people: dict[str, list[str]] = {}
	for portfolio, username in HOLDING_NEWS_RECIPIENTS.items():
		if only_username is None or username == only_username:
			people.setdefault(username, []).append(portfolio)
	results: dict[str, tuple[str, list[str]] | None] = {}
	for username, portfolio_names in people.items():
		muted = muted_codes(username)
		if "*" in muted:
			log.info("Holding news: @%s has unsubscribed from all of it", username)
			continue
		try:
			holdings = await current_holdings(server, portfolio_names)
			holdings = {k: v for k, v in holdings.items() if k.split(" ")[0].upper() not in muted}
			if not holdings:
				continue
			news = await holding_news_text(bot, username, holdings)
		except Exception:
			log.exception("Holding news check failed for @%s", username)
			continue
		results[username] = (news, news_codes(news, holdings)) if news else None
		log.info("Holding news for @%s: %s", username, "found" if news else "nothing major")
		if not news or only_username:	# on-demand checks are replied to by the caller
			continue
		chat_id = user_id_for(username)
		if not chat_id:
			log.warning(
				"Holding news: don't know @%s's user ID yet; they need to message the bot "
				"or a group it's in", username,
			)
			continue
		try:
			await send_holding_news(bot, chat_id, news, results[username][1])
			db.execute("INSERT INTO holding_news VALUES (?, ?, ?)",
					   (username, int(datetime.now().timestamp()), news))
			db.commit()
		except Forbidden:
			log.warning("Holding news: @%s hasn't started a private chat with the bot", username)
	return results


def seconds_until(hhmm: str) -> float:
	hour, minute = (int(x) for x in hhmm.split(":"))
	now = datetime.now(TZ)
	target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
	if target <= now:
		target += timedelta(days=1)
	return (target - now).total_seconds()


async def holding_news_loop(bot) -> None:
	while True:
		wait = seconds_until(HOLDING_NEWS_TIME)
		log.info("Next holding news check in %.1f hours", wait / 3600)
		await asyncio.sleep(wait)
		try:
			await run_holding_news(bot)
		except Exception:
			log.exception("Holding news check failed")


# -------------------------------------------------------------- handlers ----

async def start_typing(bot, chat_id: int) -> asyncio.Task:
	"""Send the typing indicator right now, then keep it going until the task is
	cancelled (Telegram's lasts ~5 s). The first one is sent directly rather than from
	the task, which wouldn't run until the next await, possibly after the prompt has been
	built. A failed send is logged, never fatal."""
	async def send() -> None:
		try:
			await bot.send_chat_action(chat_id, ChatAction.TYPING)
		except Exception as e:
			log.warning("Typing indicator failed: %s", e)

	async def keep() -> None:
		while True:
			await asyncio.sleep(4)
			await send()

	await send()
	return asyncio.create_task(keep())

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
	return {
		"type": "input_image",
		"image_url": f"data:{mime};base64,{base64.b64encode(data).decode()}",
		"detail": "high",
	}

async def holding_news_on_demand(bot, msg: Message) -> None:
	"""/holdingnews in a private chat: run the check now for the sender, and
	always reply, even when there's nothing (handy for testing)."""
	username = (msg.from_user.username or "").lower() if msg.from_user else ""
	if username not in HOLDING_NEWS_RECIPIENTS.values():
		save(await msg.reply_text("You're not set up for holding news."))
		return
	if "*" in muted_codes(username):
		save(await msg.reply_text(
			"You've unsubscribed from all holding news.", reply_markup=undo_button("*")
		))
		return
	draft = Draft(bot, msg.chat_id)
	await draft.start()
	try:
		result = (await run_holding_news(bot, only_username=username)).get(username)
	finally:
		draft.stop()
	if result:
		await send_holding_news(bot, msg.chat_id, *result)
	else:
		save(await msg.reply_text("No major news on your holdings in the past 24 hours."))


def is_movers_list(msg: Message) -> bool:
	"""A big-movers-at-close list from one of MOVERS_BOTS: a bold header like
	"≥ 5.0% at close (ASX):" followed by stocks with % changes."""
	u = msg.from_user
	if not (u and u.is_bot and (u.username or "").lower() in MOVERS_BOTS):
		return False
	if msg.text:
		bold = msg.parse_entities([MessageEntity.BOLD])
	else:
		bold = msg.parse_caption_entities([MessageEntity.BOLD])
	if not any(MOVERS_HEADER.search(t) for t in bold.values()):
		return False
	# The header has one percentage; a list needs at least one more.
	return len(PERCENT.findall(msg.text or msg.caption or "")) >= 2


def untag(text: str) -> str:
	"""Turn @mentions of MOVERS_BOTS into plain names, so replies never tag them."""
	for name in MOVERS_BOTS:
		text = re.sub(rf"@({re.escape(name)})\b", r"\1", text, flags=re.IGNORECASE)
	return text


def mentions(text: str, username: str | None) -> bool:
	"""Whether text @mentions this bot: @stockbot2 is a different bot from @stockbot."""
	return bool(username and re.search(rf"(?<![\w@])@{re.escape(username)}(?!\w)", text, re.I))


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
	msg = update.effective_message
	if msg is None:
		return
	if HOLDING_NEWS:
		await asyncio.to_thread(remember_user, msg)	# username -> ID, for holding-news DMs

	await asyncio.to_thread(save, msg)  # log everything, incl. edits (they overwrite the original)
	if update.edited_message:
		return	# don't answer edited messages
	# Other bots are logged but never answered (avoids bot-to-bot loops),
	# except for movers lists, which are answered once and never tag the bot.
	movers = is_movers_list(msg)
	if msg.from_user and msg.from_user.is_bot and not movers:
		return

	bot = context.bot
	private = msg.chat.type == "private"
	text = msg.text or msg.caption or ""
	mentioned = private or mentions(text, bot.username)

	command = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
	if private and command == "/start":
		sent = await msg.reply_text(
			"Hi! Ask me anything" + (", or tap a button below." if DM_BUTTONS else "."),
			reply_markup=DM_KEYBOARD if DM_BUTTONS else None,
		)
		save(sent)
		return
	if private and command == "/holdingnews" and HOLDING_NEWS:
		await holding_news_on_demand(bot, msg)
		return
	reply_target = msg.reply_to_message
	replied_to_bot = bool(
		reply_target and reply_target.from_user and reply_target.from_user.id == bot.id
	)
	if not (movers or mentioned or replied_to_bot):
		return

	user_id = msg.from_user.id if msg.from_user else None
	log.info(
		"%s in chat %s (%s) by %s [user id %s]",
		"Movers list" if movers else "Triggered",
		msg.chat_id, msg.chat.title, sender_name(msg), user_id,
	)

	# Show we're working straight away, before building the prompt or calling Grok.
	# Private chats stream into a draft (plus typing); groups get the typing indicator.
	draft = Draft(bot, msg.chat_id) if private else None
	if draft:
		await draft.start()
	else:
		typing = await start_typing(bot, msg.chat_id)
	try:
		preset = DM_PRESETS.get(text.strip()) if private and DM_BUTTONS else None
		if preset:
			# Preset buttons are standalone requests: no chat transcript, which
			# would only add tokens (and old answers for Grok to copy).
			prompt = (
				f"This is a private chat with {sender_name(msg)}. They tapped the "
				f"\"{text.strip()}\" button, which asks for: {preset}. It's now {now_str()}. "
				f"Search {SEARCH_WHAT} for current information, and do the whole job before replying."
			)
		else:
			# Must match what sender_name() produces for this bot's own messages.
			self_name = f"{bot.first_name} (@{bot.username})"
			history = await asyncio.to_thread(recent_history, msg.chat_id, HISTORY_LIMIT)
			transcript = "\n".join(format_row(r, self_name) for r in history)
			if private:
				prompt = (
					f"This is a private one-to-one chat with {sender_name(msg)}, not the group. "
					f"Chat transcript:\n{transcript}"
				)
			else:
				prompt = f"Group chat transcript:\n{transcript}"

			# Include the replied-to message explicitly: it may be older than the
			# history window, or from before the bot joined.
			if reply_target:
				who = "You" if replied_to_bot else sender_name(reply_target)
				prompt += (
					f"\n\nThe last message (#{msg.message_id}) is a reply to this message:\n"
					f"[#{reply_target.message_id}] {who}: {describe(reply_target)}"
				)
			if movers:
				# Same as someone replying "explain" to the list, which works better
				# than a detailed instruction.
				listing = msg.text_html if msg.text else (msg.caption_html or "")	# caption only, no [photo] tag
				prompt += f"\n\n{listing}\n\nexplain. one blank line after each news item and nothing else"
			prompt += (
				f"\n\nIt's now {now_str()}. "
				f"Respond to the last message (#{msg.message_id}). Do the whole job with your "
				"tools before replying: don't do part of it and offer to do the rest, and don't ask "
				"whether to continue. Don't imitate earlier replies of yours that did either. "
				"Fetch fresh data rather than reusing numbers from earlier replies, and follow the "
				"formatting rules in your instructions (e.g. Name (linked bold TICKER) metric for "
				"stock lists) even where your earlier replies didn't."
			)

		content: list[dict] = [{"type": "input_text", "text": prompt}]
		# Movers lists are explained from their caption alone; finbot's image adds
		# nothing Grok needs and costs a lot of tokens.
		image_sources = () if movers else (reply_target, msg)
		# replied-to photo first, then the mention itself; downloaded concurrently
		file_ids = [fid for m in image_sources if m and (fid := photo_file_id(m))]
		images = await asyncio.gather(*(image_part(bot, f) for f in file_ids[:MAX_IMAGES]),
									  return_exceptions=True)
		for img in images:
			if isinstance(img, BaseException):
				log.error("Couldn't download image", exc_info=img)
			else:
				content.append(img)
		if len(content) > 1:
			log.info("Attached %d image(s)", len(content) - 1)
		raw = await ask_llm(
			content, user_id, bot.first_name,
			on_text=draft.update if draft else None,
			must_search=movers or bool(preset),
			cache_key=f"chat-{msg.chat_id}",
		)
		answer = md_to_html(untag(strip_disclaimer(raw)))
		if movers:
			answer = strip_summary(answer)
		answer = auto_mark_tickers(answer) or "(no response)"
	except Exception as e:
		log.exception("Reply failed")
		answer = (
			"Sorry, something went wrong:\n"
			f"<pre>{html.escape(f'{type(e).__name__}: {e}'[:1500])}</pre>"
		)
	finally:
		if draft:
			draft.stop()
		else:
			typing.cancel()

	chunks = split_html(answer)
	for i, chunk in enumerate(chunks):
		buttons = DM_KEYBOARD if private and DM_BUTTONS and i == len(chunks) - 1 else None
		sent = await send_formatted(msg, chunk, quote=not private, reply_markup=buttons)
		await asyncio.to_thread(save, sent)  # bots don't receive their own messages, so log manually

# Otherwise a list of Yahoo-linked tickers gets a big Yahoo preview card under it.
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


async def send_formatted(msg: Message, text: str, quote: bool = True, reply_markup=None) -> Message:
	"""Send as Telegram HTML; if Telegram rejects the markup, send plain text."""
	try:
		return await msg.reply_text(
			text, parse_mode=ParseMode.HTML, do_quote=quote, disable_notification=True,
			link_preview_options=NO_PREVIEW, reply_markup=reply_markup,
		)
	except BadRequest as e:
		if "parse entities" not in str(e).lower():
			raise
		log.warning("Bad HTML from Grok, sending as plain text: %s", e)
		plain = re.sub(r'<a href="([^"]*)">(.*?)</a>', r"\2 (\1)", text)
		plain = html.unescape(re.sub(r"<[^>]+>", "", plain))
		return await msg.reply_text(
			plain, do_quote=quote, disable_notification=True, link_preview_options=NO_PREVIEW,
			reply_markup=reply_markup,
		)

async def log_stopped_generation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
	"""Log it if a user presses Telegram's stop button on a draft. Not acted on:
	the reply still finishes and is sent."""
	stopped = getattr(update, "stopped_message_generation", None) or (
		(update.api_kwargs or {}).get("stopped_message_generation")
	)
	if stopped:
		log.info("User stopped message generation (ignored): %s", stopped)


async def credits(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
	"""/credits - owner only (OWNER_USER_ID), OpenRouter backend only: credit left."""
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
	await update.effective_message.reply_text(text, disable_notification=True)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
	if isinstance(context.error, NetworkError):
		log.warning("Telegram network hiccup (retrying automatically): %s", context.error)
		return
	log.error("Unhandled error", exc_info=context.error)

BACKGROUND: list[asyncio.Task] = []


async def post_init(app: Application) -> None:
	if MCP_SERVERS:
		await asyncio.gather(*(s.start(app.bot) for s in MCP_SERVERS.values()))
		schema = json.dumps([t for s in MCP_SERVERS.values() for t in s.tools])
		log.info("MCP tool schemas: ~%d tokens, re-sent every round", len(schema) // 4)
	if HOLDING_NEWS:
		BACKGROUND.append(asyncio.create_task(holding_news_loop(app.bot)))

async def post_shutdown(app: Application) -> None:
	for task in BACKGROUND:
		task.cancel()
	await asyncio.gather(*(s.stop() for s in MCP_SERVERS.values()), return_exceptions=True)

def main() -> None:
	# The label does nothing: it's only there to tell instances apart in ps/top when
	# several run side by side with different environments (python bot.py mimo).
	parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
	parser.add_argument("label", nargs="?", help="ignored; identifies this instance in ps/top")
	args, _ = parser.parse_known_args()
	app = (
		Application.builder()
		.token(TELEGRAM_TOKEN)
		.concurrent_updates(True)
		.post_init(post_init)
		.post_shutdown(post_shutdown)
		.build()
	)
	if BACKEND == "openrouter" and OWNER_ID:	# must come before the catch-all handler
		app.add_handler(CommandHandler("credits", credits))
	app.add_handler(
		MessageHandler(
			(filters.ChatType.GROUPS | filters.ChatType.PRIVATE)
			& (filters.UpdateType.MESSAGE | filters.UpdateType.EDITED_MESSAGE)
			& ~filters.StatusUpdate.ALL,
			on_message,
		)
	)
	updates = ["message", "edited_message"]
	if LOG_STOPPED_GENERATION:
		app.add_handler(TypeHandler(Update, log_stopped_generation), group=-1)
		updates.append("stopped_message_generation")
	if HOLDING_NEWS:	# the Unsubscribe / Undo buttons on holding-news messages
		app.add_handler(CallbackQueryHandler(on_holding_news_button, pattern=r"^hn:"))
		updates.append("callback_query")
	app.add_error_handler(on_error)
	log.info("Starting%s: %s on %s, reasoning %s, MCP servers: %s",
			 f" instance {args.label}" if args.label else "", MODEL, BACKEND,
			 REASONING or "default", ", ".join(MCP_SERVERS) or "none")
	app.run_polling(allowed_updates=updates)

if __name__ == "__main__":
	main()
