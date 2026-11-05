import json
from datetime import datetime
from types import SimpleNamespace as N
from zoneinfo import ZoneInfo

import pytest
from telegram.error import Forbidden

from lib import config
from lib.context import Ctx
from lib.features import dm_buttons, holding_news, movers
from lib.llm.base import Overloaded
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
            return json.dumps({"portfolios": [{"name": "Bob", "id": 7}]})
        cols = ["code", "market", "name"]
        rows = [["NVDA", "NASDAQ", "Nvidia"], ["AMD", "NASDAQ", "AMD"]]
        return json.dumps({"report": {"holdings": {"columns": cols, "rows": rows}}})


def holding_ctx(env, store, script):
    st = config.load({**env, "SHARESIGHT_HOLDING_NEWS_RECIPIENTS": "Bob:bob_llama"})
    ss = Sharesight("sharesight", tools=("list_portfolios",))
    ctx = Ctx(st, store, ScriptedBackend(script), registry(ss), FakeBot())
    store.remember_user(make_msg(1, "x", username="bob_llama", chat_id=-1))
    return ctx


async def test_daily_digest_dms_news_with_links_and_records_it(env, store):
    ctx = holding_ctx(env, store, [step("• NVDA (NASDAQ): beat estimates https://x.com/a")])
    results = await holding_news.run_holding_news(ctx)
    text, codes = results["bob_llama"]
    assert codes == ["NVDA"] and 'quote/NVDA"' in text
    sent = ctx.bot.sent[-1]
    assert sent["chat_id"] == 5 and "Holding news" in sent["text"] and sent["reply_markup"] is not None
    assert store.recent_news("bob_llama", 0) == [text]
    assert ctx.backend.seen[0]["tools"] == [] and ctx.backend.seen[0]["search"] is True  # forced search only


async def test_digest_links_held_codes_however_the_model_writes_them(env, store):
    ctx = holding_ctx(env, store, [step("• Nvidia (NVDA): beat estimates\n• AMD fell, GAAP loss")])
    text, codes = (await holding_news.run_holding_news(ctx))["bob_llama"]
    assert '<b>Nvidia (<a href="https://finance.yahoo.com/quote/NVDA">NVDA</a>)</b>' in text
    assert 'quote/AMD"' in text and "quote/GAAP" not in text and codes == ["AMD", "NVDA"]


def test_known_symbols_and_listed_codes_give_yahoo_symbols():
    held = {"NXT (ASX)": "NextDC", "NVDA (NASDAQ)": "Nvidia", "X (MOON)": "x"}
    assert holding_news.known_symbols(held) == {"NXT": "NXT.AX", "NVDA": "NVDA", "X": "X"}
    hk = {"700 (HKG)": "Tencent"}
    news = holding_news.link_tickers("• Tencent (700): profit up; 1,700 staff", known=holding_news.known_symbols(hk))
    assert news.count("quote/0700.HK") == 1 and holding_news.news_codes(news, hk) == ["700"]  # not "1,700"
    assert movers.listed_codes("<b>≥ 5.0% at close (ASX):</b>\nDRO +12.1%\nNXT +5.5%") == {"DRO": "DRO.AX", "NXT": "NXT.AX"}
    assert movers.listed_codes("<b>≤ -5% at close (NASDAQ, NYSE):</b>\nNVDA -6.1%") == {"NVDA": "NVDA"}
    assert movers.listed_codes("<b>≥ 5% at close (ASX, NYSE):</b>\nBHP +6%") == {}  # which exchange? no guess
    assert movers.listed_codes("<b>≥ 5% at close:</b>\nBHP +6%") == {}


class TwoPortfolios(FakeMcp):
    async def call(self, tool, args):
        if tool == "list_portfolios":
            return json.dumps({"portfolios": [{"name": "Bob", "id": 1}, {"name": "Alice", "id": 2}]})
        rows = {1: [["NVDA", "NASDAQ", "Nvidia"], ["AMD", "NASDAQ", "AMD"]],
                2: [["NVDA", "NASDAQ", "Nvidia"], ["BHP", "ASX", "BHP Group"]]}[args["portfolio_id"]]
        return json.dumps({"report": {"holdings": {"columns": ["code", "market", "name"], "rows": rows}}})


