"""SQLite storage: chat history, users, holding-news state, key/value, usage ledger."""

from __future__ import annotations

import sqlite3
import threading
import time
from typing import NamedTuple

from telegram import Message

from .msgtext import describe, sender_name

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    chat_id INTEGER, message_id INTEGER, sender TEXT, text TEXT, ts INTEGER, reply_to INTEGER,
    PRIMARY KEY (chat_id, message_id));
-- Private chats (chat_id > 0), keyed by bot. A group's message IDs are shared by every bot in it,
-- but each bot's DM with a person is its own ID sequence under the same chat ID (the person's
-- user ID), so bots sharing one file would collide in `messages`.
CREATE TABLE IF NOT EXISTS dm_messages (
    bot_id INTEGER, chat_id INTEGER, message_id INTEGER, sender TEXT, text TEXT, ts INTEGER,
    reply_to INTEGER, PRIMARY KEY (bot_id, chat_id, message_id));
CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY, user_id INTEGER);
CREATE TABLE IF NOT EXISTS holding_news (username TEXT, ts INTEGER, text TEXT);
CREATE TABLE IF NOT EXISTS holding_news_mutes (
    username TEXT, code TEXT, PRIMARY KEY (username, code));
CREATE TABLE IF NOT EXISTS holding_news_msgs (
    chat_id INTEGER, message_id INTEGER, codes TEXT, PRIMARY KEY (chat_id, message_id));
-- Groups the bot has seen (Telegram can't list a bot's chats), so "post in <name>" can find one.
CREATE TABLE IF NOT EXISTS chats (chat_id INTEGER PRIMARY KEY, title TEXT, ts INTEGER);
CREATE TABLE IF NOT EXISTS kv (bot_id INTEGER, key TEXT, value TEXT, PRIMARY KEY (bot_id, key));
CREATE TABLE IF NOT EXISTS usage (
    ts INTEGER, bot_id INTEGER, chat_id INTEGER, model TEXT, kind TEXT, rounds INTEGER,
    tokens_in INTEGER, tokens_cached INTEGER, tokens_out INTEGER, cost REAL);
"""


class HistoryRow(NamedTuple):
    message_id: int
    sender: str
    text: str
    ts: int
    reply_to: int | None


def is_dm(chat_id: int) -> bool:
    """Private chat IDs are the person's user ID (positive); groups and supergroups are negative."""
    return chat_id > 0


