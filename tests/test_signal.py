import asyncio
import json
import stat
import sys

import pytest

from lib import config
from lib.context import Ctx
from lib.signalbot.client import SignalClient, SignalError, list_accounts
from lib.signalbot.handlers import SignalHandlers, signal_chunks
from lib.signalbot.parse import parse, unwrap

from .conftest import FakeBot
from .fakes import ScriptedBackend, registry, step

BOT = "bbbbbbbb-0000-4000-8000-000000000001"
ROB = "aaaaaaaa-0000-4000-8000-000000000002"
GROUP = "GGGGgroupidGGGGgroupidGGGGgroupidGGGGgroupid="


def envelope(data=None, *, typing=None, source=ROB, name="Alex", ts=1_792_592_655_157):
    env = {"source": source, "sourceNumber": None, "sourceUuid": source, "sourceName": name,
           "sourceDevice": 1, "timestamp": ts}
    if data is not None:
        env["dataMessage"] = {"timestamp": ts, "expiresInSeconds": 0, **data}
    if typing is not None:
        env["typingMessage"] = typing
    return {"envelope": env, "account": BOT}


def group_data(message, **extra):
    return {"message": message, "groupInfo": {"groupId": GROUP, "groupName": "test", "type": "DELIVER"}, **extra}


def mention(start=0):
    return {"name": BOT, "number": None, "uuid": BOT, "start": start, "length": 1}


# ---- parsing --------------------------------------------------------------------------
def test_group_message_dm_and_noise():
    env, account = unwrap(envelope(group_data("Group test")))
    group = parse(env, BOT)
    assert account == BOT and group.group and group.chat == GROUP and group.text == "Group test"
    assert group.sender == ROB and group.sender_name == "Alex" and not group.mentioned_bot

    dm = parse(unwrap(envelope({"message": "Test DM"}))[0], BOT)
    assert not dm.group and dm.chat == f"dm:{ROB}" and dm.ts == 1_792_592_655_157

    for typing in ({"action": "STARTED", "timestamp": 1}, {"action": "STOPPED", "timestamp": 2}):
        assert parse(unwrap(envelope(typing=typing))[0], BOT) is None  # typing indicators
    assert parse(unwrap(envelope({"message": ""}))[0], BOT) is None  # attachment with no text
    assert parse(unwrap(envelope({"message": "hi"}, source=BOT))[0], BOT) is None  # the bot's own


def test_mention_is_resolved_and_a_jsonrpc_wrapper_is_unwrapped():
    text = "￼ what is NVDA at, ￼?"
    other = {"name": "Sam", "number": None, "uuid": "cccccccc-0000-4000-8000-000000000003",
             "start": text.index("\ufffc", 1), "length": 1}
    obj = envelope(group_data(text, mentions=[mention(0), other]))
    inc = parse(unwrap({"jsonrpc": "2.0", "method": "receive", "params": obj})[0], BOT)
    assert inc.mentioned_bot and inc.text == "what is NVDA at, @Sam?"


def test_quote_reply_to_the_bot():
    quote = {"id": 99, "author": BOT, "authorUuid": BOT, "text": "earlier answer"}
    inc = parse(unwrap(envelope(group_data("and AMD?", quote=quote)))[0], BOT)
    assert inc.quote_ts == 99 and inc.quote_author == BOT and inc.quote_text == "earlier answer"


def test_answer_is_plain_text_with_lines_kept():
    out = signal_chunks("<b>NVDA</b> up 3%\nAMD (<a href=\"https://x.test/a\">src</a>) &amp; more\n\n\n\nbye")
    assert out == ["NVDA up 3%\nAMD (src (https://x.test/a)) & more\n\nbye"]


# ---- the reply flow -------------------------------------------------------------------
class FakeSignal:
    def __init__(self):
        self.requests = []
        self.fail_quote = False

    async def request(self, method, params):
        self.requests.append((method, dict(params)))
        if self.fail_quote and "quoteTimestamp" in params:
            raise SignalError("bad quote")
        return {"timestamp": 5_000_000 + len(self.requests)}


def make(env, store, script, **extra):
    st = config.load({**env, "SIGNAL_ALLOWED_SENDERS": ROB.upper(), **extra})
    backend, client = ScriptedBackend(script), FakeSignal()
    ctx = Ctx(st, store, backend, registry(), FakeBot())
    return ctx, backend, client, SignalHandlers(ctx, client, BOT, BOT)