async def test_one_search_covers_everyone_and_each_person_gets_their_own_items(env, store):
    st = config.load({**env, "SHARESIGHT_HOLDING_NEWS_RECIPIENTS": "Bob:bob_llama,Alice:alice_llama"})
    news = ("• Nvidia (NVDA): beat estimates\n• AMD fell on a guidance cut\n• BHP (ASX): trading halt\n"
            "• Chip export rules tightened")
    ctx = Ctx(st, store, ScriptedBackend([step(news)]), registry(TwoPortfolios("sharesight", tools=("x",))), FakeBot())
    store.remember_user(make_msg(1, "x", username="bob_llama", chat_id=5))
    store.remember_user(make_msg(2, "x", username="alice_llama", chat_id=6))
    store.add_news("alice_llama", "• BHP (ASX): an older item")
    results = await holding_news.run_holding_news(ctx)
    assert len(ctx.backend.seen) == 1  # NVDA, which both hold, is searched once, in one request
    prompt = ctx.backend.seen[0]["prompt"]
    assert prompt.count("NVDA (NASDAQ)") == 1 and "AMD (NASDAQ)" in prompt and "BHP (ASX)" in prompt
    assert "an older item" in prompt  # anyone's recent items aren't repeated
    bob, bob_codes = results["bob_llama"]
    alice, alice_codes = results["alice_llama"]
    assert "NVDA" in bob and "AMD" in bob and "BHP" not in bob and bob_codes == ["AMD", "NVDA"]
    assert "NVDA" in alice and "AMD fell" not in alice and "BHP" in alice and alice_codes == ["BHP", "NVDA"]
    assert "export rules" in bob and "export rules" in alice  # names no holding: everyone gets it
    assert {m["chat_id"] for m in ctx.bot.sent} == {5, 6}


def test_funds_and_notes_about_the_search_are_left_out():
    from lib.features.holding_news.text import drop_notes, is_fund
    for name in ("VanEck Semiconductor ETF", "Betashares Nasdaq 100 ETF", "GraniteShares 2x Long NVDA Daily ETF",
                 "Global X Physical Silver", "iShares Core S&P 500", "Roundhill Magnificent Seven ETF"):
        assert is_fund(name), name
    for name in ("Nvidia", "NextDC", "Goodman Group", "Commonwealth Bank of Australia", "Rocket Lab"):
        assert not is_fund(name), name
    news = ("• CrowdStrike (CRWD): agreed to buy Fiddler AI\n"
            "• Note: my search budget ran out after the first pass, so coverage is incomplete.\n"
            "• <b>Coverage</b> of the smaller names was thin")
    assert drop_notes(news) == "• CrowdStrike (CRWD): agreed to buy Fiddler AI"


async def test_the_daily_check_skips_funds_and_asks_for_a_wide_search(env, store):
    class WithEtf(Sharesight):
        async def call(self, tool, args):
            if tool == "list_portfolios":
                return await super().call(tool, args)
            rows = [["NVDA", "NASDAQ", "Nvidia"], ["SMH", "NASDAQ", "VanEck Semiconductor ETF"]]
            return json.dumps({"report": {"holdings": {"columns": ["code", "market", "name"], "rows": rows}}})

    st = config.load({**env, "SHARESIGHT_HOLDING_NEWS_RECIPIENTS": "Bob:bob_llama"})
    seen = []

    class Backend(ScriptedBackend):
        async def step(self, conv, req, *, tool_choice):
            seen.append(req.search_results)
            return await super().step(conv, req, tool_choice=tool_choice)

    ctx = Ctx(st, store, Backend([step("NOTHING")]), registry(WithEtf("sharesight", tools=("list_portfolios",))),
              FakeBot())
    await holding_news.run_holding_news(ctx)
    assert "NVDA (NASDAQ)" in ctx.backend.seen[0]["prompt"] and "SMH" not in ctx.backend.seen[0]["prompt"]
    assert "no notes about your search" in ctx.backend.seen[0]["prompt"]
    assert seen == [config.HOLDING_NEWS_SEARCH_RESULTS]


async def test_digest_says_nothing_when_nothing_and_respects_mutes(env, store):
    ctx = holding_ctx(env, store, [step("NOTHING")])
    assert (await holding_news.run_holding_news(ctx)) == {"bob_llama": None} and ctx.bot.sent == []
    store.set_muted("bob_llama", "*", True)
    assert await holding_news.run_holding_news(ctx) == {}
    store.set_muted("bob_llama", "*", False)
    store.set_muted("bob_llama", "NVDA", True)
    ctx.backend.script = [step("• AMD: news")]
    await holding_news.run_holding_news(ctx)
    prompt = ctx.backend.seen[-1]["prompt"]
    assert "AMD (NASDAQ)" in prompt and "NVDA (NASDAQ)" not in prompt  # muted code left out


