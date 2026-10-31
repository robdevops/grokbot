import asyncio

import pytest

from lib import config
from lib.context import Ctx
from lib.features import dm_buttons
from lib.llm.base import Overloaded, Usage
from lib.telegram.handlers import Handlers, format_answer, mentions
from lib.telegram.handlers import photo_file_id as pick_photo

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


async def test_a_short_follow_up_to_a_portfolio_reply_still_gets_the_portfolio_tools(env, store):
    sharesight = FakeMcp("sharesight", gate="portfolio", tools=("list_portfolios",))
    ctx, backend, h = make_ctx(env, store, [step("Your portfolio best month was May"), step("In dollars: ..."), step("c"), step("d")],
                               servers=[sharesight])
    await run(h, user_msg(ctx.bot, "@stockbot how is my portfolio doing?", mid=1))
    await run(h, user_msg(ctx.bot, "@stockbot what about in dollar terms?", mid=2000))
    assert "sharesight__list_portfolios" in backend.seen[1]["tools"] and "period_returns" in backend.seen[1]["tools"]
    # too long after the reply: a plain message again
    await run(h, user_msg(ctx.bot, "@stockbot and what about the other one?", mid=3000, ts=100_000))
    assert backend.seen[2]["tools"] == []
    # a long unrelated message right after a reply is not read as a follow-up
    long = "@stockbot " + " ".join(["tell me a long story about the history of the word mortgage"] * 3)
    await run(h, user_msg(ctx.bot, long, mid=4000))
    assert backend.seen[3]["tools"] == []


async def test_portfolio_question_reminds_the_model_to_fetch_fresh_figures(env, store):
    sharesight = FakeMcp("sharesight", gate="portfolio", tools=("list_portfolios",))
    ctx, backend, h = make_ctx(env, store, [step("a"), step("b")], servers=[sharesight])
    await run(h, user_msg(ctx.bot, "@stockbot why did I outperform Alice this month?", mid=1))
    await run(h, user_msg(ctx.bot, "@stockbot how is NVDA looking?", mid=2))
    assert "portfolio tools before answering" in backend.seen[0]["prompt"]
    assert "tailwinds and headwinds" in backend.seen[0]["prompt"]
    assert "your market tools" in backend.seen[0]["prompt"]  # not portfolio tools only (Yahoo comparisons)
    assert "portfolio tools before answering" not in backend.seen[1]["prompt"]


async def test_typing_starts_while_the_bot_waits_for_mcp(env, store):
    mcp = FakeMcp("yahoo")
    mcp._task = object()  # started, not yet connected
    ctx, backend, h = make_ctx(env, store, [step("ok")], servers=[mcp])
    m = user_msg(ctx.bot, "@stockbot how is NVDA looking?")
    job = asyncio.create_task(run(h, m))
    await asyncio.sleep(0.05)
    assert ctx.bot.actions and not job.done()  # typing shown, still waiting
    mcp._ready.set()
    await job
    assert m.replies[0]["text"] == "ok"


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


async def test_busy_provider_gets_a_plain_reply_and_a_one_line_log(env, store, caplog):
    err = Overloaded("xAI response failed: ResponseError(code='server_error',\n message='at capacity')")
    ctx, backend, h = make_ctx(env, store, [err])
    m = user_msg(ctx.bot, "@stockbot hello")
    await run(h, m)
    assert "at capacity right now" in m.replies[0]["text"] and "<pre>" not in m.replies[0]["text"]
    line = next(r.getMessage() for r in caplog.records if "provider is busy" in r.getMessage())
    assert "\n" not in line and not any(r.exc_info for r in caplog.records)


async def test_nocontext_sends_only_the_tagging_message_without_the_tag(env, store):
    ctx, backend, h = make_ctx(env, store, [step("fine"), step("fine too")])
    await run(h, user_msg(ctx.bot, "did anyone watch the football last night", mid=1))
    target = user_msg(ctx.bot, "BHP looks cheap", mid=2)
    await run(h, target)
    m = user_msg(ctx.bot, "@stockbot  #NoContext what do you make of NVDA?", mid=3)
    m.reply_to_message = target
    await run(h, m)
    prompt = backend.seen[0]["prompt"]
    assert "NVDA" in prompt and "nocontext" not in prompt.lower()
    assert "football" not in prompt and "BHP" not in prompt  # no history, no replied-to message
    assert "is a reply to" not in prompt
    assert len(m.replies) == 1
    await run(h, user_msg(ctx.bot, "@stockbot what about NVDA?", mid=4))  # without the tag: history is back
    assert "football" in backend.seen[1]["prompt"]