class Store:
    """One SQLite connection shared by the event loop and worker threads (call methods through
    asyncio.to_thread to keep a locked shared file from freezing the loop). Calls are
    serialised by a lock; results are fetched under it."""

    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self._lock = threading.RLock()
        self.bot_id: int | None = None  # set once the bot has started (DMs are stored per bot)
        self._run("PRAGMA journal_mode=WAL")
        with self._lock:
            self._conn.executescript(SCHEMA)
            # Drop "NOTHING" digests sent by an earlier version, so they aren't fed to the model.
            self._conn.execute(
                "DELETE FROM holding_news WHERE length(text) < 40 AND upper(text) LIKE '%NOTHING%'"
            )
            self._conn.commit()

    def _run(self, sql: str, params=(), commit: bool = False) -> list[tuple]:
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
            if commit:
                self._conn.commit()
            return rows

    def _own_id(self) -> int:
        if self.bot_id is None:
            raise RuntimeError("bot id not known yet (private chats are stored per bot)")
        return self.bot_id

    # -- chat history ------------------------------------------------------------------
    def save(self, msg: Message) -> None:
        row = (
            msg.chat_id, msg.message_id, sender_name(msg), describe(msg),
            int(msg.date.timestamp()),
            msg.reply_to_message.message_id if msg.reply_to_message else None,
        )
        if is_dm(msg.chat_id):
            self._run("INSERT OR REPLACE INTO dm_messages VALUES (?,?,?,?,?,?,?)",
                      (self._own_id(), *row), commit=True)
        else:
            self._run("INSERT OR REPLACE INTO messages VALUES (?,?,?,?,?,?)", row, commit=True)

    def remember_chat(self, chat_id: int, title: str) -> None:
        self._run("INSERT OR REPLACE INTO chats VALUES (?,?,?)", (chat_id, title, int(time.time())), commit=True)

    def forget_chat(self, chat_id: int) -> None:
        self._run("DELETE FROM chats WHERE chat_id=?", (chat_id,), commit=True)

    def chats(self) -> list[tuple[int, str]]:
        return [(r[0], r[1]) for r in self._run("SELECT chat_id, title FROM chats")]

    def history(self, chat_id: int, limit: int) -> list[HistoryRow]:
        """Recent messages, oldest first.

        A plain "last N" window drops its oldest line on every message, changing the start of
        the prompt and defeating prompt caching. Instead the window's start only moves every
        limit/2 messages: it holds between `limit` and 1.5 x `limit` messages and in between
        only grows at the end, so everything before the new messages stays cached."""
        if is_dm(chat_id):
            table, where, args = "dm_messages", "bot_id = ? AND chat_id = ?", (self._own_id(), chat_id)
        else:
            table, where, args = "messages", "chat_id = ?", (chat_id,)
        total = self._run(f"SELECT COUNT(*) FROM {table} WHERE {where}", args)[0][0]
        step = max(1, limit // 2)
        size = total - max(0, (total - limit) // step * step)
        rows = self._run(
            f"SELECT message_id, sender, text, ts, reply_to FROM {table} "
            f"WHERE {where} ORDER BY message_id DESC LIMIT ?", (*args, size))
        return [HistoryRow(*r) for r in reversed(rows)]

    def dm_chats(self) -> list[int]:
        """Everyone this bot has a private chat with."""
        rows = self._run("SELECT DISTINCT chat_id FROM dm_messages WHERE bot_id = ?", (self._own_id(),))
        return [r[0] for r in rows]

    # -- users -------------------------------------------------------------------------
    def remember_user(self, msg: Message) -> None:
        u = msg.from_user
        if u and u.username and not u.is_bot:
            self._run("INSERT OR REPLACE INTO users VALUES (?, ?)", (u.username.lower(), u.id),
                      commit=True)

    def user_id_for(self, username: str) -> int | None:
        rows = self._run("SELECT user_id FROM users WHERE username = ?", (username.lower(),))
        return rows[0][0] if rows else None

    # -- key/value (per bot) -----------------------------------------------------------
    def kv_get(self, key: str) -> str | None:
        rows = self._run("SELECT value FROM kv WHERE bot_id = ? AND key = ?", (self._own_id(), key))
        return rows[0][0] if rows else None

    def kv_set(self, key: str, value: str) -> None:
        self._run("INSERT OR REPLACE INTO kv VALUES (?,?,?)", (self._own_id(), key, value), commit=True)

    # -- holding news ------------------------------------------------------------------
    def recent_news(self, username: str, since_ts: int) -> list[str]:
        rows = self._run("SELECT text FROM holding_news WHERE username = ? AND ts > ? ORDER BY ts",
                         (username, since_ts))
        return [r[0] for r in rows]

    def add_news(self, username: str, text: str) -> None:
        self._run("INSERT INTO holding_news VALUES (?,?,?)", (username, int(time.time()), text),
                  commit=True)

    def muted_codes(self, username: str) -> set[str]:
        rows = self._run("SELECT code FROM holding_news_mutes WHERE username = ?", (username,))
        return {r[0] for r in rows}

    def set_muted(self, username: str, code: str, muted: bool) -> None:
        if muted:
            self._run("INSERT OR IGNORE INTO holding_news_mutes VALUES (?,?)", (username, code),
                      commit=True)
        else:
            self._run("DELETE FROM holding_news_mutes WHERE username = ? AND code = ?",
                      (username, code), commit=True)

    def remember_news_message(self, chat_id: int, message_id: int, codes: list[str]) -> None:
        self._run("INSERT OR REPLACE INTO holding_news_msgs VALUES (?,?,?)",
                  (chat_id, message_id, ",".join(codes)), commit=True)

    def news_message_codes(self, chat_id: int, message_id: int) -> list[str]:
        rows = self._run("SELECT codes FROM holding_news_msgs WHERE chat_id = ? AND message_id = ?",
                         (chat_id, message_id))
        return [c for c in (rows[0][0] if rows else "").split(",") if c]

    # -- usage ledger ------------------------------------------------------------------
    def add_usage(self, chat_id: int, model: str, kind: str, rounds: int,
                  tokens_in: int, tokens_cached: int, tokens_out: int, cost: float) -> None:
        self._run("INSERT INTO usage VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (int(time.time()), self.bot_id or 0, chat_id, model, kind, rounds,
                   tokens_in, tokens_cached, tokens_out, cost), commit=True)

    def usage_summary(self, since_ts: int) -> list[tuple]:
        """(model, requests, tokens_in, tokens_cached, tokens_out, cost) per model since a time."""
        return self._run(
            "SELECT model, COUNT(*), SUM(tokens_in), SUM(tokens_cached), SUM(tokens_out), SUM(cost) "
            "FROM usage WHERE bot_id = ? AND ts >= ? GROUP BY model ORDER BY SUM(cost) DESC",
            (self.bot_id or 0, since_ts))
