import json
from datetime import datetime
from types import SimpleNamespace as N
from zoneinfo import ZoneInfo

import pytest
from telegram.error import Forbidden

from lib import config
from lib.context import Ctx
from lib.features import dm_buttons, holding_news, movers
from lib.telegram import commands

from .conftest import FakeBot, make_msg, update_for, user_msg
from .fakes import FakeMcp, ScriptedBackend, registry, step


def test_is_nothing_and_bullets_and_codes():
    hn = holding_news
    for raw in ("NOTHING", "• NOTHING", "<b>Nothing.</b>", "None", "", "  "):
        assert hn.is_nothing(raw)
    assert not hn.is_nothing("• Nothing Ltd: results beat by a mile, shares up ten percent today")
    assert hn.as_bullets("- A: x\n\n* B: y\n3. C: z") == "• A: x\n• B: y\n• C: z"
    news = '• <a href="https://finance.yahoo.com/quote/NVDA">NVDA</a>: up\n• <b>AMD</b>: down'
    assert hn.news_codes(news, {"NVDA (NASDAQ)": "Nvidia", "AMD (NASDAQ)": "AMD", "F (NYSE)": "Ford"}) == ["AMD", "NVDA"]


def test_seconds_until_rolls_to_tomorrow():
    tz = ZoneInfo("UTC")
    assert holding_news.seconds_until("08:00", datetime(2026, 1, 1, 7, 0, tzinfo=tz)) == 3600
    assert holding_news.seconds_until("08:00", datetime(2026, 1, 1, 9, 0, tzinfo=tz)) == 23 * 3600


def test_unsubscribe_menu_and_undo_buttons():
    menu = holding_news.unsubscribe_menu(["A", "B", "C", "D"])
    data = [b.callback_data for row in menu.inline_keyboard for b in row]
    assert data == ["hn:mute:A", "hn:mute:B", "hn:mute:C", "hn:mute:D", "hn:mute:*", "hn:cancel"]
    assert holding_news.undo_button("*").inline_keyboard[0][0].callback_data == "hn:undo:*"


class Sharesight(FakeMcp):
    async def call(self, tool, args):
        if tool == "list_portfolios":
            return json.dumps({"portfolios": [{"name": "Alex", "id": 7}]})
        cols = ["code", "market", "name"]
        rows = [["NVDA", "NASDAQ", "Nvidia"], ["AMD", "NASDAQ", "AMD"]]
        return json.dumps({"report": {"holdings": {"columns": cols, "rows": rows}}})


def holding_ctx(env, store, script):
    st = config.load({**env, "SHARESIGHT_HOLDING_NEWS_RECIPIENTS": "Alex:alex_llama"})
    ss = Sharesight("sharesight", tools=("list_portfolios",))
    ctx = Ctx(st, store, ScriptedBackend(script), registry(ss), FakeBot())
    store.remember_user(make_msg(1, "x", username="alex_llama", chat_id=-1))
    return ctx


async def test_daily_digest_dms_news_with_links_and_records_it(env, store):
    ctx = holding_ctx(env, store, [step("• NVDA (NASDAQ): beat estimates https://x.com/a")])
    results = await holding_news.run_holding_news(ctx)
    text, codes = results["alex_llama"]
    assert codes == ["NVDA"] and 'quote/NVDA"' in text
    sent = ctx.bot.sent[-1]
    assert sent["chat_id"] == 5 and "Holding news" in sent["text"] and sent["reply_markup"] is not None
    assert store.recent_news("alex_llama", 0) == [text]
    assert ctx.backend.seen[0]["tools"] == [] and ctx.backend.seen[0]["search"] is True  # forced search only


async def test_digest_says_nothing_when_nothing_and_respects_mutes(env, store):
    ctx = holding_ctx(env, store, [step("NOTHING")])
    assert (await holding_news.run_holding_news(ctx)) == {"alex_llama": None} and ctx.bot.sent == []
    store.set_muted("alex_llama", "*", True)
    assert await holding_news.run_holding_news(ctx) == {}
    store.set_muted("alex_llama", "*", False)
    store.set_muted("alex_llama", "NVDA", True)
    ctx.backend.script = [step("• AMD: news")]
    await holding_news.run_holding_news(ctx)
    prompt = ctx.backend.seen[-1]["prompt"]
    assert "AMD (NASDAQ)" in prompt and "NVDA (NASDAQ)" not in prompt  # muted code left out


async def test_digest_skips_people_the_bot_cannot_reach(env, store):
    ctx = holding_ctx(env, store, [step("• NVDA: big news")])
    ctx.bot.fail_send[5] = Forbidden("bot was blocked")
    await holding_news.run_holding_news(ctx)
    assert store.recent_news("alex_llama", 0) == []