async def test_movers_prompt_leaves_out_the_earlier_chat(env, store):
    ctx, backend, h = make_ctx(env, store, [step("NVDA up on news.")], MOVERS_BOTS="finbotibot")
    await run(h, user_msg(ctx.bot, "did anyone watch the football last night"))
    m = user_msg(ctx.bot, "≥ 5.0% at close (ASX):\nNVDA +6.1%\nAMD +5.5%", is_bot=True, mid=2)
    m.from_user.is_bot, m.from_user.username = True, "finbotibot"
    m.parse_entities = lambda types=None: {"b": "≥ 5.0% at close (ASX):"}
    await run(h, m)
    prompt = backend.seen[0]["prompt"]
    assert "football" not in prompt and "explain" in prompt
    assert prompt.count("NVDA +6.1%") == 1  # the list once, not also as the last transcript line


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
    assert pick_photo(msg) == "l"  # the smallest size at least 640 px wide
    assert pick_photo(N(photo=photos[:2], document=None)) == "m"
    assert pick_photo(N(photo=None, document=N(mime_type="image/png", file_id="d"))) == "d"
    assert pick_photo(N(photo=None, document=None)) is None


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


# ---- admin-only (the default) ---------------------------------------------------------
async def test_dm_from_a_non_admin_is_logged_but_ignored(env, store):
    ctx, backend, h = make_ctx(env, store, [step("hi")], ADMIN_ONLY="on", ADMIN_CHAT_IDS="5")
    stranger = user_msg(ctx.bot, "hello", chat_id=9, user_id=9)
    await run(h, stranger)
    assert stranger.replies == [] and backend.seen == [] and store.history(9, 20)
    admin = user_msg(ctx.bot, "hello", chat_id=5, user_id=5)
    await run(h, admin)
    assert admin.replies[0]["text"] == "hi"


async def test_group_admin_can_dm_but_admin_only_off_lets_everyone_in(env, store):
    ctx, backend, h = make_ctx(env, store, [step("hi"), step("hi")], ADMIN_ONLY="on")
    store.remember_chat(-100, "Stocks")
    ctx.bot.admins[-100] = [7]
    m = user_msg(ctx.bot, "hello", chat_id=7, user_id=7)
    await run(h, m)
    assert m.replies[0]["text"] == "hi"
    ctx, backend, h = make_ctx(env, store, [step("hi")], ADMIN_ONLY="off")
    open_dm = user_msg(ctx.bot, "hello", chat_id=9, user_id=9, mid=5)
    await run(h, open_dm)
    assert open_dm.replies[0]["text"] == "hi"


async def test_non_admin_gets_no_portfolio_data_in_a_group(env, store):
    sharesight = FakeMcp("sharesight", gate="portfolio", tools=("list_portfolios",))
    ctx, backend, h = make_ctx(env, store, [step("ok")], servers=[sharesight], ADMIN_ONLY="on",
                               ADMIN_CHAT_IDS="5")
    asked = user_msg(ctx.bot, "@stockbot how is my portfolio doing?", user_id=9)
    await run(h, asked)
    assert asked.replies[0]["text"] == "Portfolio data is for admins only." and backend.seen == []
    admin = user_msg(ctx.bot, "@stockbot how is my portfolio doing?", user_id=5, mid=2)
    await run(h, admin)
    assert backend.seen[0]["tools"] == ["sharesight__list_portfolios", "period_returns"]  # admins also get the period tool


async def test_non_admin_never_gets_portfolio_tools_even_on_the_rerun(env, store):
    yahoo, sharesight = FakeMcp("yahoo"), FakeMcp("sharesight", gate="portfolio", tools=("list_portfolios",))
    ctx, backend, h = make_ctx(env, store, [step("ok")], servers=[yahoo, sharesight], ADMIN_ONLY="on")
    m = user_msg(ctx.bot, "@stockbot how is NVDA looking?", user_id=9)
    await run(h, m)
    assert backend.seen[0]["tools"] == ["yahoo__get_quote"]
    ctx, backend, h = make_ctx(env, store, [step("NEEDS_TOOLS"), step("done")], servers=[yahoo, sharesight],
                               ADMIN_ONLY="on")
    m = user_msg(ctx.bot, "@stockbot how is NVDA looking?", user_id=9)
    await run(h, m)
    assert backend.seen[1]["tools"] == ["yahoo__get_quote"] and m.replies[0]["text"] == "done"


