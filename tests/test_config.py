import re
from pathlib import Path

import pytest

from tgbot import config


def test_both_keys_refused():
    with pytest.raises(config.ConfigError, match="Both"):
        config.load({"TELEGRAM_BOT_TOKEN": "1:x", "XAI_API_KEY": "a", "OPENROUTER_API_KEY": "b"})


def test_neither_key_refused():
    with pytest.raises(config.ConfigError, match="Set XAI_API_KEY"):
        config.load({"TELEGRAM_BOT_TOKEN": "1:x"})


def test_token_required():
    with pytest.raises(config.ConfigError, match="TELEGRAM_BOT_TOKEN"):
        config.load({"XAI_API_KEY": "a"})


def test_provider_defaults():
    x = config.load({"TELEGRAM_BOT_TOKEN": "1:x", "XAI_API_KEY": "a"})
    assert (x.provider, x.model, x.api_key) == ("xai", "grok-4.7", "a")
    o = config.load({"TELEGRAM_BOT_TOKEN": "1:x", "OPENROUTER_API_KEY": "b"})
    assert (o.provider, o.model) == ("openrouter", "xiaomi/mimo-v2.6-pro")


def test_model_and_reasoning(env):
    s = config.load({**env, "MODEL": "m/x", "REASONING": "Low", "GROK_MODEL": "ignored"})
    assert (s.model, s.reasoning) == ("m/x", "low")


def test_features_off_by_default(settings):
    assert not (settings.movers_explain or settings.dm_buttons or settings.holding_news)


def test_holding_news_and_movers_switch_on_by_listing_names(env):
    on = config.load({**env, "SHARESIGHT_HOLDING_NEWS_RECIPIENTS": "Pf:alice", "MOVERS_BOTS": "otherbot"})
    assert on.holding_news and on.movers_explain
    assert not config.load({**env, "SHARESIGHT_HOLDING_NEWS_RECIPIENTS": "", "MOVERS_BOTS": " "}).holding_news
    assert not config.load({**env, "MOVERS_BOTS": ","}).movers_explain


def test_token_saver_switches_defaults(env):
    on, off = config.load(env), config.load({**env, "TOKEN_SAVER": "off"})
    assert (on.max_tokens, off.max_tokens) == (1500, 4000)
    assert on.max_tool_output < off.max_tool_output
    assert config.load({**env, "FAST_MODEL": "f"}).fast_model == "f"
    assert config.load({**env, "FAST_MODEL": "f", "TOKEN_SAVER": "off"}).fast_model == ""


def test_search_off_without_search_model(env):
    assert config.load(env).search
    assert not config.load({**env, "SEARCH_MODEL": ""}).search
    assert not config.load({**env, "SEARCH": "off"}).search


def test_alert_chats_and_recipients(env):
    s = config.load({**env, "ALERT_CHAT_IDS": "-1, -2 3", "SHARESIGHT_HOLDING_NEWS_RECIPIENTS": "Rob:@Me"})
    assert s.alert_chats == {-1, -2, 3} and s.holding_news_recipients == {"rob": "me"}


def test_no_names_in_code_defaults(env):
    s = config.load(env)
    assert s.holding_news_recipients == {} and s.movers_bots == frozenset() and s.portfolio_names == frozenset()


def test_portfolio_names_default_to_the_recipients_portfolios_and_can_be_overridden(env):
    base = {**env, "SHARESIGHT_HOLDING_NEWS_RECIPIENTS": "Rob:me, RobSMSF:me"}
    assert config.load(base).portfolio_names == {"rob", "robsmsf"}
    assert config.load({**base, "PORTFOLIO_NAMES": "Family Trust, Rob"}).portfolio_names == {"family trust", "rob"}


def test_movers_bots_normalised(env):
    assert config.load({**env, "MOVERS_BOTS": "@FinBot, other"}).movers_bots == {"finbot", "other"}


def test_readme_documents_every_env_var():
    readme = (Path(__file__).parent.parent / "README.md").read_text()
    missing = [n for n in config.ENV_VARS if not re.search(rf"\b{n}\b", readme)]
    assert not missing, f"README.md does not mention: {missing}"
