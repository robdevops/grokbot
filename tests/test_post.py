import json
from types import SimpleNamespace as N

from lib import config
from lib.context import Ctx
from lib.features import post

from . import test_app
from .conftest import FakeBot, user_msg
from .fakes import ScriptedBackend, registry, step
from .test_handlers import make_ctx, run

GROUP, OTHER, ME = -1001, -1002, 5467


def test_store_remembers_renames_and_forgets_chats(store):
    store.remember_chat(GROUP, "Finance Alliance")
    store.remember_chat(GROUP, "Finance Alliance 2")
    store.remember_chat(OTHER, "Chess")
    assert sorted(store.chats()) == sorted([(GROUP, "Finance Alliance 2"), (OTHER, "Chess")])
    store.forget_chat(OTHER)
    assert store.chats() == [(GROUP, "Finance Alliance 2")]


def test_wants_post_needs_a_posting_verb_and_a_group_word():
    assert post.wants_post("say hello in the finance alliance channel")
    assert post.wants_post("Tell the Chess group we start at 7")
    assert not post.wants_post("how is NVDA doing?")
    assert not post.wants_post("say hello")


def test_find_groups_ignores_case_emoji_and_filler_then_tries_close_matches():
    chats = [(GROUP, "💹 Finance Alliance"), (OTHER, "Chess Club"), (-3, "Finance Daily")]
    assert post.find_groups(chats, "the finance alliance channel") == [(GROUP, "💹 Finance Alliance")]
    assert [i for i, _ in post.find_groups(chats, "finance")] == [GROUP, -3]
    assert post.find_groups(chats, "finanse alliance") == [(GROUP, "💹 Finance Alliance")]
    assert post.find_groups(chats, "the group") == [] and post.find_groups(chats, "poker") == []


async def run_tool(env, store, args, *, admins=None, chats=None, user=ME, name=""):
    ctx = Ctx(config.load({**env, "POST_TO_GROUPS_FROM_DM": "on"}), store, ScriptedBackend([]),
              registry(), FakeBot())
    for chat_id, title in (chats if chats is not None else [(GROUP, "Finance Alliance")]):
        store.remember_chat(chat_id, title)
    ctx.bot.admins = {GROUP: [ME, 9]} if admins is None else admins
    _, send = post.tool(ctx, user, name)
    return await send(args), ctx


async def test_tool_posts_only_for_an_admin_of_that_group(env, store):
    out, ctx = await run_tool(env, store, {"group": "finance alliance", "text": "hello <all> & co"})
    assert out == "Posted in Finance Alliance."
    assert ctx.bot.sent[0]["chat_id"] == GROUP and ctx.bot.sent[0]["text"] == "hello &lt;all&gt; &amp; co"
    assert store.history(GROUP, 5)[0].text == "hello &lt;all&gt; &amp; co"  # the bot's own post is logged


async def test_tool_refuses_non_admins_unknown_groups_and_unreadable_groups_the_same_way(env, store):
    not_admin, ctx1 = await run_tool(env, store, {"group": "finance", "text": "hi"}, user=77)
    unknown, ctx2 = await run_tool(env, store, {"group": "poker", "text": "hi"})
    unreadable, ctx3 = await run_tool(env, store, {"group": "finance", "text": "hi"}, admins={})
    assert not_admin == "I don't know a group called 'finance' that you administer."
    assert unknown == "I don't know a group called 'poker' that you administer."
    assert unreadable == not_admin
    assert not (ctx1.bot.sent or ctx2.bot.sent or ctx3.bot.sent)


async def test_tool_asks_which_when_several_of_your_groups_match(env, store):
    out, ctx = await run_tool(env, store, {"group": "finance", "text": "hi"},
                              chats=[(GROUP, "Finance Alliance"), (OTHER, "Finance Daily")],
                              admins={GROUP: [ME], OTHER: [ME]})
    assert out.startswith("Several of your groups match: ") and "Finance Daily" in out and not ctx.bot.sent
    only, _ = await run_tool(env, store, {"group": "finance", "text": "hi"},
                             chats=[(GROUP, "Finance Alliance"), (OTHER, "Finance Daily")],
                             admins={GROUP: [ME], OTHER: [1]})
    assert only == "Posted in Finance Alliance."


async def test_tool_rejects_empty_or_too_long_text_and_reports_send_failures(env, store):
    assert (await run_tool(env, store, {"group": "finance", "text": " "}))[0].startswith("Error")
    long, ctx = await run_tool(env, store, {"group": "finance", "text": "x" * 5000})
    assert "limit is" in long and not ctx.bot.sent
    ctx = Ctx(config.load({**env, "POST_TO_GROUPS_FROM_DM": "on"}), store, ScriptedBackend([]),
              registry(), FakeBot())
    store.remember_chat(GROUP, "Finance Alliance")
    ctx.bot.admins = {GROUP: [ME]}
    ctx.bot.fail_send[GROUP] = post.TelegramError("Forbidden: bot was kicked")
    out = await post.tool(ctx, ME)[1]({"group": "finance", "text": "hi"})
    assert out.startswith("Couldn't post in Finance Alliance")