async def test_busy_provider_is_retried_for_just_that_person_then_delivered(env, store, monkeypatch, caplog):
    monkeypatch.setattr(config, "HOLDING_NEWS_RETRY_EVERY", 0)
    ctx = holding_ctx(env, store, [Overloaded("at capacity"), Overloaded("at capacity"), step("• NVDA: big news")])
    busy: list[str] = []
    assert await holding_news.run_holding_news(ctx, busy=busy) == {} and busy == ["bob_llama"]
    assert ctx.bot.sent == []
    await holding_news.retry_busy(ctx, busy)  # still busy once, then works
    assert len(ctx.backend.seen) == 3 and "Holding news" in ctx.bot.sent[-1]["text"]
    assert not any(r.exc_info for r in caplog.records)


def test_the_window_reaches_back_to_the_last_check():
    from lib.features.holding_news.text import window_hours
    now = 1_000_000_000
    assert window_hours(now, None) == 24 and window_hours(now, now - 2 * 3600) == 24  # a day at least
    assert window_hours(now, now - 24 * 3600) == 24 and window_hours(now, now - 72 * 3600 + 60) == 72
    assert window_hours(now, now - 30 * 86400) == config.HOLDING_NEWS_MAX_WINDOW == 168


def test_an_item_citing_an_article_already_sent_is_dropped():
    from lib.features.holding_news.text import drop_repeats
    sent = ['• <a href="https://finance.yahoo.com/quote/NVDA">NVDA</a>: beat <a href="https://x.com/a">[1]</a>']
    new = ('• <a href="https://finance.yahoo.com/quote/NVDA">NVDA</a>: beat again <a href="https://x.com/a">[1]</a>\n'
           '• <a href="https://finance.yahoo.com/quote/NVDA">NVDA</a>: guidance raised <a href="https://x.com/b">[2]</a>\n'
           '• AMD fell')
    kept, dropped = drop_repeats(new, sent)
    assert "x.com/a" not in kept and "x.com/b" in kept and "AMD fell" in kept and len(dropped) == 1
    assert drop_repeats(new, []) == (new, [])  # the quote link on NVDA doesn't count as an article


def test_skip_days_parse_names_and_abbreviations(env):
    assert config.load({**env, "SHARESIGHT_HOLDING_NEWS_SKIP_DAYS": "sun, Monday"}).holding_news_skip == {6, 0}
    assert config.load(env).holding_news_skip == frozenset()
    with pytest.raises(config.ConfigError, match="funday"):
        config.load({**env, "SHARESIGHT_HOLDING_NEWS_SKIP_DAYS": "funday"})


async def test_after_skipped_days_the_check_covers_them_and_a_busy_one_keeps_the_window(env, store):
    import time
    ctx = holding_ctx(env, store, [Overloaded("at capacity"), step("• NVDA (NASDAQ): halted https://x.com/h")])
    store.kv_set(holding_news.LAST_RUN_KEY, str(int(time.time()) - 72 * 3600 + 60))  # Saturday's, now it's Tuesday
    await holding_news.run_holding_news(ctx, busy=[])
    assert int(store.kv_get(holding_news.LAST_RUN_KEY)) < time.time() - 71 * 3600  # busy: the window stays open
    await holding_news.run_holding_news(ctx)
    assert "past 72 hours" in ctx.backend.seen[-1]["prompt"] and "(past 72h)" in ctx.bot.sent[-1]["text"]
    assert int(store.kv_get(holding_news.LAST_RUN_KEY)) >= time.time() - 5  # answered: the next window starts now


async def test_the_daily_loop_skips_its_days(env, store, monkeypatch, caplog):
    import asyncio
    import logging
    st = config.load({**env, "SHARESIGHT_HOLDING_NEWS_RECIPIENTS": "Bob:bob_llama",
                      "SHARESIGHT_HOLDING_NEWS_SKIP_DAYS": "sun"})
    ctx = Ctx(st, store, ScriptedBackend([]), registry(), FakeBot())
    sunday = datetime(2026, 11, 8, 8, 0, tzinfo=st.tz)
    ran, sleeps = [], []

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return sunday

    async def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) > 2:
            raise asyncio.CancelledError

    async def run(ctx, **kw):
        ran.append(kw)
        return {}

    monkeypatch.setattr(holding_news, "datetime", Clock)
    monkeypatch.setattr(holding_news.asyncio, "sleep", sleep)
    monkeypatch.setattr(holding_news, "run_holding_news", run)
    with caplog.at_level(logging.INFO, logger="bot"), pytest.raises(asyncio.CancelledError):
        await holding_news.daily_loop(ctx)
    assert ran == [] and "Holding news: skipped (Sunday)" in caplog.text


