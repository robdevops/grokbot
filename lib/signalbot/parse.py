"""signal-cli JSON envelopes -> the few facts the bot needs (None for typing, receipts, syncs...)."""

from __future__ import annotations

from dataclasses import dataclass

OBJECT_REPLACEMENT = "￼"  # what Signal puts in the text where a mention goes


@dataclass(frozen=True)
class Incoming:
    chat: str  # a group's ID, or "dm:<sender>" for a private chat
    group: bool
    sender: str  # the sender's UUID (or number if there is no UUID)
    sender_ids: frozenset[str]  # every ID the sender is known by, lowercase (for the allow-list)
    sender_name: str
    text: str  # mentions written out; the bot's own mention removed
    ts: int  # Signal's message ID: the sender's timestamp in ms
    mentioned_bot: bool
    quote_ts: int | None
    quote_author: str | None
    quote_text: str


def unwrap(obj: dict) -> tuple[dict, str] | None:
    """(envelope, account) from a bare `{"envelope":..}` line or a JSON-RPC `receive` notification."""
    if obj.get("method") == "receive":
        obj = obj.get("params") or {}
    envelope = obj.get("envelope")
    if not isinstance(envelope, dict):
        return None
    return envelope, str(obj.get("account") or "")


def _resolve_mentions(text: str, mentions: list[dict], own_id: str) -> tuple[str, bool]:
    """Replace each mention placeholder (from the end, so offsets hold) with @name; the bot's own
    mention is dropped."""
    mentioned = False
    for m in sorted(mentions, key=lambda m: m.get("start", 0), reverse=True):
        start, length = int(m.get("start", 0)), int(m.get("length", 1))
        is_bot = own_id and own_id in (m.get("uuid"), m.get("number"))
        mentioned = mentioned or bool(is_bot)
        who = "" if is_bot else "@" + str(m.get("name") or m.get("number") or str(m.get("uuid"))[:8])
        text = text[:start] + who + text[start + length:]
    return " ".join(text.replace(OBJECT_REPLACEMENT, "").split()) if mentions else text.strip(), mentioned


def parse(envelope: dict, own_id: str) -> Incoming | None:
    data = envelope.get("dataMessage")
    if not isinstance(data, dict) or not data.get("message"):
        return None
    ids = [envelope.get("sourceUuid"), envelope.get("source"), envelope.get("sourceNumber")]
    sender = next((i for i in ids if i), "")
    if not sender or sender == own_id:
        return None
    group = (data.get("groupInfo") or {}).get("groupId")
    text, mentioned = _resolve_mentions(data["message"], data.get("mentions") or [], own_id)
    quote = data.get("quote") or {}
    name = envelope.get("sourceName") or envelope.get("sourceNumber") or str(sender)[:8]
    return Incoming(
        chat=group or f"dm:{sender}", group=bool(group), sender=sender,
        sender_ids=frozenset(str(i).lower() for i in ids if i), sender_name=str(name),
        text=text, ts=int(data.get("timestamp") or envelope.get("timestamp") or 0),
        mentioned_bot=mentioned, quote_ts=quote.get("id"), quote_author=quote.get("author")
        or quote.get("authorUuid"), quote_text=str(quote.get("text") or ""))
