import asyncio

import pytest

from tgbot import config
from tgbot.context import Ctx
from tgbot.features import dm_buttons
from tgbot.llm.base import Usage
from tgbot.telegram.handlers import Handlers, format_answer, mentions
from tgbot.telegram.handlers import photo_file_id as pick_photo

from .conftest import FakeBot, update_for, user_msg
from .fakes import FakeMcp, ScriptedBackend, registry, step


def make_ctx(env, store, script, *, servers=(), **extra):
    st = config.load({**env, **extra})
    backend = ScriptedBackend(script)
    ctx = Ctx(st, store, backend, registry(*servers), FakeBot())
    return ctx, backend, Handlers(ctx)


async def run(h, msg, edited=False):
    await h.on_message(update_for(msg, edited), None)
    await asyncio.sleep(0)


async def test_group_mention_is_answered_with_linked_tickers_and_logged(env, store):
    ctx, backend, h = make_ctx(env, store, [step("NVDA looks <b>strong</b>, SQX.AX too")])
    m = user_msg(ctx.bot, "@stockbot how is NVDA?")
    await run(h, m)
    assert len(m.replies) == 1
    text = m.replies[0]["text"]
    assert '<a href="https://finance.yahoo.com/quote/NVDA">NVDA</a>' in text
    assert '<a href="https://finance.yahoo.com/quote/SQX.AX">SQX</a>' in text and "<b>NVDA" not in text
    assert m.replies[0]["do_quote"] is True and m.replies[0]["disable_notification"] is True
    rows = store.history(-100, 20)
    assert [r.text for r in rows][0] == "@stockbot how is NVDA?" and len(rows) == 2
    assert ctx.bot.actions  # typing indicator in groups


async def test_not_addressed_to_the_bot_is_logged_but_not_answered(env, store):
    ctx, backend, h = make_ctx(env, store, [])
    for i, text in enumerate(("hello everyone", "@stockbot2 hi", "mail me@stockbot"), start=1):
        m = user_msg(ctx.bot, text, mid=i)
        await run(h, m)
        assert m.replies == []
    other_bot = user_msg(ctx.bot, "@stockbot hi", mid=4, is_bot=True)
    other_bot.from_user.is_bot = True
    await run(h, other_bot)
    assert other_bot.replies == [] and len(store.history(-100, 20)) == 4


async def test_reply_to_the_bots_message_triggers(env, store):
    ctx, backend, h = make_ctx(env, store, [step("sure")])
    m = user_msg(ctx.bot, "and now?")
    m.reply_to_message = type(m)(**{**vars(m)})
    m.reply_to_message.from_user = type(m.from_user)(**{**vars(m.from_user), "id": ctx.bot.id})
    await run(h, m)
    assert len(m.replies) == 1


async def test_edited_messages_are_logged_not_answered(env, store):
    ctx, backend, h = make_ctx(env, store, [])
    m = user_msg(ctx.bot, "@stockbot hi")
    await run(h, m, edited=True)
    assert m.replies == [] and len(store.history(-100, 20)) == 1


async def test_chit_chat_gets_no_tools_and_needs_tools_reruns_with_everything(env, store):
    mcp = FakeMcp("yahoo")
    ctx, backend, h = make_ctx(env, store, [step("NEEDS_TOOLS"), step("price is 5")], servers=[mcp])
    m = user_msg(ctx.bot, "@stockbot what do you make of the zeitgeist?")
    await run(h, m)
    assert backend.seen[0]["tools"] == [] and backend.seen[0]["search"] is False
    assert backend.seen[1]["tools"] == ["yahoo__get_quote"] and backend.seen[1]["search"] is True
    assert m.replies[0]["text"] == "price is 5"
    assert "NEEDS_TOOLS" not in m.replies[0]["text"]


async def test_partial_route_that_needs_more_is_asked_again_with_everything(env, store):
    yahoo, sharesight = FakeMcp("yahoo"), FakeMcp("sharesight", gate="portfolio", tools=("list_portfolios",))
    ctx, backend, h = make_ctx(env, store, [step("NEEDS_TOOLS"), step("your portfolio is up")],
                               servers=[yahoo, sharesight])
    m = user_msg(ctx.bot, "@stockbot how is NVDA looking compared with what I hold?")
    await run(h, m)
    assert backend.seen[0]["tools"] == ["yahoo__get_quote"]  # partial: no Sharesight
    assert sorted(backend.seen[1]["tools"]) == ["sharesight__list_portfolios", "yahoo__get_quote"]
    assert m.replies[0]["text"] == "your portfolio is up"
    assert "NEEDS_TOOLS" in backend.seen[0]["system"]  # the partial route told the model how to ask


async def test_market_question_offers_tools_up_front(env, store):
    mcp = FakeMcp("yahoo")
    ctx, backend, h = make_ctx(env, store, [step("ok")], servers=[mcp])
    await run(h, user_msg(ctx.bot, "@stockbot how is NVDA looking?"))
    assert backend.seen[0]["tools"] == ["yahoo__get_quote"] and backend.seen[0]["search"] is True


async def test_portfolio_question_reminds_the_model_to_fetch_fresh_figures(env, store):
    sharesight = FakeMcp("sharesight", gate="portfolio", tools=("list_portfolios",))
    ctx, backend, h = make_ctx(env, store, [step("a"), step("b")], servers=[sharesight])
    await run(h, user_msg(ctx.bot, "@stockbot why did I outperform Sue this month?", mid=1))
    await run(h, user_msg(ctx.bot, "@stockbot how is NVDA looking?", mid=2))
    assert "portfolio tools before answering" in backend.seen[0]["prompt"]
    assert "tailwinds and headwinds" in backend.seen[0]["prompt"]
    assert "portfolio tools before answering" not in backend.seen[1]["prompt"]


