"""Signal message -> reply: the same history, tools and model as Telegram, with Signal's own plumbing."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time

from ..ask import ask
from ..context import Ctx
from ..features import periods
from ..history import compact, format_rows
from ..llm.base import BUSY_REPLY, is_busy
from ..llm.gate import is_portfolio_question, route
from ..prompts import chat_prompt, down_note
from ..store import HistoryRow
from ..textfmt import NO_CONTEXT, md_to_html, plain_text, split_html, strip_disclaimer
from .client import SignalClient, SignalError
from .parse import Incoming, parse, unwrap

log = logging.getLogger("bot")
PORTFOLIO_ADMINS_ONLY = "Portfolio data is for admins only."


def signal_chunks(model_text: str) -> list[str]:
    """The model's Telegram-HTML answer as plain-text messages (Signal has no HTML), keeping its lines."""
    out = []
    for chunk in split_html(md_to_html(strip_disclaimer(model_text))):
        lines = [plain_text(line) for line in chunk.split("\n")]
        text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
        if text:
            out.append(text)
    return out or ["(no response)"]


class SignalHandlers:
    def __init__(self, ctx: Ctx, client: SignalClient, account: str, own_id: str):
        self.ctx, self.client, self.account, self.own_id = ctx, client, account, own_id

    async def on_message(self, obj: dict) -> None:
        unwrapped = unwrap(obj)
        if unwrapped is None:
            return
        envelope, account = unwrapped
        if account and account not in (self.account, self.own_id):
            return  # another account of the same daemon
        inc = parse(envelope, self.own_id)
        if inc is None:
            return
        await asyncio.to_thread(self.ctx.store.save_signal, self.own_id, inc.chat,
                                HistoryRow(inc.ts, inc.sender_name, inc.text, inc.ts // 1000, inc.quote_ts))
        if self._is_for_bot(inc):
            await self._answer(inc)

    def _is_for_bot(self, inc: Incoming) -> bool:
        """DMs from the allow-list; in groups an @mention or a reply to one of the bot's messages."""
        if inc.group:
            return inc.mentioned_bot or inc.quote_author == self.own_id
        if inc.sender_ids & self.ctx.st.signal_allowed:
            return True
        log.info("Ignored a Signal DM from %s: not in SIGNAL_ALLOWED_SENDERS", inc.sender)
        return False

    async def _answer(self, inc: Incoming) -> None:
        text = NO_CONTEXT.sub("", inc.text).strip() if NO_CONTEXT.search(inc.text) else inc.text
        preview = " ".join(text.split())
        log.info("[Signal %s%s] %s", inc.sender_name, " (group)" if inc.group else "",
                 preview[:60].rstrip() + " ..." if len(preview) > 60 else preview)
        try:
            chunks = await self._generate(inc, text, nocontext=text != inc.text)
        except Exception as e:
            if is_busy(e):
                log.warning("Reply failed, the provider is busy: %s", " ".join(str(e).split()))
                chunks = [BUSY_REPLY]
            else:
                log.exception("Reply failed")
                chunks = [f"Sorry, something went wrong:\n{type(e).__name__}: {e}"[:1500]]
        await self._send(inc, chunks)

    async def _generate(self, inc: Incoming, text: str, *, nocontext: bool) -> list[str]:
        ctx, st = self.ctx, self.ctx.st
        admin = bool(inc.sender_ids & st.signal_allowed)  # the people trusted with DMs also get portfolio data
        if nocontext:  # only the message that tagged the bot
            rows = [HistoryRow(inc.ts, inc.sender_name, text, inc.ts // 1000, None)]
        else:
            rows = await asyncio.to_thread(ctx.store.signal_history, self.own_id, inc.chat, st.history_limit)
        quote = None if nocontext else self._quote(inc, rows)
        gate_text = text + ("" if nocontext else " " + inc.quote_text)
        chosen = route(gate_text, st, ctx.registry)
        portfolio_up = any(s.cfg.get("gate") == "portfolio" for s in ctx.registry.up())
        if not admin and portfolio_up and is_portfolio_question(gate_text, st):
            return [PORTFOLIO_ADMINS_ONLY]
        has_portfolio = any(s.cfg.get("gate") == "portfolio" for s in chosen.servers)
        extra = [periods.tool(ctx)] if admin and has_portfolio else []
        fresh = has_portfolio and is_portfolio_question(gate_text, st)
        prompt = chat_prompt(
            transcript=format_rows(rows, ctx.self_name, st.tz, line_max=st.history_line_max,
                                   compact_text=st.token_saver, own_line_max=st.history_own_line_max),
            sender=inc.sender_name, private=not inc.group, reply_quote=quote, msg_id=inc.ts,
            now=ctx.now(), down=down_note(ctx.registry.down()), saver=st.token_saver, fresh=fresh)
        answer = await ask(ctx, [{"type": "text", "text": prompt}], chosen,
                           cache_key="signal-" + hashlib.sha1(inc.chat.encode()).hexdigest()[:12],
                           extra_tools=extra, admin=admin)
        return signal_chunks(answer.text)

    def _quote(self, inc: Incoming, rows: list[HistoryRow]) -> str | None:
        if inc.quote_ts is None:
            return None
        who = "You" if inc.quote_author == self.own_id else next(
            (r.sender for r in rows if r.message_id == inc.quote_ts), "someone")
        body = inc.quote_text
        return f"[#{inc.quote_ts}] {who}: {compact(body) if self.ctx.st.token_saver else body}"

    async def _send(self, inc: Incoming, chunks: list[str]) -> None:
        for i, chunk in enumerate(chunks):
            params: dict = {"account": self.account, "message": chunk}
            params.update({"groupId": inc.chat} if inc.group else {"recipient": [inc.sender]})
            if inc.group and i == 0:  # a group reply quotes the message it answers, as on Telegram
                params.update(quoteTimestamp=inc.ts, quoteAuthor=inc.sender, quoteMessage=inc.text[:200])
            try:
                try:
                    result = await self.client.request("send", params)
                except SignalError:
                    if "quoteTimestamp" not in params:
                        raise
                    for key in ("quoteTimestamp", "quoteAuthor", "quoteMessage"):
                        params.pop(key)
                    result = await self.client.request("send", params)
            except SignalError as e:
                log.error("Signal: couldn't send a reply: %s", e)
                return
            sent = int((result or {}).get("timestamp") or time.time() * 1000)
            await asyncio.to_thread(self.ctx.store.save_signal, self.own_id, inc.chat,
                                    HistoryRow(sent, self.ctx.self_name, chunk, sent // 1000, inc.ts))
