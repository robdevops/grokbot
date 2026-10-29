"""SQLite storage: chat history, users, holding-news state, key/value, usage ledger."""

from __future__ import annotations

import json
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
-- Charts sent with period buttons: the make_chart call to redraw from, the model's caption, the period shown.
CREATE TABLE IF NOT EXISTS charts (
    bot_id INTEGER, chat_id INTEGER, message_id INTEGER, spec TEXT, caption TEXT, period TEXT, ts INTEGER,
    PRIMARY KEY (bot_id, chat_id, message_id));
-- Chart images already on Telegram (uploaded to TELEGRAM_PRELOAD_CHAT, or sent as the reply): reused by the period buttons.
CREATE TABLE IF NOT EXISTS chart_files (
    bot_id INTEGER, chat_id INTEGER, message_id INTEGER, period TEXT, file_id TEXT, caption TEXT, ts INTEGER,
    PRIMARY KEY (bot_id, chat_id, message_id, period));
CREATE TABLE IF NOT EXISTS usage (
    ts INTEGER, bot_id INTEGER, chat_id INTEGER, model TEXT, kind TEXT, rounds INTEGER,
    tokens_in INTEGER, tokens_cached INTEGER, tokens_out INTEGER, cost REAL);
-- Finished-period Sharesight returns (sold holdings included), kept until flushed. pct NULL = no data
-- for that window. movers = the holdings that moved most, as JSON [[code, gain, gain_pct], ...].
CREATE TABLE IF NOT EXISTS period_cache (
    portfolio_id INTEGER, start TEXT, end TEXT, pct REAL, gain REAL, currency TEXT, movers TEXT,
    v INTEGER, PRIMARY KEY (portfolio_id, start, end)) WITHOUT ROWID;