async def test_group_is_answered_on_mention_or_reply_to_the_bot_only(env, store):
    ctx, backend, client, h = make(env, store, [step("NVDA is up"), step("AMD too")])
    await h.on_message(envelope(group_data("lunch anyone?", mentions=[]), ts=1_000))
    assert client.requests == []  # logged as context, not answered
    await h.on_message(envelope(group_data("\ufffc how is NVDA?", mentions=[mention()]), ts=2_000))
    method, params = client.requests[0]
    assert method == "send" and params["groupId"] == GROUP and params["message"] == "NVDA is up"
    assert params["account"] == BOT and params["quoteTimestamp"] == 2_000 and params["quoteAuthor"] == ROB
    assert "lunch anyone?" in backend.seen[0]["prompt"] and "how is NVDA?" in backend.seen[0]["prompt"]
    quote = {"id": 5_000_001, "author": BOT, "text": "NVDA is up"}
    await h.on_message(envelope(group_data("and AMD?", quote=quote), ts=3_000))
    assert client.requests[1][1]["message"] == "AMD too"
    rows = store.signal_history(BOT, GROUP, 20)
    assert [r.sender for r in rows].count(ctx.self_name) == 2  # the bot's replies are in its history, as "You"
    assert "[#5000001] You: NVDA is up" in backend.seen[1]["prompt"]


async def test_dm_needs_an_allowed_sender_and_goes_to_that_person(env, store):
    ctx, backend, client, h = make(env, store, [step("hello Alex")])
    stranger = envelope({"message": "hi bot"}, source="dddddddd-0000-4000-8000-000000000004")
    await h.on_message(stranger)
    assert client.requests == [] and backend.seen == []
    await h.on_message(envelope({"message": "hi bot"}))
    method, params = client.requests[0]
    assert params["recipient"] == [ROB] and "groupId" not in params and "quoteTimestamp" not in params
    assert "Private one-to-one chat with Alex" in backend.seen[0]["prompt"]


async def test_a_rejected_quote_is_retried_without_it(env, store):
    ctx, backend, client, h = make(env, store, [step("ok")])
    client.fail_quote = True
    await h.on_message(envelope(group_data("\ufffc hi", mentions=[mention()])))
    assert len(client.requests) == 2 and "quoteTimestamp" not in client.requests[1][1]


async def test_nocontext_sends_the_message_alone_without_the_tag(env, store):
    ctx, backend, client, h = make(env, store, [step("fine")])
    await h.on_message(envelope(group_data("the football was great"), ts=1_000))
    await h.on_message(envelope(group_data("\ufffc #nocontext what about NVDA?", mentions=[mention()]), ts=2_000))
    prompt = backend.seen[0]["prompt"]
    assert "NVDA" in prompt and "football" not in prompt and "nocontext" not in prompt.lower()


async def test_portfolio_questions_are_for_allowed_senders_only(env, store):
    from .fakes import FakeMcp
    ctx, backend, client, h = make(env, store, [step("never asked")])
    ctx.registry = registry(FakeMcp("sharesight", gate="portfolio", tools=("list_portfolios",)))
    stranger = "dddddddd-0000-4000-8000-000000000004"  # in the group, but not on the allow-list
    await h.on_message(envelope(group_data("\ufffc how is my portfolio doing?", mentions=[mention()]), source=stranger))
    assert client.requests[0][1]["message"] == "Portfolio data is for admins only." and backend.seen == []


# ---- the daemon -----------------------------------------------------------------------
FAKE_DAEMON = """#!{python}
import asyncio, json, sys
args = sys.argv[1:]
if "listAccounts" in args:
    print(json.dumps([{{"number": None, "aci": "{bot}"}}]))
    sys.exit(0)
sock = args[args.index("--socket") + 1]

async def handle(reader, writer):
    note = {{"jsonrpc": "2.0", "method": "receive", "params": json.loads({envelope!r})}}
    writer.write((json.dumps(note) + "\\n").encode())
    await writer.drain()
    async for line in reader:
        req = json.loads(line)
        if req["method"] == "boom":
            reply = {{"jsonrpc": "2.0", "id": req["id"], "error": {{"code": -1, "message": "no good"}}}}
        else:
            reply = {{"jsonrpc": "2.0", "id": req["id"], "result": {{"timestamp": 1234}}}}
        writer.write((json.dumps(reply) + "\\n").encode())
        await writer.drain()

async def main():
    server = await asyncio.start_unix_server(handle, sock)
    async with server:
        await server.serve_forever()

asyncio.run(main())
"""


@pytest.fixture
def fake_signal_cli(tmp_path):
    path = tmp_path / "signal-cli"
    path.write_text(FAKE_DAEMON.format(python=sys.executable, bot=BOT, envelope=json.dumps(envelope({"message": "hi"}))))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


async def test_accounts_are_listed(fake_signal_cli):
    assert await list_accounts(fake_signal_cli) == [{"number": None, "aci": BOT}]


async def test_client_receives_notifications_and_sends_requests(fake_signal_cli):
    got = asyncio.Queue()

    async def on_message(obj):
        await got.put(obj)

    client = SignalClient(fake_signal_cli, on_message)
    task = asyncio.create_task(client.run())
    try:
        obj = await asyncio.wait_for(got.get(), 20)
        assert parse(unwrap(obj)[0], BOT).text == "hi"
        assert await client.request("send", {"message": "x"}) == {"timestamp": 1234}
        with pytest.raises(SignalError, match="no good"):
            await client.request("boom", {})
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    with pytest.raises(SignalError, match="isn't connected"):
        await client.request("send", {})