async def test_busy_provider_is_given_up_on_after_the_retry_window(env, store, monkeypatch, caplog):
    monkeypatch.setattr(config, "HOLDING_NEWS_RETRY_EVERY", 4 * 3600)
    monkeypatch.setattr(config, "HOLDING_NEWS_RETRY_FOR", 12 * 3600)
    slept = []

    async def no_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(holding_news.asyncio, "sleep", no_sleep)
    ctx = holding_ctx(env, store, [Overloaded("at capacity")] * 4)
    await holding_news.retry_busy(ctx, ["bob_llama"])
    assert slept == [4 * 3600] * 3 and len(ctx.backend.seen) == 3 and ctx.bot.sent == []  # 12 h / 4 h
    assert "gave up on @bob_llama after 12 hours" in caplog.text


async def test_digest_skips_people_the_bot_cannot_reach(env, store):
    ctx = holding_ctx(env, store, [step("• NVDA: big news")])
    ctx.bot.fail_send[5] = Forbidden("bot was blocked")
    await holding_news.run_holding_news(ctx)
    assert store.recent_news("bob_llama", 0) == []


async def test_mute_and_undo_buttons(env, store):
    ctx = holding_ctx(env, store, [])
    answers, edits = [], []

    async def answer(text=None):
        answers.append(text)

    async def edit(markup):
        edits.append(markup)

    def query(data, username="bob_llama"):
        return N(data=data, from_user=N(username=username), answer=answer, edit_message_reply_markup=edit,
                 message=N(chat_id=5, message_id=9))

    store.remember_news_message(5, 9, ["NVDA", "AMD"])
    await holding_news.on_button(ctx, N(callback_query=query("hn:menu")), None)
    assert [b.callback_data for r in edits[-1].inline_keyboard for b in r][:2] == ["hn:mute:NVDA", "hn:mute:AMD"]
    await holding_news.on_button(ctx, N(callback_query=query("hn:mute:NVDA")), None)
    assert store.muted_codes("bob_llama") == {"NVDA"} and "Unsubscribed from NVDA news" in answers[-1]
    await holding_news.on_button(ctx, N(callback_query=query("hn:undo:NVDA")), None)
    assert store.muted_codes("bob_llama") == set()
    await holding_news.on_button(ctx, N(callback_query=query("hn:menu", "stranger")), None)
    assert "not set up" in answers[-1]


async def test_holdingnews_command_always_replies(env, store):
    ctx = holding_ctx(env, store, [step("NOTHING")])
    m = user_msg(ctx.bot, "/holdingnews", chat_id=5, user_id=5)
    m.from_user.username = "bob_llama"
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
async def test_credits_shows_balance_and_usage_to_admins_only(env, store):
    st = config.load({**env, "TELEGRAM_ADMIN_CHAT_IDS": "5, -100"})
    ctx = Ctx(st, store, ScriptedBackend([]), registry(), FakeBot())
    store.add_usage(-1, "m", "chat", 1, 1000, 500, 20, 0.5)
    stranger = user_msg(ctx.bot, "/credits", user_id=9, mid=1)
    await commands.credits(ctx, update_for(stranger), None)
    assert stranger.replies == [] and store.history(-100, 20)[0].text == "/credits"
    owner = user_msg(ctx.bot, "/credits", user_id=5, mid=2)
    await commands.credits(ctx, update_for(owner), None)
    text = owner.replies[0]["text"]
    assert "no credit balance" in text and "m: 1 requests, in 1,000 (50% cached)" in text
    assert commands.usage_text([], "last 24 hours") == "No usage recorded in the last 24 hours."
    assert "By kind" not in text  # one kind only: nothing to split
    store.add_usage(5, "m", "holding-news", 1, 100, 0, 10, 0.25)
    again = user_msg(ctx.bot, "/credits", user_id=5, mid=3)
    await commands.credits(ctx, update_for(again), None)
    assert "By kind: chat 1 ($0.5000), holding news (daily) 1 ($0.2500)" in again.replies[0]["text"]


