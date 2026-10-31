"""Wiring: settings -> context -> Telegram application, and the startup/shutdown hooks."""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import logging
import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path

from telegram import (
    BotCommand,
    BotCommandScopeAllChatAdministrators,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import NetworkError, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import config
from .context import Ctx
from .features import chat_commands, dm_buttons, holding_news, post
from .features.charts import buttons as chart_buttons
from .features.charts import pages as chart_pages
from .features.periods import backfill
from .features.periods.windows import describe_scope, flush_scope
from .llm.base import Backend
from .llm.openrouter import OpenRouterBackend
from .llm.xai import XaiBackend
from .mcp.server import MCPServer, Registry
from .store import Store
from .telegram import commands
from .telegram.handlers import Handlers

log = logging.getLogger("bot")


# The bot's own commands: a chat command in mcp_servers.json can't take one of these names.
BUILT_IN_COMMANDS = {"start", "tokens", "usage", "credits", "flushcache", "clear", "segments", "narrative", "events",
                     "holdingnews"}


def build_backend(st: config.Settings) -> Backend:
    return {"openrouter": OpenRouterBackend, "xai": XaiBackend}[st.provider](st)


def journal_priority(levelno: int) -> int:
    """The syslog priority journald should give a record: err 3, warning 4, info 6, debug 7 (crit 2)."""
    for level, priority in ((logging.CRITICAL, 2), (logging.ERROR, 3), (logging.WARNING, 4), (logging.INFO, 6)):
        if levelno >= level:
            return priority
    return 7


class JournalFormatter(logging.Formatter):
    """Starts every line with a `<N>` priority prefix (sd-daemon(3)): journald strips it and stores the
    entry at that priority, so journalctl colours it and `-p` filters it. Every line, because journald
    stores each line of a traceback as its own entry."""

    def format(self, record: logging.LogRecord) -> str:
        prefix = f"<{journal_priority(record.levelno)}>"
        return "\n".join(prefix + line for line in super().format(record).split("\n"))


def is_journal(stream) -> bool:
    """True when `stream` is systemd's journal stream: JOURNAL_STREAM names its device and inode, and is
    checked against the stream because a child started from a service inherits the variable."""
    try:
        dev, ino = (int(part) for part in os.environ.get("JOURNAL_STREAM", "").split(":"))
        st = os.fstat(stream.fileno())
    except (ValueError, OSError, AttributeError):
        return False
    return (st.st_dev, st.st_ino) == (dev, ino)


def _log_handler(stream) -> logging.Handler:
    handler = logging.StreamHandler(stream)
    if is_journal(stream):  # journald stamps the time, so the line doesn't
        handler.setFormatter(JournalFormatter("%(levelname)s %(message)s"))
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    handler.addFilter(_drop_protocol_line)
    return handler


def setup_logging() -> None:
    """The bot's own logger logs everything down to debug (per-call detail); `journalctl -p info` hides
    that. Libraries stay at info. Debug and info go to stdout, warnings and errors to stderr, so a log
    driver that only sees the stream (Docker's journald driver stores stderr as err) still tells them apart."""
    out, err = _log_handler(sys.stdout), _log_handler(sys.stderr)
    out.addFilter(lambda record: record.levelno < logging.WARNING)
    err.setLevel(logging.WARNING)
    logging.basicConfig(level=logging.INFO, handlers=[out, err])
    log.setLevel(logging.DEBUG)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _drop_protocol_line(record: logging.LogRecord) -> bool:
    """Library log lines that are noise here: the MCP library's own "Negotiated protocol version" line
    doesn't say which server it is about (the bot logs "MCP <name>: connected, protocol ..." instead), and
    a remote server's 30 s keepalive arrives as an "Unknown SSE event: ping" warning."""
    msg = record.getMessage()
    return not (msg.startswith("Negotiated protocol version") or msg == "Unknown SSE event: ping")


async def alert_down(ctx: Ctx, server: MCPServer) -> None:
    """Tell ADMIN_CHAT_IDS that an MCP server died (and log it, so the model sees it in history)."""
    if not ctx.bot:
        return
    text = (f"⚠️ The <b>{html.escape(server.label)}</b> data source is down. "
            f"I can't use it until the bot is restarted.\n"
            f"<pre>{html.escape(server.error or 'unknown error')}</pre>")
    for chat_id in ctx.st.admin_chats:
        try:
            sent = await ctx.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML,
                                              disable_notification=True)
            await asyncio.to_thread(ctx.store.save, sent)
        except Exception:
            log.exception("Couldn't send MCP alert to chat %s", chat_id)


