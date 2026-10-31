import json
import logging

import pytest

from lib.features import chat_commands
from lib.features.chat_commands import ChatCommand

from .conftest import user_msg
from .fakes import FakeMcp, step
from .test_handlers import make_ctx, update_for

FORECAST = ChatCommand("forecast", "weather", "get_forecast", require_arg=True, description="Weather forecast")
ALERTS = ChatCommand("alerts", "weather", "get_alerts", description="get_alerts via weather")


def weather(**cfg):
    return FakeMcp("weather", tools=("get_forecast", "get_alerts"), reply="Sunny, 24C", **cfg)


def test_commands_load_from_each_servers_config_and_bad_ones_are_skipped(caplog):
    servers = {
        "weather": {"chat_commands": {"forecast": {"tool": "get_forecast", "require_arg": True,
                                                   "description": "Weather forecast"},
                                      "alerts": {"tool": "get_alerts"},
                                      "Bad-Name": {"tool": "x"}, "tokens": {"tool": "y"}, "notool": {}}},
        "files": {"chat_commands": {"alerts": {"tool": "z"}}},
        "plain": {},
    }
    with caplog.at_level(logging.WARNING, logger="bot"):
        found = chat_commands.load(servers, {"tokens"})
    assert found == [FORECAST, ALERTS]
    for text in ("/Bad-Name isn't a valid command name", "/tokens is one of the bot's own commands",
                 "/notool names no tool", "MCP files: chat command /alerts is already used by another server"):
        assert text in caplog.text


def test_the_text_after_the_command_is_what_was_asked():
    assert chat_commands.args_of("/forecast@stockbot sydney this weekend") == "sydney this weekend"
    assert chat_commands.args_of("/forecast") == "" and chat_commands.args_of("") == ""


def chat(env, store, script, server, *commands, **extra):
    ctx, backend, h = make_ctx(env, store, script, servers=(server,), **extra)
    ctx.chat_commands = list(commands)
    return ctx, backend, h


async def run_command(h, cmd, msg):
    await h.chat_command(cmd)(update_for(msg), None)


async def test_a_command_offers_only_its_tool_requires_a_call_and_answers_from_it(env, store):
    call = [("1", "weather__get_forecast", json.dumps({"place": "sydney"}))]
    server = weather()
    ctx, backend, h = chat(env, store, [step(calls=call, finish="tool_calls"), step("Sunny in Sydney, 24C.")],
                           server, FORECAST, ALERTS)
    m = user_msg(ctx.bot, "/forecast sydney this weekend", chat_id=-100)
    await run_command(h, FORECAST, m)
    first, second = backend.seen
    assert first["tools"] == ["weather__get_forecast"] and first["choice"] == "required" and not first["search"]
    assert second["choice"] is None  # forced on the first round only
    assert 'call weather__get_forecast for what they asked ("sydney this weekend")' in first["prompt"]
    assert server.calls == [("get_forecast", {"place": "sydney"})]
    assert m.replies[0]["text"] == "Sunny in Sydney, 24C."
    assert [r.text for r in store.history(-100, 20)][0] == "/forecast sydney this weekend"  # the command is logged
    assert "typing" in ctx.bot.actions


async def test_a_bare_command_that_needs_an_argument_gets_its_usage_without_the_model(env, store):
    ctx, backend, h = chat(env, store, [], weather(), FORECAST)
    m = user_msg(ctx.bot, "/forecast", chat_id=-100)
    await run_command(h, FORECAST, m)
    assert backend.seen == [] and m.replies[0]["text"] == "Usage: /forecast &lt;what to look up&gt;"
    m = user_msg(ctx.bot, "/alerts", chat_id=-100, mid=2)  # no require_arg: asked as it is
    ctx2, backend2, h2 = chat(env, store, [step("No alerts.")], weather(), ALERTS)
    await run_command(h2, ALERTS, m)
    assert backend2.seen[0]["tools"] == ["weather__get_alerts"] and m.replies[0]["text"] == "No alerts."


@pytest.mark.parametrize("down, tools, said", [
    (True, ("get_forecast",), "The weather data source isn't available right now."),
    (False, ("get_alerts",), "/forecast can't run: the weather tool get_forecast isn't enabled."),
])
async def test_a_command_that_cannot_run_says_why_without_the_model(env, store, down, tools, said):
    server = FakeMcp("weather", tools=tools)
    if down:
        server.session = None
    ctx, backend, h = chat(env, store, [], server, FORECAST)
    m = user_msg(ctx.bot, "/forecast sydney", chat_id=-100)
    await run_command(h, FORECAST, m)
    assert backend.seen == [] and m.replies[0]["text"] == said


async def test_a_portfolio_command_is_for_admins_only(env, store):
    holdings = ChatCommand("holdings", "sharesight", "list_portfolios")
    server = FakeMcp("sharesight", tools=("list_portfolios",), gate="portfolio")
    ctx, backend, h = chat(env, store, [], server, holdings, ADMIN_ONLY="on", ADMIN_CHAT_IDS="99")
    m = user_msg(ctx.bot, "/holdings", chat_id=-100, user_id=5)
    await run_command(h, holdings, m)
    assert backend.seen == [] and m.replies[0]["text"] == "Portfolio data is for admins only."


async def test_a_non_admin_dm_is_ignored_in_admin_only_mode(env, store):
    ctx, backend, h = chat(env, store, [], weather(), ALERTS, ADMIN_ONLY="on", ADMIN_CHAT_IDS="99")
    m = user_msg(ctx.bot, "/alerts", chat_id=5, user_id=5)
    await run_command(h, ALERTS, m)
    assert backend.seen == [] and m.replies == []


async def test_a_failed_forced_call_is_not_answered_from_memory(env, store):
    ctx, backend, h = chat(env, store, [RuntimeError("empty reply (finish_reason=stop)"), step("from memory")],
                           weather(), ALERTS)
    m = user_msg(ctx.bot, "/alerts", chat_id=-100)
    await run_command(h, ALERTS, m)
    assert len(backend.seen) == 1 and "went wrong" in m.replies[0]["text"]  # no second try without tools