async def test_credits_still_shows_usage_when_the_balance_fails(env, store):
    class Broken(ScriptedBackend):
        async def credits(self):
            raise RuntimeError("boom")

    ctx = Ctx(config.load({**env, "TELEGRAM_ADMIN_CHAT_IDS": "5"}), store, Broken([]), registry(), FakeBot())
    store.add_usage(-1, "m", "chat", 1, 10, 0, 2, 0.1)
    msg = user_msg(ctx.bot, "/credits", user_id=5)
    await commands.credits(ctx, update_for(msg), None)
    assert "Couldn't fetch credits: boom" in msg.replies[0]["text"] and "m: 1 requests" in msg.replies[0]["text"]


async def test_group_admin_can_run_credits_in_the_group_and_in_a_dm(env, store):
    ctx = Ctx(config.load(env), store, ScriptedBackend([]), registry(), FakeBot())
    store.remember_chat(-100, "Stocks")
    ctx.bot.admins[-100] = [7]
    for chat_id, mid in ((-100, 1), (7, 2)):
        msg = user_msg(ctx.bot, "/credits", user_id=7, chat_id=chat_id, mid=mid)
        await commands.credits(ctx, update_for(msg), None)
        assert "Usage" in msg.replies[0]["text"] or "No usage" in msg.replies[0]["text"]
    other = user_msg(ctx.bot, "/credits", user_id=8, mid=3)
    await commands.credits(ctx, update_for(other), None)
    assert other.replies == []


@pytest.mark.parametrize("cached,tin,pct", [(0, 0, 0), (50, 100, 50)])
def test_usage_text_percentages(cached, tin, pct):
    assert f"({pct}% cached)" in commands.usage_text([("m", 1, tin, cached, 1, 0.0)], "x")


async def test_flushcache_command_is_admin_only_and_flushes_by_portfolio_and_period(env, store):
    from lib.features.periods import backfill
    from lib.store import PeriodRow

    st = config.load({**env, "TELEGRAM_ADMIN_CHAT_IDS": "5", "PERIOD_BACKFILL": "x"})
    ctx = Ctx(st, store, ScriptedBackend([]), registry(), FakeBot())
    store.period_put([PeriodRow(p, f"{y}-0{m}-01", f"{y}-0{m}-28", 1.0, 1.0, "AUD", [])
                      for p in (70007, 80008) for y in (2024, 2025) for m in (1, 2)])
    count = lambda p: store.period_count(p, "2000-01-01", "2100-01-01")  # noqa: E731
    stranger = user_msg(ctx.bot, "/flushcache", user_id=9, mid=1)
    await commands.flush_cache(ctx, update_for(stranger), N(args=[]))
    assert stranger.replies == [] and count(70007) == 4
    year = user_msg(ctx.bot, "/flushcache 70007 2024", user_id=5, mid=2)
    await commands.flush_cache(ctx, update_for(year), N(args=["70007", "2024"]))
    assert "Flushed 2 cached periods (portfolio 70007, 2024-01-01 to 2024-12-31). Refilling" in year.replies[0]["text"]
    assert count(70007) == 2 and count(80008) == 4 and backfill.wake_event().is_set()
    month = user_msg(ctx.bot, "/flushcache 2025-01", user_id=5, mid=3)
    await commands.flush_cache(ctx, update_for(month), N(args=["2025-01"]))
    assert "Flushed 2 cached periods (all portfolios, 2025-01-01 to 2025-01-31)" in month.replies[0]["text"]
    bad = user_msg(ctx.bot, "/flushcache x", user_id=5, mid=4)
    await commands.flush_cache(ctx, update_for(bad), N(args=["x"]))
    assert "Usage" in bad.replies[0]["text"]
    every = user_msg(ctx.bot, "/flushcache", user_id=5, mid=5)
    await commands.flush_cache(ctx, update_for(every), N(args=[]))
    assert "Flushed 4 cached periods (all portfolios)" in every.replies[0]["text"]


async def test_holdingnews_command_says_when_the_provider_is_busy(env, store):
    ctx = holding_ctx(env, store, [Overloaded("at capacity")])
    m = user_msg(ctx.bot, "/holdingnews", chat_id=5, user_id=5)
    m.from_user.username = "bob_llama"
    await holding_news.on_command(ctx, m)
    assert "at capacity" in m.replies[-1]["text"] and "No major news" not in m.replies[-1]["text"]
