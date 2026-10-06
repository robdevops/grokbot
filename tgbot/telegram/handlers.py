"""on_message: decide whether a message is for the bot, build the prompt, answer, send."""

from __future__ import annotations

import asyncio
import base64
import html
import logging
import re
from dataclasses import dataclass

from telegram import Message, Update
from telegram.ext import ContextTypes

from .. import config
from ..ask import ask
from ..context import Ctx
from ..features import dm_buttons, holding_news, movers
from ..history import compact, format_rows
from ..llm.gate import route
from ..msgtext import describe, sender_name
from ..prompts import MOVERS_EXPLAIN, PRESET_PROMPT, chat_prompt, down_note
from ..store import is_dm
from ..textfmt import md_to_html, strip_disclaimer
from ..tickers import link_tickers
from .draft import Draft, start_typing
from .send import reply_chunks, reply_html

log = logging.getLogger("bot")
READ_IMAGE_WORDS = re.compile(r"\b(read|text|says?|chart|table|number|screenshot|ocr|zoom|detail|label)\b", re.I)


def mentions(text: str, username: str | None) -> bool:
    """Whether text @mentions this bot: @stockbot2 is a different bot from @stockbot."""
    return bool(username and re.search(rf"(?<![\w@])@{re.escape(username)}(?!\w)", text, re.I))


def photo_file_id(msg: Message, *, small: bool) -> str | None:
    """A photo's file id: with `small`, the smallest size at least 640 px wide (fewer image
    tokens), otherwise the largest."""
    if msg.photo:
        sizes = sorted(msg.photo, key=lambda p: p.width)
        if small:
            return next((p for p in sizes if p.width >= 640), sizes[-1]).file_id
        return sizes[-1].file_id
    doc = msg.document
    if doc and (doc.mime_type or "") in ("image/jpeg", "image/png"):
        return doc.file_id
    return None


async def image_part(bot, file_id: str, detail: str) -> dict:
    f = await bot.get_file(file_id)
    data = bytes(await f.download_as_bytearray())
    mime = "image/png" if data[:4] == b"\x89PNG" else "image/jpeg"
    return {"type": "image", "url": f"data:{mime};base64,{base64.b64encode(data).decode()}",
            "detail": detail}


@dataclass
class Trigger:
    text: str
    private: bool
    movers: bool
    reply_target: Message | None
    replied_to_bot: bool


