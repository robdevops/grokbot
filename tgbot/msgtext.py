"""Turning a Telegram Message into the strings we log and show the model."""

from __future__ import annotations

from telegram import Message

MEDIA_KINDS = (
    "photo", "video", "animation", "voice", "video_note", "audio",
    "document", "sticker", "poll", "location", "contact",
)


def sender_name(msg: Message) -> str:
    """'Full Name (@username)'; must match how a bot's own replies are labelled in history."""
    if msg.from_user:
        u = msg.from_user
        return u.full_name + (f" (@{u.username})" if u.username else "")
    if msg.sender_chat:  # anonymous admins, linked channels
        return msg.sender_chat.title or "Anonymous"
    return "Unknown"


def describe(msg: Message) -> str:
    """Message text as Telegram HTML, with a [media] tag for non-text content.

    Uses text_html / caption_html (rebuilt from Telegram's entities) rather than the
    stripped text, so every bot sharing the DB logs the same thing for the same message."""
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