CALL = [("1", "send_to_group", json.dumps({"group": "finance alliance", "text": "Hello everyone"}))]


async def test_dm_request_offers_the_tool_and_posts_in_the_group(env, store):
    ctx, backend, h = make_ctx(env, store, [step(calls=CALL, finish="tool_calls"), step("Done.")],
                               POST_TO_GROUPS_FROM_DM="on")
    store.remember_chat(GROUP, "Finance Alliance")
    ctx.bot.admins = {GROUP: [ME]}
    m = user_msg(ctx.bot, "say hello in the finance alliance channel", chat_id=ME, user_id=ME)
    await run(h, m)
    assert backend.seen[0]["tools"] == ["send_to_group"] and "no live data tools" not in backend.seen[0]["system"]
    assert [s["chat_id"] for s in ctx.bot.sent] == [GROUP] and ctx.bot.sent[0]["text"] == "Hello everyone"
    assert m.replies[0]["text"] == "Done."


async def test_flag_off_or_a_group_chat_or_a_normal_message_gets_no_post_tool(env, store):
    ctx, backend, h = make_ctx(env, store, [step("can't")])
    await run(h, user_msg(ctx.bot, "say hello in the finance alliance channel", chat_id=ME, user_id=ME))
    assert backend.seen[0]["tools"] == []
    ctx, backend, h = make_ctx(env, store, [step("ok"), step("ok")], POST_TO_GROUPS_FROM_DM="on")
    await run(h, user_msg(ctx.bot, "@stockbot say hello in the chess group", mid=2))  # in a group
    await run(h, user_msg(ctx.bot, "how is the weather", chat_id=ME, user_id=ME, mid=3))
    assert all("send_to_group" not in s["tools"] for s in backend.seen)


async def test_groups_are_remembered_from_messages_only_when_the_flag_is_on(env, store):
    ctx, backend, h = make_ctx(env, store, [], POST_TO_GROUPS_FROM_DM="on")
    await run(h, user_msg(ctx.bot, "hello all", chat_id=GROUP, chat_title="Finance Alliance"))
    await run(h, user_msg(ctx.bot, "hi", chat_id=ME, user_id=ME, mid=2))  # a DM is not a group
    assert store.chats() == [(GROUP, "Finance Alliance")]
    ctx2, _, h2 = make_ctx(env, store, [])
    await run(h2, user_msg(ctx2.bot, "hello", chat_id=OTHER, chat_title="Chess", mid=3))
    assert store.chats() == [(GROUP, "Finance Alliance")]


async def test_membership_updates_add_and_remove_groups(env, store):
    ctx = Ctx(config.load({**env, "POST_TO_GROUPS_FROM_DM": "on"}), store, ScriptedBackend([]), registry(), FakeBot())

    def change(chat_id, kind, status, title="Chess"):
        return N(my_chat_member=N(chat=N(id=chat_id, type=kind, title=title), new_chat_member=N(status=status)))

    await post.on_membership(ctx, change(OTHER, "supergroup", "member"), None)
    await post.on_membership(ctx, change(-5, "channel", "administrator"), None)  # channels are ignored
    assert store.chats() == [(OTHER, "Chess")]
    await post.on_membership(ctx, change(OTHER, "supergroup", "kicked"), None)
    assert store.chats() == []


def test_app_listens_for_membership_changes_only_with_the_flag(env, store):
    def build(**extra):
        st = config.load({**env, **extra})
        return test_app.app.build_app(Ctx(st, store, ScriptedBackend([]), registry())), st

    plain, st = build()
    assert "ChatMemberHandler" not in test_app.handler_kinds(plain) and "my_chat_member" not in test_app.app.allowed_updates(st)
    on, st = build(POST_TO_GROUPS_FROM_DM="on")
    assert "ChatMemberHandler" in test_app.handler_kinds(on) and "my_chat_member" in test_app.app.allowed_updates(st)


def test_tool_description_tells_the_model_to_post_as_itself():
    assert "never say or hint that someone asked" in post.TOOL.description


async def test_a_post_that_names_the_requester_is_refused_until_reworded(env, store):
    args = {"group": "finance", "text": "Rob asked me to say hello"}
    out, ctx = await run_tool(env, store, args, name="Rob Smith")
    assert out.startswith("Error: the text names the person who asked") and not ctx.bot.sent
    ok, ctx = await run_tool(env, store, {"group": "finance", "text": "Hello everyone, robust week!"}, name="Rob Smith")
    assert ok == "Posted in Finance Alliance." and ctx.bot.sent[0]["text"] == "Hello everyone, robust week!"
