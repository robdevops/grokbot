"""Wiring: settings -> context -> Telegram application, and the startup/shutdown hooks."""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import logging
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import NetworkError
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
from .features import dm_buttons, holding_news, post
from .llm.base import Backend
from .llm.openrouter import OpenRouterBackend
from .llm.xai import XaiBackend
from .mcp.server import MCPServer, Registry
from .store import Store
from .telegram import commands
from .telegram.handlers import Handlers

log = logging.getLogger("bot")


def build_backend(st: config.Settings) -> Backend:
    return OpenRouterBackend(st) if st.provider == "openrouter" else XaiBackend(st)


def setup_logging() -> None:
    # Under systemd, journald already stamps every line with the time, so don't repeat it.
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(message)s" if os.getenv("JOURNAL_STREAM")
        else "%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)


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


def build_ctx(st: config.Settings) -> Ctx:
    ctx = Ctx(st, Store(st.db_path), build_backend(st), Registry([]))

    async def on_down(server: MCPServer) -> None:
        await alert_down(ctx, server)

    ctx.registry = Registry.load(st.mcp_config, timeout=config.MCP_TIMEOUT,
                                 max_output=st.max_tool_output, on_down=on_down)
    return ctx


def build_app(ctx: Ctx) -> Application:
    st = ctx.st
    background: list[asyncio.Task] = []

    async def post_init(app: Application) -> None:
        ctx.bot = app.bot
        ctx.store.bot_id = app.bot.id
        ctx.registry.start()  # connects in the background; requests see whichever servers are up
        background.append(asyncio.create_task(log_tool_size(ctx)))
        if st.holding_news:
            background.append(asyncio.create_task(holding_news.daily_loop(ctx)))
        if st.dm_buttons:
            background.append(asyncio.create_task(dm_buttons.push_on_startup(ctx)))

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
    if st.owner_id:
        app.add_handler(CommandHandler("credits", command(commands.credits)))
        app.add_handler(CommandHandler("usage", command(commands.usage)))
    app.add_handler(MessageHandler(
        (filters.ChatType.GROUPS | filters.ChatType.PRIVATE)
        & (filters.UpdateType.MESSAGE | filters.UpdateType.EDITED_MESSAGE)
        & ~filters.StatusUpdate.ALL, handlers.on_message))
    if st.holding_news:  # the Unsubscribe / Undo buttons on holding-news messages
        app.add_handler(CallbackQueryHandler(command(holding_news.on_button), pattern=r"^hn:"))
    if st.post_to_groups:  # keeps the list of groups the bot can post in
        app.add_handler(ChatMemberHandler(command(post.on_membership), ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_error_handler(on_error)
    return app


def allowed_updates(st: config.Settings) -> list[str]:
    return (["message", "edited_message"] + (["callback_query"] if st.holding_news else [])
            + (["my_chat_member"] if st.post_to_groups else []))


def git_hash() -> str:
    """Short hash of the commit this checkout is on, or "" if it can't be read (no git, no checkout)."""
    try:
        done = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=Path(__file__).resolve().parent.parent,
                              capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def main(argv: Sequence[str] | None = None) -> None:
    # The label does nothing: it only shows in ps/top, to tell instances apart when several run
    # side by side with different environments (python bot.py mimo).
    parser = argparse.ArgumentParser(description="Telegram group bot that answers @mentions with an LLM.")
    parser.add_argument("label", nargs="?", help="ignored; identifies this instance in ps/top")
    args, _ = parser.parse_known_args(argv)
    try:
        st = config.load()
    except config.ConfigError as e:
        sys.exit(str(e))
    setup_logging()
    ctx = build_ctx(st)
    app = build_app(ctx)
    commit = git_hash()
    log.info("Starting%s%s: %s on %s, reasoning %s, MCP servers: %s",
             f" instance {args.label}" if args.label else "", f" ({commit})" if commit else "", st.model, st.provider,
             st.reasoning or "default", ", ".join(ctx.registry.servers) or "none")
    app.run_polling(allowed_updates=allowed_updates(st))