"""
CHART_KEEP_DAYS = 90  # a chart's buttons stop working after this
PERIOD_CACHE_VERSION = 1  # bump when the stored shape changes: rows of another version are dropped


class PeriodRow(NamedTuple):
    """One cached window of a portfolio's return (start/end are ISO dates)."""
    portfolio_id: int
    start: str
    end: str
    pct: float | None
    gain: float | None
    currency: str
    movers: list


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
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=30, autocommit=True)
        self._lock = threading.RLock()
        self.bot_id: int | None = None  # set once the bot has started (DMs are stored per bot)
        self._run("PRAGMA journal_mode=WAL")
        self._run("PRAGMA synchronous=NORMAL")  # with WAL: no fsync per commit; a power cut may lose the last few
        with self._lock:
            self._conn.executescript(SCHEMA)
            # Drop "NOTHING" digests sent by an earlier version, so they aren't fed to the model.
            self._conn.execute(
                "DELETE FROM holding_news WHERE length(text) < 40 AND upper(text) LIKE '%NOTHING%'"
            )
            self._conn.execute("DELETE FROM period_cache WHERE v != ?", (PERIOD_CACHE_VERSION,))

    def _run(self, sql: str, params=()) -> list[tuple]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

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
                      (self._own_id(), *row))
        else:
            self._run("INSERT OR REPLACE INTO messages VALUES (?,?,?,?,?,?)", row)

    def remember_chat(self, chat_id: int, title: str) -> None:
        self._run("INSERT OR REPLACE INTO chats VALUES (?,?,?)", (chat_id, title, int(time.time())))

    def forget_chat(self, chat_id: int) -> None:
        self._run("DELETE FROM chats WHERE chat_id=?", (chat_id,))

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
            self._run("INSERT OR REPLACE INTO users VALUES (?, ?)", (u.username.lower(), u.id))

    def user_id_for(self, username: str) -> int | None:
        rows = self._run("SELECT user_id FROM users WHERE username = ?", (username.lower(),))
        return rows[0][0] if rows else None

    # -- key/value (per bot) -----------------------------------------------------------
    def kv_get(self, key: str) -> str | None:
        rows = self._run("SELECT value FROM kv WHERE bot_id = ? AND key = ?", (self._own_id(), key))
        return rows[0][0] if rows else None

    def kv_set(self, key: str, value: str) -> None:
        self._run("INSERT OR REPLACE INTO kv VALUES (?,?,?)", (self._own_id(), key, value))

    # -- charts with period buttons (per bot: DM message IDs are per bot) -----------------
    def chart_put(self, chat_id: int, message_id: int, spec: str, caption: str, period: str) -> None:
        now = int(time.time())
        self._run("INSERT OR REPLACE INTO charts VALUES (?,?,?,?,?,?,?)",
                  (self._own_id(), chat_id, message_id, spec, caption, period, now))
        self._run("DELETE FROM charts WHERE ts < ?", (now - CHART_KEEP_DAYS * 86400,))
        self._run("DELETE FROM chart_files WHERE ts < ?", (now - CHART_KEEP_DAYS * 86400,))

    def chart_file_put(self, chat_id: int, message_id: int, period: str, file_id: str, caption: str) -> None:
        self._run("INSERT OR REPLACE INTO chart_files VALUES (?,?,?,?,?,?,?)",
                  (self._own_id(), chat_id, message_id, period, file_id, caption, int(time.time())))

    def chart_file_get(self, chat_id: int, message_id: int, period: str) -> tuple[str, str, int] | None:
        rows = self._run("SELECT file_id, caption, ts FROM chart_files "
                         "WHERE bot_id = ? AND chat_id = ? AND message_id = ? AND period = ?",
                         (self._own_id(), chat_id, message_id, period))
        return rows[0] if rows else None

    def chart_get(self, chat_id: int, message_id: int) -> tuple[str, str, str] | None:
        rows = self._run("SELECT spec, caption, period FROM charts WHERE bot_id = ? AND chat_id = ? AND message_id = ?",
                         (self._own_id(), chat_id, message_id))
        return rows[0] if rows else None

    def chart_set_period(self, chat_id: int, message_id: int, period: str) -> None:
        self._run("UPDATE charts SET period = ? WHERE bot_id = ? AND chat_id = ? AND message_id = ?",
                  (period, self._own_id(), chat_id, message_id))

    # -- holding news ------------------------------------------------------------------
    def recent_news(self, username: str, since_ts: int) -> list[str]:
        rows = self._run("SELECT text FROM holding_news WHERE username = ? AND ts > ? ORDER BY ts",
                         (username, since_ts))
        return [r[0] for r in rows]

    def add_news(self, username: str, text: str) -> None:
        self._run("INSERT INTO holding_news VALUES (?,?,?)", (username, int(time.time()), text))

    def muted_codes(self, username: str) -> set[str]:
        rows = self._run("SELECT code FROM holding_news_mutes WHERE username = ?", (username,))
        return {r[0] for r in rows}

    def set_muted(self, username: str, code: str, muted: bool) -> None:
        if muted:
            self._run("INSERT OR IGNORE INTO holding_news_mutes VALUES (?,?)", (username, code))
        else:
            self._run("DELETE FROM holding_news_mutes WHERE username = ? AND code = ?",
                      (username, code))

    def remember_news_message(self, chat_id: int, message_id: int, codes: list[str]) -> None:
        self._run("INSERT OR REPLACE INTO holding_news_msgs VALUES (?,?,?)",
                  (chat_id, message_id, ",".join(codes)))

    def news_message_codes(self, chat_id: int, message_id: int) -> list[str]:
        rows = self._run("SELECT codes FROM holding_news_msgs WHERE chat_id = ? AND message_id = ?",
                         (chat_id, message_id))
        return [c for c in (rows[0][0] if rows else "").split(",") if c]

    # -- usage ledger ------------------------------------------------------------------
    def add_usage(self, chat_id: int, model: str, kind: str, rounds: int,
                  tokens_in: int, tokens_cached: int, tokens_out: int, cost: float) -> None:
        self._run("INSERT INTO usage VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (int(time.time()), self.bot_id or 0, chat_id, model, kind, rounds,
                   tokens_in, tokens_cached, tokens_out, cost))

    def usage_summary(self, since_ts: int) -> list[tuple]:
        """(model, requests, tokens_in, tokens_cached, tokens_out, cost) per model since a time."""
        return self._run(
            "SELECT model, COUNT(*), SUM(tokens_in), SUM(tokens_cached), SUM(tokens_out), SUM(cost) "
            "FROM usage WHERE bot_id = ? AND ts >= ? GROUP BY model ORDER BY SUM(cost) DESC",
            (self.bot_id or 0, since_ts))

    # -- Sharesight period cache -------------------------------------------------------
    def period_get(self, portfolio_id: int, first: str, last: str) -> dict[tuple[str, str], PeriodRow]:
        """Cached windows of a portfolio lying within first..last (ISO dates), by (start, end)."""
        rows = self._run("SELECT start, end, pct, gain, currency, movers FROM period_cache "
                         "WHERE portfolio_id = ? AND start >= ? AND end <= ?", (portfolio_id, first, last))
        return {(r[0], r[1]): PeriodRow(portfolio_id, r[0], r[1], r[2], r[3], r[4] or "", json.loads(r[5] or "[]"))
                for r in rows}

    def period_put(self, rows: list[PeriodRow]) -> None:
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO period_cache VALUES (?,?,?,?,?,?,?,?)",
                [(r.portfolio_id, r.start, r.end, r.pct, r.gain, r.currency, json.dumps(r.movers),
                  PERIOD_CACHE_VERSION) for r in rows])

    def period_count(self, portfolio_id: int, first: str, last: str) -> int:
        return self._run("SELECT COUNT(*) FROM period_cache WHERE portfolio_id = ? AND start >= ? AND end <= ?",
                         (portfolio_id, first, last))[0][0]

    def period_flush(self, portfolio_id: int | None = None, first: str | None = None, last: str | None = None) -> int:
        """Delete cached windows, of one portfolio or all, and (if first/last ISO dates are given) only those
        overlapping that date range. Returns how many went."""
        where, args = [], []
        if portfolio_id is not None:
            where.append("portfolio_id = ?")
            args.append(portfolio_id)
        if first and last:
            where.append("start <= ? AND end >= ?")
            args += [last, first]
        with self._lock:
            sql = "DELETE FROM period_cache" + (" WHERE " + " AND ".join(where) if where else "")
            return self._conn.execute(sql, args).rowcount