def test_questions_about_the_bots_tools_are_recognised():
    from lib.llm.gate import asks_tools
    for text in ("what tools do you have?", "list your tools", "Which tools can you use", "show me your capabilities",
                 "what can you do", "what are you able to do?", "tell me what data sources you have access to"):
        assert asks_tools(text), text
    for text in ("how is NVDA doing?", "which tools does NVDA use for AI", "show me your portfolio",
                 "what do you think of the features of the new iPhone", "what is the best tool for charting"):
        assert not asks_tools(text), text


async def test_asked_what_tools_it_has_the_bot_lists_them_from_its_own_list_and_offers_none(env, store):
    yahoo = FakeMcp("yahoo", tools=("get_stock_quote", "compare_stocks"), description="Yahoo Finance market data. More.")
    sharesight = FakeMcp("sharesight", tools=("list_portfolios",), gate="portfolio", description="Sharesight portfolios.")
    ctx, backend, h = make_ctx(env, store, [step("I have these.")], servers=(yahoo, sharesight),
                               POST_TO_GROUPS_FROM_DM="on")
    m = user_msg(ctx.bot, "@stockbot what tools do you have?")
    await run(h, m)
    seen = backend.seen[0]
    assert seen["tools"] == [] and seen["search"] is True and "no live data tools" not in seen["system"]
    for word in ("yahoo", "get_stock_quote", "compare_stocks", "sharesight", "list_portfolios", "period_returns",
                 "make_chart", "send_to_group", "web search", "complete list"):
        assert word in seen["prompt"], word
    assert "Yahoo Finance market data" in seen["prompt"] and "More." not in seen["prompt"]  # first sentence only
    assert m.replies[0]["text"] == "I have these."


async def test_the_tool_list_leaves_out_what_the_asker_may_not_use(env, store):
    yahoo = FakeMcp("yahoo", tools=("get_stock_quote",))
    sharesight = FakeMcp("sharesight", tools=("list_portfolios",), gate="portfolio")
    ctx, backend, h = make_ctx(env, store, [step("ok")], servers=(yahoo, sharesight), ADMIN_ONLY="on",
                               SEARCH="off")
    await run(h, user_msg(ctx.bot, "@stockbot what can you do?"))
    prompt = backend.seen[0]["prompt"]
    assert "get_stock_quote" in prompt and "make_chart" in prompt
    assert not any(w in prompt for w in ("sharesight", "list_portfolios", "period_returns", "send_to_group", "web search"))
    assert backend.seen[0]["search"] is False


def sticker(file_id="st", *, animated=False, video=False, thumb=None):
    from types import SimpleNamespace as N
    return N(photo=None, document=None, sticker=N(file_id=file_id, is_animated=animated, is_video=video,
                                                  thumbnail=N(file_id=thumb) if thumb else None))


def test_a_static_sticker_is_sent_as_it_is_and_an_animated_one_by_its_preview_frame():
    assert pick_photo(sticker()) == "st"
    assert pick_photo(sticker(animated=True, thumb="prev")) == "prev"
    assert pick_photo(sticker(video=True, thumb="prev")) == "prev"
    assert pick_photo(sticker(animated=True)) is None  # no preview to show


@pytest.mark.parametrize("data, mime", [
    (b"\x89PNG\r\n\x1a\n....", "image/png"),
    (b"RIFF\x24\x00\x00\x00WEBPVP8 ", "image/webp"),
    (b"\xff\xd8\xff\xe0....", "image/jpeg"),
])
async def test_an_image_is_sent_with_the_type_it_has_stickers_are_webp(data, mime):
    import base64
    from types import SimpleNamespace as N

    from lib.telegram.handlers import image_part

    class Bot:
        async def get_file(self, file_id):
            async def download():
                return bytearray(data)
            return N(download_as_bytearray=download)

    part = await image_part(Bot(), "f", "low")
    assert part["url"] == f"data:{mime};base64,{base64.b64encode(data).decode()}" and part["detail"] == "low"


