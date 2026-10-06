"""SHARESIGHT_HOLDING_NEWS_RECIPIENTS: a daily DM about major news on each person's Sharesight holdings,
with per-ticker mute/undo buttons, plus the /holdingnews on-demand check."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from datetime import datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.error import Forbidden
from telegram.ext import ContextTypes

from ..ask import ask
from ..context import Ctx
from ..llm.gate import Route
from ..mcp.results import table_records
from ..prompts import HOLDING_NEWS_PROMPT
from ..telegram.draft import Draft
from ..telegram.send import send_html
from ..textfmt import ANY_TAG_RE, md_to_html, split_html, strip_disclaimer
from ..tickers import link_tickers

log = logging.getLogger("bot")
UNSUBSCRIBE_BUTTON = InlineKeyboardMarkup([[InlineKeyboardButton("🔕 Unsubscribe…", callback_data="hn:menu")]])


def is_nothing(raw: str) -> bool:
    """True for the model's "nothing to report" answer, however it's dressed up ("NOTHING",
    "• NOTHING", "**Nothing.**", "None") or an empty reply."""
    plain = html.unescape(ANY_TAG_RE.sub("", raw or ""))
    words = re.sub(r"[^A-Za-z ]", " ", plain).split()
    # A real item is longer, even one about a company called "Nothing ...".
    return not words or (words[0].upper() in ("NOTHING", "NONE") and len(words) <= 5)


def as_bullets(text: str) -> str:
    """One "• " bullet per non-empty line, whatever bullet style (if any) the model used."""
    lines = (re.sub(r"^\s*(?:[•\-*–·]|\d+[.)])\s+", "", ln).strip() for ln in text.splitlines())
    return "\n".join(f"• {ln}" for ln in lines if ln)


def news_codes(news: str, holdings: dict[str, str]) -> list[str]:
    """Holding codes that appear as bold tickers or Yahoo links in a news message."""
    held = {k.split(" ")[0].upper() for k in holdings}
    found = {m.upper() for m in re.findall(r"<b>([^<]{1,12})</b>", news)}
    found |= {m.split(".")[0].upper()
              for m in re.findall(r"finance\.yahoo\.com/quote/([\w.\-^]+)", news)}
    return sorted(held & found)


def unsubscribe_menu(codes: list[str]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(c, callback_data=f"hn:mute:{c}") for c in codes[i:i + 3]]
            for i in range(0, len(codes), 3)]
    rows.append([InlineKeyboardButton("All holding news", callback_data="hn:mute:*")])
    rows.append([InlineKeyboardButton("Cancel", callback_data="hn:cancel")])
    return InlineKeyboardMarkup(rows)


def undo_button(code: str) -> InlineKeyboardMarkup:
    what = "all holding news" if code == "*" else f"{code} news"
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"↩️ Undo (unsubscribed from {what})", callback_data=f"hn:undo:{code}")]])


async def current_holdings(server, portfolio_names: list[str]) -> dict[str, str]:
    """Current holdings across the named portfolios, as {"CODE (MARKET)": name}."""
    data = json.loads(await server.call("list_portfolios", {}))
    portfolios = {p["name"].lower(): p for p in data.get("portfolios", [])}
    holdings: dict[str, str] = {}
    for name in portfolio_names:
        p = portfolios.get(name)
        if not p:
            log.warning("Holding news: no Sharesight portfolio called %r (have: %s)",
                        name, ", ".join(sorted(portfolios)))
            continue
        report = json.loads(await server.call(
            "get_performance_report", {"portfolio_id": p["id"], "include_sales": False})).get("report") or {}
        for h in table_records(report.get("holdings")):
            if h.get("code"):
                holdings[f"{h['code']} ({h.get('market', '?')})"] = h.get("name") or h["code"]
    return holdings


async def holding_news_text(ctx: Ctx, username: str, holdings: dict[str, str]) -> str | None:
    """Ask the model for major news about these holdings; None if there's nothing."""
    since = int((datetime.now() - timedelta(days=3)).timestamp())
    recent = await asyncio.to_thread(ctx.store.recent_news, username, since)
    already = ("\nAlready reported in the last few days; don't repeat these unless there's a "
               "genuinely new development:\n" + "\n".join(recent) + "\n") if recent else ""
    prompt = HOLDING_NEWS_PROMPT.format(
        now=ctx.now(), what=ctx.backend.search_what, already=already,
        holdings="\n".join(f"- {k}: {v}" for k, v in sorted(holdings.items())))
    answer = await ask(ctx, [{"type": "text", "text": prompt}], Route(), must_search=True,
                       cache_key=f"holding-news-{username}", kind="holding-news")
    raw = strip_disclaimer(answer.text)
    if is_nothing(raw):
        return None
    return link_tickers(as_bullets(md_to_html(raw)))


async def send_holding_news(ctx: Ctx, chat_id: int, news: str, codes: list[str]) -> None:
    """Send a holding-news message with its Unsubscribe button on the last part."""
    chunks = split_html(f"📰 <b>Holding news</b> (past 24h)\n\n{news}")
    for i, chunk in enumerate(chunks):
        last = i == len(chunks) - 1
        sent = await send_html(ctx.bot, chat_id, chunk, UNSUBSCRIBE_BUTTON if last else None)
        await asyncio.to_thread(ctx.store.save, sent)
        if last:
            await asyncio.to_thread(ctx.store.remember_news_message, chat_id, sent.message_id, codes)


