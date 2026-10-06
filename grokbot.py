#!/usr/bin/env python3
"""
Telegram group bot that answers @mentions using Grok (xAI), with the group's
recent chat history as context.

Every message the bot sees is logged to SQLite. When someone @mentions the bot
(or replies to one of its messages), the recent log is sent to Grok as a
transcript so it can answer questions like "what's with that last message?".

The database can be shared by several bots in the same group: each bot's
replies are stored under its real name and relabelled "You" only when that
bot builds its own transcript.

MCP servers listed in mcp_servers.json (e.g. Yahoo Finance, Sharesight) are
started by the bot itself and their tools are offered to Grok as function
tools. The bot runs the tool calls locally, so the servers never need to be
reachable from the internet and credentials stay on this machine.

Setup:
  1. Create a bot with @BotFather, then /setprivacy -> Disable.
	 (Remove and re-add the bot to any group it's already in.)
  2. pip install -r requirements.txt   (plus Node.js for npx-based MCP servers)
  3. export TELEGRAM_BOT_TOKEN=...	XAI_API_KEY=...
  4. Optional: edit mcp_servers.json
  5. python bot.py
"""

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
import tempfile
import time
from contextlib import AsyncExitStack
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client
from openai import AsyncOpenAI, BadRequestError
from telegram import (
	InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Message, MessageEntity,
	ReplyKeyboardMarkup, Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
	Application, CallbackQueryHandler, ContextTypes, MessageHandler, TypeHandler, filters,
)
from telegram.error import BadRequest, Forbidden, NetworkError

# ---------------------------------------------------------------- config ----

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
XAI_API_KEY = os.environ["XAI_API_KEY"]
MODEL = os.getenv("GROK_MODEL", "grok-4.7")
REASONING = os.getenv("GROK_REASONING", "").strip().lower()  # low / medium / high; empty = model default
MAX_IMAGES = int(os.getenv("MAX_IMAGES", "2"))
# Longer transcript lines (usually the bot's own earlier answers) are cut to this.
TRANSCRIPT_LINE_MAX = int(os.getenv("TRANSCRIPT_LINE_MAX", "400"))
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "20"))  # messages of context (window is 20-29, see recent_history)
DB_PATH = os.getenv("DB_PATH", "chat_log.db")
TZ = ZoneInfo(os.getenv("BOT_TZ", "UTC"))  # e.g. "Europe/London"
# Grok's server-side search tools. Set SEARCH_TOOLS="" to disable.
SEARCH_TOOLS = [
	{"type": t} for t in os.getenv("SEARCH_TOOLS", "web_search,x_search").replace(",", " ").split()
]

# MCP servers (see mcp_servers.json). Missing file = no MCP tools.
MCP_CONFIG = os.getenv("MCP_CONFIG", "mcp_servers.json")
MCP_TIMEOUT = int(os.getenv("MCP_TIMEOUT", "60"))  # seconds per tool call
MAX_TOOL_ROUNDS = int(os.getenv("MAX_TOOL_ROUNDS", "6"))  # Grok <-> tools round trips per answer
MAX_TOOL_OUTPUT = int(os.getenv("MAX_TOOL_OUTPUT", "50000"))  # chars per tool result sent to Grok
# Chats that get a message when an MCP server goes down. Empty = no alerts.
ALERT_CHATS = {
	int(x) for x in os.getenv("ALERT_CHAT_IDS", "").replace(",", " ").split()
}

# Bots whose end-of-day "big movers" lists get an automatic news explanation
# (usernames, comma-separated; set MOVERS_BOTS="" to turn this off).
MOVERS_BOTS = {
	u.lower().lstrip("@") for u in os.getenv("MOVERS_BOTS", "finbotibot").replace(",", " ").split()
}
# The bold header, e.g. "≥ 5.0% at close (ASX):" or "≤ -5% at close (NASDAQ, NYSE):"
MOVERS_HEADER = re.compile(r"(?:≥|≤|>=?|<=?)\s*[-−]?\s*\d+(?:\.\d+)?\s*%.*\bclose\b", re.IGNORECASE)
PERCENT = re.compile(r"\d+(?:\.\d+)?\s*%")