async def test_a_sticker_in_a_dm_or_replied_to_is_attached_but_other_group_stickers_are_not(env, store):
    from types import SimpleNamespace as N
    ctx, _, h = make_ctx(env, store, [])
    fetched = []

    async def get_file(file_id):
        fetched.append(file_id)

        async def download():
            return bytearray(b"RIFF\x24\x00\x00\x00WEBPVP8 ")
        return N(download_as_bytearray=download)

    ctx.bot.get_file = get_file
    dm = user_msg(ctx.bot, "", chat_id=5, user_id=5)
    dm.sticker = sticker("dm-sticker").sticker
    trig = h._trigger(dm)
    parts = await h._images(dm, trig)
    assert [p["url"].split(",")[0] for p in parts] == ["data:image/webp;base64"] and fetched == ["dm-sticker"]
    group = user_msg(ctx.bot, "@stockbot what does this sticker mean?", chat_id=-100)
    group.reply_to_message = user_msg(ctx.bot, "", chat_id=-100, mid=7)
    group.reply_to_message.sticker = sticker("replied").sticker
    assert len(await h._images(group, h._trigger(group))) == 1 and fetched[-1] == "replied"
    stray = user_msg(ctx.bot, "", chat_id=-100, mid=8)  # a sticker in the group that nobody addressed to the bot
    stray.sticker = sticker("stray").sticker
    assert h._trigger(stray) is None  # so it is never downloaded, let alone sent


def reaction_probe(ctx, error=None):
    """Records the clear-check calls; raises `error` if given."""
    calls = []

    async def set_message_reaction(chat_id, message_id, reaction=None, **kw):
        calls.append((chat_id, message_id, reaction))
        if error:
            raise error

    ctx.bot.set_message_reaction = set_message_reaction
    return calls


async def test_a_chat_cleared_for_the_bot_is_forgotten(env, store):
    from telegram.error import BadRequest
    ctx, backend, h = make_ctx(env, store, [step("first answer"), step("fresh answer")])
    calls = reaction_probe(ctx)
    await run(h, user_msg(ctx.bot, "remember the banana plan", chat_id=5, user_id=5, mid=1))
    assert calls == []  # nothing before it to look at
    reaction_probe(ctx, BadRequest("Message to react not found"))  # the chat was cleared for the bot in between
    await run(h, user_msg(ctx.bot, "hello again", chat_id=5, user_id=5, mid=2000))
    assert "banana plan" not in backend.seen[1]["prompt"] and "hello again" in backend.seen[1]["prompt"]
    assert not any("banana" in r.text or "first answer" in r.text for r in store.history(5, 20))  # the old rows are gone


async def test_a_chat_that_still_exists_keeps_its_history_and_is_checked_with_an_empty_reaction(env, store):
    ctx, backend, h = make_ctx(env, store, [step("first answer"), step("second answer")])
    calls = reaction_probe(ctx)
    await run(h, user_msg(ctx.bot, "remember the banana plan", chat_id=5, user_id=5, mid=1))
    await run(h, user_msg(ctx.bot, "hello again", chat_id=5, user_id=5, mid=2000))
    assert calls == [(5, 1001, None)]  # the bot's own earlier reply, with no reaction: harmless
    assert "banana plan" in backend.seen[1]["prompt"]


async def test_an_error_the_check_does_not_understand_keeps_the_history_and_is_logged_once(env, store, caplog):
    from telegram.error import BadRequest
    ctx, backend, h = make_ctx(env, store, [step("a"), step("b"), step("c")])
    reaction_probe(ctx, BadRequest("Reaction_invalid"))
    for mid in (1, 2000, 3000):
        await run(h, user_msg(ctx.bot, f"message {mid}", chat_id=5, user_id=5, mid=mid))
    assert "message 1" in backend.seen[2]["prompt"]
    assert [r.getMessage() for r in caplog.records if "Couldn't tell whether" in r.getMessage()] == [
        "Couldn't tell whether chat 5 was cleared: Reaction_invalid"]


async def test_group_chats_are_never_checked(env, store):
    ctx, backend, h = make_ctx(env, store, [step("a"), step("b")])
    calls = reaction_probe(ctx)
    await run(h, user_msg(ctx.bot, "@stockbot one", chat_id=-100, mid=1))
    await run(h, user_msg(ctx.bot, "@stockbot two", chat_id=-100, mid=2000))
    assert calls == []