async def run_holding_news(ctx: Ctx, only_username: str | None = None
                           ) -> dict[str, tuple[str, list[str]] | None]:
    """Check each recipient's holdings; DM anyone with major news.
    Returns {username: (news text, codes it covers) or None}."""
    server = ctx.registry.servers.get("sharesight")
    if not (server and server.session):
        log.warning("Holding news: Sharesight isn't connected, skipping")
        return {}
    people: dict[str, list[str]] = {}
    for portfolio, username in ctx.st.holding_news_recipients.items():
        if only_username is None or username == only_username:
            people.setdefault(username, []).append(portfolio)
    results: dict[str, tuple[str, list[str]] | None] = {}
    for username, portfolio_names in people.items():
        results.update(await _check_person(ctx, server, username, portfolio_names, only_username))
    return results


async def _check_person(ctx: Ctx, server, username: str, portfolio_names: list[str],
                        only_username: str | None) -> dict:
    muted = await asyncio.to_thread(ctx.store.muted_codes, username)
    if "*" in muted:
        log.info("Holding news: @%s has unsubscribed from all of it", username)
        return {}
    try:
        holdings = await current_holdings(server, portfolio_names)
        holdings = {k: v for k, v in holdings.items() if k.split(" ")[0].upper() not in muted}
        if not holdings:
            return {}
        news = await holding_news_text(ctx, username, holdings)
    except Exception:
        log.exception("Holding news check failed for @%s", username)
        return {}
    result = (news, news_codes(news, holdings)) if news else None
    log.info("Holding news for @%s: %s", username, "found" if news else "nothing major")
    if news and not only_username:  # on-demand checks are replied to by the caller
        await _deliver(ctx, username, news, result[1])
    return {username: result}


async def _deliver(ctx: Ctx, username: str, news: str, codes: list[str]) -> None:
    chat_id = await asyncio.to_thread(ctx.store.user_id_for, username)
    if not chat_id:
        log.warning("Holding news: don't know @%s's user ID yet; they need to message the bot "
                    "or a group it's in", username)
        return
    try:
        await send_holding_news(ctx, chat_id, news, codes)
        await asyncio.to_thread(ctx.store.add_news, username, news)
    except Forbidden:
        log.warning("Holding news: @%s hasn't started a private chat with the bot", username)


def seconds_until(hhmm: str, now: datetime) -> float:
    hour, minute = (int(x) for x in hhmm.split(":"))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def daily_loop(ctx: Ctx) -> None:
    while True:
        wait = seconds_until(ctx.st.holding_news_time, datetime.now(ctx.st.tz))
        log.info("Next holding news check in %.1f hours", wait / 3600)
        await asyncio.sleep(wait)
        try:
            await run_holding_news(ctx)
        except Exception:
            log.exception("Holding news check failed")
        await asyncio.sleep(1)


def username_of(msg_or_user) -> str:
    return (getattr(msg_or_user, "username", None) or "").lower()


async def on_command(ctx: Ctx, msg: Message) -> None:
    """/holdingnews in a private chat: run the check now for the sender, and always reply, even
    when there's nothing (handy for testing)."""
    username = username_of(msg.from_user) if msg.from_user else ""
    save = ctx.store.save
    if username not in ctx.st.holding_news_recipients.values():
        await asyncio.to_thread(save, await msg.reply_text("You're not set up for holding news."))
        return
    if "*" in await asyncio.to_thread(ctx.store.muted_codes, username):
        await asyncio.to_thread(save, await msg.reply_text(
            "You've unsubscribed from all holding news.", reply_markup=undo_button("*")))
        return
    draft = Draft(ctx.bot, msg.chat_id)
    await draft.start()
    try:
        result = (await run_holding_news(ctx, only_username=username)).get(username)
    finally:
        draft.stop()
    if result:
        await send_holding_news(ctx, msg.chat_id, *result)
    else:
        await asyncio.to_thread(
            save, await msg.reply_text("No major news on your holdings in the past 24 hours."))


async def on_button(ctx: Ctx, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The Unsubscribe button and its menu under holding-news messages."""
    query = update.callback_query
    username = username_of(query.from_user)
    if username not in ctx.st.holding_news_recipients.values():
        await query.answer("You're not set up for holding news.")
        return
    _, action, *rest = query.data.split(":", 2)
    code = rest[0] if rest else ""
    msg = query.message
    if action == "menu":
        codes = await asyncio.to_thread(ctx.store.news_message_codes, msg.chat_id, msg.message_id)
        await query.answer()
        await query.edit_message_reply_markup(unsubscribe_menu(codes))
    elif action == "cancel":
        await query.answer()
        await query.edit_message_reply_markup(UNSUBSCRIBE_BUTTON)
    elif action in ("mute", "undo"):
        muting = action == "mute"
        await asyncio.to_thread(ctx.store.set_muted, username, code, muting)
        what = "all holding news" if code == "*" else f"{code} news"
        log.info("@%s %s holding news: %s", username, "unsubscribed from" if muting else "resubscribed to", code)
        await query.answer(f"Unsubscribed from {what}" if muting else "Resubscribed")
        await query.edit_message_reply_markup(undo_button(code) if muting else UNSUBSCRIBE_BUTTON)