class Handlers:
    def __init__(self, ctx: Ctx):
        self.ctx = ctx

    def _record(self, msg: Message) -> None:
        """Log every message, including edits (they overwrite the original), and remember who
        sent it so holding-news DMs can reach people by username."""
        if self.ctx.st.holding_news:
            self.ctx.store.remember_user(msg)
        self.ctx.store.save(msg)

    async def on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if msg is None:
            return
        await asyncio.to_thread(self._record, msg)
        if update.edited_message:
            return  # don't answer edited messages
        trig = self._trigger(msg)
        if trig is None:
            return
        if await self._command(msg, trig):
            return
        await self._answer(msg, trig)

    def _trigger(self, msg: Message) -> Trigger | None:
        """Is this message for the bot? Other bots are logged but never answered (avoids bot
        loops), except movers lists, which are answered once."""
        st, bot = self.ctx.st, self.ctx.bot
        is_movers = st.movers_explain and movers.is_movers_list(msg, st.movers_bots)
        if msg.from_user and msg.from_user.is_bot and not is_movers:
            return None
        text = msg.text or msg.caption or ""
        private = is_dm(msg.chat_id)
        reply_target = msg.reply_to_message
        replied_to_bot = bool(reply_target and reply_target.from_user
                              and reply_target.from_user.id == bot.id)
        if not (is_movers or private or mentions(text, bot.username) or replied_to_bot):
            return None
        return Trigger(text, private, is_movers, reply_target, replied_to_bot)

    async def _command(self, msg: Message, trig: Trigger) -> bool:
        """Private-chat commands handled here (/credits and /usage have their own handlers)."""
        if not trig.private or not trig.text.startswith("/"):
            return False
        command = trig.text.split()[0].split("@")[0].lower()
        st = self.ctx.st
        if command == "/start":
            hello = "Hi! Ask me anything" + (", or tap a button below." if st.dm_buttons else ".")
            sent = await msg.reply_text(hello, reply_markup=dm_buttons.keyboard() if st.dm_buttons else None)
            await asyncio.to_thread(self.ctx.store.save, sent)
            return True
        if command == "/holdingnews" and st.holding_news:
            await holding_news.on_command(self.ctx, msg)
            return True
        return False

    # -- building the request --------------------------------------------------------
    async def _images(self, msg: Message, trig: Trigger) -> list[dict]:
        st = self.ctx.st
        small = st.token_saver
        detail = "high" if (not small or READ_IMAGE_WORDS.search(trig.text)) else "low"
        sources = () if trig.movers else (trig.reply_target, msg)  # movers: the caption is enough
        ids = [fid for m in sources if m and (fid := photo_file_id(m, small=small))]
        found = await asyncio.gather(
            *(image_part(self.ctx.bot, f, detail) for f in ids[:config.MAX_IMAGES]),
            return_exceptions=True)
        out = []
        for img in found:
            if isinstance(img, BaseException):
                log.error("Couldn't download image", exc_info=img)
            else:
                out.append(img)
        if out:
            log.info("Attached %d image(s)", len(out))
        return out

    def _quote(self, trig: Trigger) -> str | None:
        t = trig.reply_target
        if not t:
            return None
        who = "You" if trig.replied_to_bot else sender_name(t)
        body = describe(t)
        return f"[#{t.message_id}] {who}: {compact(body) if self.ctx.st.token_saver else body}"

    async def _chat_text(self, msg: Message, trig: Trigger, middle: str = "") -> str:
        ctx, st = self.ctx, self.ctx.st
        rows = await asyncio.to_thread(ctx.store.history, msg.chat_id, st.history_limit)
        transcript = format_rows(rows, ctx.self_name, st.tz, line_max=st.history_line_max,
                                 compact_text=st.token_saver, own_line_max=st.history_own_line_max)
        return chat_prompt(
            transcript=transcript, sender=sender_name(msg), private=trig.private,
            reply_quote=self._quote(trig), msg_id=msg.message_id, now=ctx.now(),
            down=down_note(ctx.registry.down()), saver=st.token_saver, middle=middle)

    # -- answering ---------------------------------------------------------------------
    async def _answer(self, msg: Message, trig: Trigger) -> None:
        ctx, st = self.ctx, self.ctx.st
        who = f"{sender_name(msg)} {msg.from_user.id if msg.from_user else None}"
        where = "" if trig.private else f" @ {msg.chat_id}"
        preview = " ".join(trig.text.split())
        preview = preview[:60].rstrip() + " ..." if len(preview) > 60 else preview
        log.info("[%s%s]%s %s", who, where, " (movers list)" if trig.movers else "",
                 preview or f"[{describe(msg) or 'no text'}]")

        draft = Draft(ctx.bot, msg.chat_id) if trig.private else None
        if draft:
            await draft.start()
        else:
            typing = await start_typing(ctx.bot, msg.chat_id)  # a Telegram hiccup is not fatal
        try:
            html_text = await self._generate(msg, trig, draft)
        except Exception as e:
            log.exception("Reply failed")
            html_text = ("Sorry, something went wrong:\n"
                         f"<pre>{html.escape(f'{type(e).__name__}: {e}'[:1500])}</pre>")
        finally:
            if draft:
                draft.stop()
            else:
                typing.cancel()
        markup = dm_buttons.keyboard() if trig.private and st.dm_buttons else None
        await reply_chunks(msg, html_text, ctx.store, quote=not trig.private, last_markup=markup)

    async def _generate(self, msg: Message, trig: Trigger, draft: Draft | None) -> str:
        ctx, st = self.ctx, self.ctx.st
        await ctx.registry.wait_started(config.MCP_STARTUP_WAIT)
        preset = dm_buttons.preset_for(trig.text) if trig.private and st.dm_buttons else None
        on_text = draft.update if draft else None
        chat_id = msg.chat_id
        if preset:  # a standalone request: no transcript, which would only add tokens
            prompt = PRESET_PROMPT.format(sender=sender_name(msg), label=trig.text.strip(), ask=preset,
                                          now=ctx.now(), what=ctx.backend.search_what)
            parts = [{"type": "text", "text": prompt}]
            answer = await ask(ctx, parts, route("", st, ctx.registry, force_full=True),
                               must_search=True, cache_key=f"chat-{chat_id}", on_text=on_text,
                               kind="preset", chat_id=chat_id)
        elif trig.movers:
            listing = msg.text_html if msg.text else (msg.caption_html or "")
            text = await self._chat_text(msg, trig, MOVERS_EXPLAIN.format(listing=listing))
            parts = [{"type": "text", "text": text}]
            answer = await ask(ctx, parts, route("", st, ctx.registry, force_full=True),
                               must_search=True, cache_key=f"chat-{chat_id}", on_text=on_text,
                               kind="movers", chat_id=chat_id)
        else:
            parts = [{"type": "text", "text": await self._chat_text(msg, trig)}]
            parts += await self._images(msg, trig)
            context = trig.text + " " + (describe(trig.reply_target) if trig.reply_target else "")
            answer = await ask(ctx, parts, route(context, st, ctx.registry),
                               cache_key=f"chat-{chat_id}", on_text=on_text, chat_id=chat_id)
        return format_answer(answer.text, movers_bots=st.movers_bots, summary=trig.movers)


def format_answer(text: str, *, movers_bots: frozenset[str], summary: bool) -> str:
    """The model's text as Telegram HTML: Markdown fallback, no disclaimer, no tags of movers
    bots, Yahoo links on tickers (and, for a movers list, no closing wrap-up paragraph)."""
    out = link_tickers(movers.untag(md_to_html(strip_disclaimer(text)), movers_bots))
    if summary:
        out = movers.strip_summary(out)
    return out or "(no response)"


async def send_reply_text(msg: Message, text: str) -> None:
    """A short plain reply (used by commands)."""
    await reply_html(msg, html.escape(text))