async def test_fast_model_used_for_simple_requests_only(env, store):
    ctx, backend, h = make_ctx(env, store, [step("hi"), step("hi")], FAST_MODEL="fast/one")
    await run(h, user_msg(ctx.bot, "@stockbot hello there", mid=1))
    await run(h, user_msg(ctx.bot, "@stockbot how is NVDA?", mid=2))
    assert [s["model"] for s in backend.seen] == ["fast/one", ctx.st.model]


async def test_dm_streams_a_draft_and_skips_quote(env, store):
    ctx, backend, h = make_ctx(env, store, [step("hello there")])
    m = user_msg(ctx.bot, "hi", chat_id=5, user_id=5)
    await run(h, m)
    assert ctx.bot.drafts and ctx.bot.drafts[0] == ""  # "Thinking..."
    assert m.replies[0]["do_quote"] is False and "reply_markup" in m.replies[0]
    assert m.replies[0]["reply_markup"] is None  # DM buttons are off by default
    assert [r.text for r in store.history(5, 20)][0] == "hi"


async def test_dm_buttons_attached_and_preset_is_a_standalone_forced_search(env, store):
    ctx, backend, h = make_ctx(env, store, [step("headlines")], TELEGRAM_DM_BUTTONS="on")
    m = user_msg(ctx.bot, "News", chat_id=5, user_id=5)
    await run(h, m)
    assert m.replies[0]["reply_markup"] is not None
    sent = backend.seen[0]
    assert sent["tools"] == [] and sent["search"] is True


async def test_start_command_greets_with_keyboard(env, store):
    ctx, backend, h = make_ctx(env, store, [], TELEGRAM_DM_BUTTONS="on")
    m = user_msg(ctx.bot, "/start", chat_id=5, user_id=5)
    await run(h, m)
    assert "tap a button" in m.replies[0]["text"] and m.replies[0]["reply_markup"] is not None
    assert backend.seen == []


async def test_error_becomes_a_visible_message(env, store):
    ctx, backend, h = make_ctx(env, store, [RuntimeError("provider exploded")])
    m = user_msg(ctx.bot, "@stockbot hello")
    await run(h, m)
    assert "Sorry, something went wrong" in m.replies[0]["text"] and "provider exploded" in m.replies[0]["text"]


async def test_movers_list_off_by_default_then_answered_once_without_tagging(env, store):
    text = "≥ 5.0% at close (ASX):\nNVDA +6.1%\nAMD +5.5%"
    for flag, expect in (({}, 0), ({"MOVERS_BOTS": "finbotibot"}, 1)):
        ctx, backend, h = make_ctx(env, store, [step("NVDA up on news.\nAMD up too.\n\nSmall caps swing.")],
                                   **flag)
        m = user_msg(ctx.bot, text, is_bot=True)
        m.from_user.is_bot, m.from_user.username = True, "finbotibot"
        m.parse_entities = lambda types=None: {"b": "≥ 5.0% at close (ASX):"}
        await run(h, m)
        assert len(m.replies) == expect
    assert backend.seen[0]["tools"] == [] and backend.seen[0]["search"] is True  # forced search only
    assert "Small caps" not in m.replies[0]["text"]  # closing wrap-up paragraph dropped


def test_format_answer():
    out = format_answer("**NVDA** up, ping @finbotibot", movers_bots=frozenset({"finbotibot"}), summary=False)
    assert "@finbotibot" not in out and "finbotibot" in out and 'quote/NVDA"' in out
    assert format_answer("", movers_bots=frozenset(), summary=False) == "(no response)"


def test_mentions():
    assert mentions("@stockbot hi", "stockbot") and mentions("hi @StockBot!", "stockbot")
    assert not mentions("@stockbot2", "stockbot") and not mentions("me@stockbot", "stockbot")
    assert not mentions("@x", None)


def test_photo_size_choice():
    from types import SimpleNamespace as N
    photos = [N(width=90, file_id="s"), N(width=320, file_id="m"), N(width=800, file_id="l"), N(width=1280, file_id="xl")]
    msg = N(photo=photos, document=None)
    assert pick_photo(msg, small=True) == "l" and pick_photo(msg, small=False) == "xl"
    assert pick_photo(N(photo=photos[:2], document=None), small=True) == "m"
    assert pick_photo(N(photo=None, document=N(mime_type="image/png", file_id="d")), small=True) == "d"
    assert pick_photo(N(photo=None, document=None), small=True) is None


async def test_dm_follow_up_sees_the_whole_previous_answer(env, store):
    ctx, backend, h = make_ctx(env, store, [step("Headlines..." + "x" * 700 + " ADF soldier died in a training incident"),
                                            step("an ADF soldier died near Darwin")])
    await run(h, user_msg(ctx.bot, "headlines?", chat_id=5, user_id=5, mid=1))
    await run(h, user_msg(ctx.bot, "what was the training incident?", chat_id=5, user_id=5, mid=2000))
    assert "ADF soldier died in a training incident" in backend.seen[1]["prompt"]
    assert "…[cut]" not in backend.seen[1]["prompt"].split("what was the training incident?")[0].split("Headlines")[1]


async def test_usage_is_recorded(env, store):
    ctx, backend, h = make_ctx(env, store, [step("ok", usage=Usage(1000, 400, 50, 0.01))])
    await run(h, user_msg(ctx.bot, "@stockbot hi"))
    row = store.usage_summary(0)[0]
    assert row[1:] == (1, 1000, 400, 50, pytest.approx(0.01))


def test_dm_buttons_hash_changes_with_presets(monkeypatch):
    before = dm_buttons.keyboard_hash()
    monkeypatch.setitem(dm_buttons.PRESETS, "New", "x")
    assert dm_buttons.keyboard_hash() != before