async def log_tool_size(ctx: Ctx) -> None:
    """Once the servers are up, log how many tokens their tool definitions cost every round."""
    await asyncio.gather(*(s.wait_ready() for s in ctx.registry.servers.values()))
    schema = json.dumps([vars(t) for s in ctx.registry.up() for t in s.tools])
    log.info("MCP tool schemas: ~%d tokens (re-sent every round they're offered)", len(schema) // 4)


async def register_menu(ctx: Ctx) -> None:
    """The command menu. A scoped menu replaces the default one, so each lists everything for its scope: /segments,
    /narrative, /events and the chat commands of mcp_servers.json for everyone; /clear too in private chats; /tokens too for the people who can use it
    (administrators of every group, and the users in ADMIN_CHAT_IDS in their own chat with the bot)."""
    everyone = [BotCommand("segments", "Revenue breakdown pies: /segments TICKER"),
                BotCommand("narrative", "10-K/10-Q narrative: /narrative TICKER [section]"),
                BotCommand("events", "8-K events: /events TICKER [section]")]
    everyone += [BotCommand(c.name, c.description) for c in ctx.chat_commands]  # from mcp_servers.json
    private = everyone + [BotCommand("clear", "Forget our earlier messages")]
    tokens = BotCommand("tokens", "Provider balance, token use and cost")
    try:
        await ctx.bot.set_my_commands(everyone)
        await ctx.bot.set_my_commands(private, scope=BotCommandScopeAllPrivateChats())
        await ctx.bot.set_my_commands(everyone + [tokens], scope=BotCommandScopeAllChatAdministrators())
        for chat_id in ctx.st.admin_chats:
            await ctx.bot.set_my_commands(private + [tokens], scope=BotCommandScopeChat(chat_id))
    except TelegramError as e:
        log.warning("Couldn't register the command menu: %s", e)


def build_ctx(st: config.Settings) -> Ctx:
    ctx = Ctx(st, Store(st.db_path), build_backend(st), Registry([]))

    async def on_down(server: MCPServer) -> None:
        await alert_down(ctx, server)

    ctx.registry = Registry.load(str(config.MCP_CONFIG), timeout=config.MCP_TIMEOUT,
                                 max_output=config.MAX_TOOL_OUTPUT, on_down=on_down,
                                 log_width=st.log_width)
    ctx.chat_commands = chat_commands.load({label: s.cfg for label, s in ctx.registry.servers.items()},
                                           BUILT_IN_COMMANDS)
    return ctx


def build_app(ctx: Ctx) -> Application:
    st = ctx.st
    background: list[asyncio.Task] = []

    async def post_init(app: Application) -> None:
        ctx.bot = app.bot
        ctx.store.bot_id = app.bot.id
        await register_menu(ctx)
        ctx.registry.start()  # connects in the background; requests see whichever servers are up
        background.append(asyncio.create_task(log_tool_size(ctx)))
        if st.holding_news:
            background.append(asyncio.create_task(holding_news.daily_loop(ctx)))
        if st.dm_buttons:
            background.append(asyncio.create_task(dm_buttons.push_on_startup(ctx)))
        if st.period_backfill:
            background.append(asyncio.create_task(backfill.loop(ctx)))

    async def post_shutdown(app: Application) -> None:
        for task in background:
            task.cancel()
        await ctx.registry.stop()

    async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        if isinstance(context.error, NetworkError):
            log.warning("Telegram network hiccup (retrying automatically): %s", context.error)
            return
        log.error("Unhandled error", exc_info=context.error)

    app = (Application.builder().token(st.telegram_token).concurrent_updates(True)
           .post_init(post_init).post_shutdown(post_shutdown).build())
    handlers = Handlers(ctx)

    def command(fn):
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            await fn(ctx, update, context)
        return wrapper

    # Commands first: the first matching handler wins, and the catch-all would swallow them.
    app.add_handler(CommandHandler(["tokens", "usage", "credits"], command(commands.credits)))
    app.add_handler(CommandHandler("flushcache", command(commands.flush_cache)))
    app.add_handler(CommandHandler("clear", command(commands.clear)))
    app.add_handler(CommandHandler("segments", handlers.segments))
    app.add_handler(CommandHandler("narrative", command(commands.narrative)))
    app.add_handler(CommandHandler("events", command(commands.events)))
    for c in ctx.chat_commands:  # chat_commands in mcp_servers.json
        app.add_handler(CommandHandler(c.name, handlers.chat_command(c)))
    app.add_handler(MessageHandler(
        (filters.ChatType.GROUPS | filters.ChatType.PRIVATE)
        & (filters.UpdateType.MESSAGE | filters.UpdateType.EDITED_MESSAGE)
        & ~filters.StatusUpdate.ALL, handlers.on_message))
    app.add_handler(CallbackQueryHandler(command(chart_buttons.on_button), pattern=r"^ch:"))  # chart periods
    app.add_handler(CallbackQueryHandler(command(chart_pages.on_page), pattern=r"^cp:"))  # which of several charts
    app.add_handler(CallbackQueryHandler(command(commands.on_filing_button), pattern=r"^fl:"))  # section buttons
    if st.holding_news:  # the Unsubscribe / Undo buttons on holding-news messages
        app.add_handler(CallbackQueryHandler(command(holding_news.on_button), pattern=r"^hn:"))
    if st.post_to_groups or st.admin_only:  # keeps the list of groups the bot is in
        app.add_handler(ChatMemberHandler(command(post.on_membership), ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_error_handler(on_error)
    return app


def allowed_updates(st: config.Settings) -> list[str]:
    return (["message", "edited_message", "callback_query"]
            + (["my_chat_member"] if st.post_to_groups or st.admin_only else []))


def git_hash(root: Path | None = None) -> str:
    """Short hash of the commit this checkout is on, or "" if it can't be read. Read from .git directly: the
    git command refuses a checkout owned by another user (a systemd DynamicUser runs as one)."""
    root = root or Path(__file__).resolve().parent.parent
    try:
        git = root / ".git"
        if git.is_file():  # a linked worktree: ".git" holds "gitdir: <path>"
            git = (root / git.read_text().split(":", 1)[1].strip()).resolve()
        common = git / (git / "commondir").read_text().strip() if (git / "commondir").exists() else git
        head = (git / "HEAD").read_text().strip()
        if head.startswith("ref:"):
            ref = head.split(None, 1)[1]
            try:
                head = (common / ref).read_text().strip()
            except FileNotFoundError:  # packed by `git gc`
                packed = (common / "packed-refs").read_text().splitlines()
                head = next((line.split()[0] for line in packed if line.endswith(" " + ref)), "")
        return head[:7] if re.fullmatch(r"[0-9a-f]{40,64}", head) else ""
    except (OSError, IndexError):
        return ""


def flush_cache(words: list[str]) -> str:
    """--flush-cache [portfolio ID] [year|month|day]: needs only the database path, not the bot's keys.
    Returns the message to print."""
    scope = flush_scope(words)
    if scope is None:
        return "Usage: --flush-cache [portfolio ID] [2024 | 2024-05 | 2024-05-17]"
    pid, first, last = scope
    path = config.database_path(os.environ)
    n = Store(path).period_flush(pid, first and first.isoformat(), last and last.isoformat())
    return f"Flushed {n} cached periods ({describe_scope(pid, first, last)})."


def main(argv: Sequence[str] | None = None) -> None:
    # The label does nothing: it only shows in ps/top, to tell instances apart when several run
    # side by side with different environments (python bot.py mimo).
    parser = argparse.ArgumentParser(description="Telegram group bot that answers @mentions with an LLM.")
    parser.add_argument("label", nargs="?", help="ignored; identifies this instance in ps/top")
    parser.add_argument("--flush-cache", nargs="*", metavar="WHAT",
                        help="delete the cached Sharesight period returns and exit: all, or of a portfolio ID "
                             "and/or a year, month or day (2024, 2024-05, 2024-05-17)")
    args, _ = parser.parse_known_args(argv)
    if args.flush_cache is not None:
        message = flush_cache(args.flush_cache)
        print(message)
        sys.exit(0 if message.startswith("Flushed") else 1)
    try:
        st = config.load()
    except config.ConfigError as e:
        sys.exit(str(e))
    setup_logging()
    ctx = build_ctx(st)
    app = build_app(ctx)
    commit = git_hash()
    log.info("Starting%s%s: %s on %s",
             f" instance {args.label}" if args.label else "", f" ({commit})" if commit else "", st.model, st.provider)
    log.info("Reasoning %s, MCP servers: %s", st.reasoning or "default", ", ".join(ctx.registry.servers) or "none")
    if st.period_backfill:
        log.info("Period backfill: on for %s", ", ".join(sorted(st.period_backfill)))
    app.run_polling(allowed_updates=allowed_updates(st))
