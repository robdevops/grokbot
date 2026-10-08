import time

from telegram.error import TelegramError

from lib import config
from lib.context import Ctx
from lib.telegram import access

from .conftest import FakeBot
from .fakes import ScriptedBackend, registry


def make_ctx(env, store, **extra):
    return Ctx(config.load({**env, **extra}), store, ScriptedBackend([]), registry(), FakeBot())


async def test_listed_user_is_admin_without_any_lookup(env, store):
    ctx = make_ctx(env, store, ADMIN_CHAT_IDS="5, -100")
    assert await access.is_admin(ctx, 5) and not await access.is_admin(ctx, 6)


async def test_admin_of_a_known_group_is_admin(env, store):
    ctx = make_ctx(env, store)
    store.remember_chat(-1, "One")
    store.remember_chat(-2, "Two")
    ctx.bot.admins = {-1: [3], -2: [4]}
    assert await access.is_admin(ctx, 4) and not await access.is_admin(ctx, 9)


async def test_unknown_groups_and_unreadable_groups_grant_nothing(env, store):
    ctx = make_ctx(env, store)
    ctx.bot.admins = {-1: [3]}  # the bot has never seen group -1
    assert not await access.is_admin(ctx, 3)
    store.remember_chat(-2, "Gone")  # known, but the bot was removed (Telegram raises)
    assert not await access.is_admin(ctx, 3)


async def test_admin_lists_are_cached_then_refreshed(env, store, monkeypatch):
    ctx = make_ctx(env, store)
    store.remember_chat(-1, "One")
    ctx.bot.admins = {-1: [3]}
    assert await access.is_admin(ctx, 3)
    ctx.bot.admins = {-1: []}  # demoted
    assert await access.is_admin(ctx, 3)  # still cached
    now = time.monotonic()
    monkeypatch.setattr(access.time, "monotonic", lambda: now + access.CACHE_SECONDS + 1)
    assert not await access.is_admin(ctx, 3)


async def test_telegram_error_is_not_admin_and_is_cached(env, store):
    ctx = make_ctx(env, store)
    store.remember_chat(-1, "One")
    calls = []

    async def failing(chat_id):
        calls.append(chat_id)
        raise TelegramError("kicked")

    ctx.bot.get_chat_administrators = failing
    assert not await access.is_admin(ctx, 3) and not await access.is_admin(ctx, 4)
    assert calls == [-1]