async def test_mute_and_undo_buttons(env, store):
    ctx = holding_ctx(env, store, [])
    answers, edits = [], []

    async def answer(text=None):
        answers.append(text)

    async def edit(markup):
        edits.append(markup)

    def query(data, username="alex_llama"):
        return N(data=data, from_user=N(username=username), answer=answer, edit_message_reply_markup=edit,
                 message=N(chat_id=5, message_id=9))

    store.remember_news_message(5, 9, ["NVDA", "AMD"])
    await holding_news.on_button(ctx, N(callback_query=query("hn:menu")), None)
    assert [b.callback_data for r in edits[-1].inline_keyboard for b in r][:2] == ["hn:mute:NVDA", "hn:mute:AMD"]
    await holding_news.on_button(ctx, N(callback_query=query("hn:mute:NVDA")), None)
    assert store.muted_codes("alex_llama") == {"NVDA"} and "Unsubscribed from NVDA news" in answers[-1]
    await holding_news.on_button(ctx, N(callback_query=query("hn:undo:NVDA")), None)
    assert store.muted_codes("alex_llama") == set()
    await holding_news.on_button(ctx, N(callback_query=query("hn:menu", "stranger")), None)
    assert "not set up" in answers[-1]


async def test_holdingnews_command_always_replies(env, store):
    ctx = holding_ctx(env, store, [step("NOTHING")])
    m = user_msg(ctx.bot, "/holdingnews", chat_id=5, user_id=5)
    m.from_user.username = "alex_llama"
    await holding_news.on_command(ctx, m)
    assert m.replies[-1]["text"].startswith("No major news")
    stranger = user_msg(ctx.bot, "/holdingnews", chat_id=6, user_id=6)
    stranger.from_user.username = "nobody"
    await holding_news.on_command(ctx, stranger)
    assert "not set up" in stranger.replies[0]["text"]


# ---- DM buttons: refreshed on startup ------------------------------------------------------
def dm_ctx(env, store, **extra):
    st = config.load({**env, "TELEGRAM_DM_BUTTONS": "on", **extra})
    for chat in (5, 6, 7):
        store.save(make_msg(1, "hi", chat_id=chat))
    return Ctx(st, store, ScriptedBackend([]), registry(), FakeBot())


async def test_keyboard_pushed_once_per_change(env, store, monkeypatch):
    monkeypatch.setattr(dm_buttons, "PUSH_DELAY", 0)
    ctx = dm_ctx(env, store)
    assert await dm_buttons.push_on_startup(ctx) == 3
    assert {s["chat_id"] for s in ctx.bot.sent} == {5, 6, 7} and ctx.bot.sent[0]["reply_markup"] is not None
    assert await dm_buttons.push_on_startup(ctx) == 0  # unchanged: nothing sent
    monkeypatch.setitem(dm_buttons.PRESETS, "Crypto", "crypto news")
    assert await dm_buttons.push_on_startup(ctx) == 3  # changed: sent again


async def test_push_skips_blocked_users_and_still_records_the_hash(env, store, monkeypatch):
    monkeypatch.setattr(dm_buttons, "PUSH_DELAY", 0)
    ctx = dm_ctx(env, store)
    ctx.bot.fail_send[6] = Forbidden("blocked")
    assert await dm_buttons.push_on_startup(ctx) == 2
    assert await dm_buttons.push_on_startup(ctx) == 0


def test_keyboard_layout_two_per_row():
    rows = dm_buttons.keyboard().keyboard
    assert all(len(r) <= 2 for r in rows) and sum(len(r) for r in rows) == len(dm_buttons.PRESETS)
    assert dm_buttons.preset_for(" News ") == "today's top headlines" and dm_buttons.preset_for("hello") is None


# ---- movers --------------------------------------------------------------------------
def test_movers_helpers():
    bots = frozenset({"finbotibot"})
    assert movers.untag("hi @FinbotIbot!", bots) == "hi FinbotIbot!"
    text = '<a href="x">NVDA</a> up\n\n<a href="y">AMD</a> up\n\nTypical swings.'
    assert movers.strip_summary(text).endswith("up") and "Typical" not in movers.strip_summary(text)
    assert movers.strip_summary("one paragraph only") == "one paragraph only"


# ---- commands --------------------------------------------------------------------------
async def test_credits_and_usage_are_owner_only_and_logged(env, store):
    st = config.load({**env, "OWNER_USER_ID": "5"})
    ctx = Ctx(st, store, ScriptedBackend([]), registry(), FakeBot())
    store.add_usage(-1, "m", "chat", 1, 1000, 500, 20, 0.5)
    stranger = user_msg(ctx.bot, "/usage", user_id=9, mid=1)
    await commands.usage(ctx, update_for(stranger), None)
    assert stranger.replies == [] and store.history(-100, 20)[0].text == "/usage"
    owner = user_msg(ctx.bot, "/usage", user_id=5, mid=2)
    await commands.usage(ctx, update_for(owner), None)
    assert "m: 1 requests, in 1,000 (50% cached)" in owner.replies[0]["text"]
    owner2 = user_msg(ctx.bot, "/credits", user_id=5, mid=3)
    await commands.credits(ctx, update_for(owner2), None)
    assert "no credit balance" in owner2.replies[0]["text"]
    assert commands.usage_text([], "last 24 hours") == "No usage recorded in the last 24 hours."


@pytest.mark.parametrize("cached,tin,pct", [(0, 0, 0), (50, 100, 50)])
def test_usage_text_percentages(cached, tin, pct):
    assert f"({pct}% cached)" in commands.usage_text([("m", 1, tin, cached, 1, 0.0)], "x")