def _flag(name: str) -> bool:
	return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


# Preset-prompt buttons under the message box in private chats (off by default).
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

# Daily check for major news about Sharesight holdings (off by default).
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

MAX_TG_MESSAGE = 4096

# Under systemd, journald already stamps every line with the time, so don't repeat it.
logging.basicConfig(
	level=logging.INFO,
	format="%(levelname)s %(message)s" if os.getenv("JOURNAL_STREAM")
	else "%(asctime)s %(levelname)s %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("grokbot")

# Searches (especially X search) can take a while, so allow a generous timeout.
grok = AsyncOpenAI(api_key=XAI_API_KEY, base_url="https://api.x.ai/v1", timeout=180)

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

You can search the web and X (Twitter). Use them whenever a question involves news, prices,
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
db = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
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


def split_message(text: str, size: int = MAX_TG_MESSAGE) -> list[str]:
	chunks = []
	while len(text) > size:
		cut = text.rfind("\n", 0, size)
		cut = cut if cut > 0 else size
		chunks.append(text[:cut])
		text = text[cut:].lstrip()
	return chunks + [text] if text else chunks


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
				save(sent)  # so it shows up in the transcript Grok sees
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
			self.tools.append({
				"type": "function",
				"name": fn,
				"description": compact_description(t.description or ""),
				"parameters": compact_schema(t.inputSchema or {"type": "object", "properties": {}}),
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


async def grok_create(on_text=None, **kwargs):
	"""One Responses API call. With on_text, stream it, calling on_text(text so far)
	as text arrives, and return the final response object."""
	if on_text is None:
		return await grok.responses.create(**kwargs)
	stream = await grok.responses.create(stream=True, **kwargs)
	text, final = "", None
	async for event in stream:
		if event.type == "response.output_text.delta":
			text += event.delta
			on_text(text)
		elif event.type in ("response.completed", "response.incomplete", "response.failed"):
			final = event.response
	if final is None:
		raise RuntimeError("Grok's stream ended without a final response")
	if final.status == "failed":
		raise RuntimeError(f"Grok response failed: {final.error}")
	return final


async def ask_grok(
	content: list[dict], user_id: int | None, bot_name: str, on_text=None,
	must_search: bool = False, cache_key: str | None = None,
) -> str:
	"""Ask Grok, running any MCP tool calls it makes, until it produces an answer.
	With on_text, responses are streamed and on_text gets the text so far.
	With must_search, Grok only gets web/X search and has to use it at least once."""
	if must_search and SEARCH_TOOLS:
		servers, down = [], []
		tools = list(SEARCH_TOOLS)
	else:
		must_search = False
		servers = [s for s in MCP_SERVERS.values() if s.session and s.permits(user_id)]
		down = [s for s in MCP_SERVERS.values() if s.error and s.permits(user_id)]
		tools = SEARCH_TOOLS + [t for s in servers for t in s.tools]
	system = SYSTEM_PROMPT.format(bot_name=bot_name) + tools_prompt(servers, down)
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
			resp = await grok_create(on_text, input=first_input, tool_choice="required", **kwargs)
		except BadRequestError as e:
			log.warning("xAI rejected tool_choice=required (%s); retrying without it", e)
			resp = await grok_create(on_text, input=first_input, **kwargs)
	else:
		resp = await grok_create(on_text, input=first_input, **kwargs)
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
				resp = await grok_create(on_text, input=conversation, **kwargs, **extra)
			except BadRequestError as e:
				log.warning("xAI rejected the replayed conversation (%s); "
							"using previous_response_id from now on", e)
				STATELESS_ROUNDS = False
		if not STATELESS_ROUNDS:
			resp = await grok_create(
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
		plain = html.unescape(TG_TAG_RE.sub("", self.text)).strip()
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

Search the web and X for MAJOR news about these companies published in the past 24 hours.
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
		now=now_str(),
		holdings="\n".join(f"- {k}: {v}" for k, v in sorted(holdings.items())),
		already=already,
	)
	raw = strip_disclaimer(await ask_grok(
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
	chunks = split_message(f"📰 <b>Holding news</b> (past 24h)\n\n{news}")
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
	remember_user(msg)	# username -> ID, for holding-news DMs

	save(msg)  # log everything, including edits (they overwrite the original)
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
				"Search the web and X for current information, and do the whole job before replying."
			)
		else:
			# Must match what sender_name() produces for this bot's own messages.
			self_name = f"{bot.first_name} (@{bot.username})"
			transcript = "\n".join(
				format_row(r, self_name) for r in recent_history(msg.chat_id, HISTORY_LIMIT)
			)
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
				f"\n\nRespond to the last message (#{msg.message_id}). Do the whole job with your "
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
		for candidate in image_sources:  # replied-to photo first, then the mention itself
			if candidate and len(content) <= MAX_IMAGES:
				fid = photo_file_id(candidate)
				if fid:
					try:
						content.append(await image_part(bot, fid))
					except Exception:
						log.exception("Couldn't download image")
		if len(content) > 1:
			log.info("Attached %d image(s)", len(content) - 1)
		raw = await ask_grok(
			content, user_id, bot.first_name,
			on_text=draft.update if draft else None,
			must_search=movers or bool(preset),
			cache_key=f"chat-{msg.chat_id}",
		)
		answer = md_to_html(untag(strip_disclaimer(raw)))
		if movers:
			answer = strip_summary(answer)
		answer = answer or "(no response)"
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

	chunks = split_message(answer)
	for i, chunk in enumerate(chunks):
		buttons = DM_KEYBOARD if private and DM_BUTTONS and i == len(chunks) - 1 else None
		sent = await send_formatted(msg, chunk, quote=not private, reply_markup=buttons)
		save(sent)  # bots don't receive their own messages, so log manually

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


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
	if isinstance(context.error, NetworkError):
		log.warning("Telegram network hiccup (retrying automatically): %s", context.error)
		return
	log.error("Unhandled error", exc_info=context.error)

BACKGROUND: list[asyncio.Task] = []


async def post_init(app: Application) -> None:
	if MCP_SERVERS:
		await asyncio.gather(*(s.start(app.bot) for s in MCP_SERVERS.values()))
	if HOLDING_NEWS:
		BACKGROUND.append(asyncio.create_task(holding_news_loop(app.bot)))

async def post_shutdown(app: Application) -> None:
	for task in BACKGROUND:
		task.cancel()
	await asyncio.gather(*(s.stop() for s in MCP_SERVERS.values()), return_exceptions=True)

def main() -> None:
	app = (
		Application.builder()
		.token(TELEGRAM_TOKEN)
		.concurrent_updates(True)
		.post_init(post_init)
		.post_shutdown(post_shutdown)
		.build()
	)
	app.add_handler(
		MessageHandler(
			(filters.ChatType.GROUPS | filters.ChatType.PRIVATE)
			& (filters.UpdateType.MESSAGE | filters.UpdateType.EDITED_MESSAGE)
			& ~filters.StatusUpdate.ALL,
			on_message,
		)
	)
	app.add_handler(TypeHandler(Update, log_stopped_generation), group=-1)
	app.add_handler(CallbackQueryHandler(on_holding_news_button, pattern=r"^hn:"))
	app.add_error_handler(on_error)
	log.info("Starting bot with model %s; MCP servers: %s", MODEL, ", ".join(MCP_SERVERS) or "none")
	app.run_polling(allowed_updates=["message", "edited_message", "callback_query", "stopped_message_generation"])

if __name__ == "__main__":
	main()
