import re
from pathlib import Path

import pytest

from lib import config


def test_data_dir_holds_the_data_files():
    base = {"TELEGRAM_BOT_TOKEN": "1:x", "XAI_API_KEY": "a"}
    assert config.load(base).db_path == "chat_log.db"
    assert config.load({**base, "DATA_DIR": "/data"}).db_path == "/data/chat_log.db"
    assert config.MCP_CONFIG == config.ROOT / "mcp_servers.json"  # next to bot.py, whatever DATA_DIR is


def test_data_dir_defaults_to_systemd_state_directory():
    base = {"TELEGRAM_BOT_TOKEN": "1:x", "XAI_API_KEY": "a"}
    assert config.load({**base, "STATE_DIRECTORY": "/var/lib/x"}).db_path == "/var/lib/x/chat_log.db"
    assert config.load({**base, "STATE_DIRECTORY": "/a:/b"}).db_path == "/a/chat_log.db"  # first of several
    assert config.load({**base, "STATE_DIRECTORY": "/a", "DATA_DIR": "/data"}).db_path == "/data/chat_log.db"


def test_period_backfill_is_all_by_default_and_can_be_listed_or_off():
    base = {"TELEGRAM_BOT_TOKEN": "1:x", "XAI_API_KEY": "a"}
    assert config.load(base).period_backfill == {"all"}
    assert config.load({**base, "PERIOD_BACKFILL": "off"}).period_backfill == frozenset()
    assert config.load({**base, "PERIOD_BACKFILL": ""}).period_backfill == frozenset()
    assert config.load({**base, "PERIOD_BACKFILL": "RobSMSF, bobsmsf"}).period_backfill == {"robsmsf", "bobsmsf"}
    assert "all" in config.load({**base, "PERIOD_BACKFILL": "all"}).period_backfill


def test_both_keys_refused():
    with pytest.raises(config.ConfigError, match="only one"):
        config.load({"TELEGRAM_BOT_TOKEN": "1:x", "XAI_API_KEY": "a", "OPENROUTER_API_KEY": "b"})


def test_neither_key_refused():
    with pytest.raises(config.ConfigError, match="Set XAI_API_KEY"):
        config.load({"TELEGRAM_BOT_TOKEN": "1:x"})


def test_token_required():
    with pytest.raises(config.ConfigError, match="TELEGRAM_BOT_TOKEN"):
        config.load({"XAI_API_KEY": "a"})


def test_provider_defaults():
    x = config.load({"TELEGRAM_BOT_TOKEN": "1:x", "XAI_API_KEY": "a"})
    assert (x.provider, x.model, x.api_key) == ("xai", "grok-4.3", "a")
    o = config.load({"TELEGRAM_BOT_TOKEN": "1:x", "OPENROUTER_API_KEY": "b"})
    assert (o.provider, o.model) == ("openrouter", "z-ai/glm-5.3-flash")
    z = config.load({"TELEGRAM_BOT_TOKEN": "1:x", "ZAI_API_KEY": "c"})
    assert (z.provider, z.model, z.api_key, z.search) == ("zai", "glm-5.3-flash", "c", True)


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


def test_log_width_defaults_to_95(env):
    assert config.load(env).log_width == 95
    assert config.load({**env, "LOG_WIDTH": "120"}).log_width == 120
    assert config.load({**env, "LOG_WIDTH": "0"}).log_width == 0  # 0 = no limit
    assert config.load({**env, "LOG_WIDTH": ""}).log_width == 95


def test_token_limits():
    assert config.load({"TELEGRAM_BOT_TOKEN": "1:x", "XAI_API_KEY": "a"}).max_tokens == 3000
    assert config.MAX_TOOL_OUTPUT == 12_000 and config.HISTORY_OWN_LINE_MAX < config.HISTORY_LINE_MAX


def test_search_off_without_search_model(env):
    assert config.load(env).search
    assert not config.load({**env, "SEARCH_MODEL": ""}).search
    assert not config.load({**env, "SEARCH": "off"}).search


def test_admin_chats_and_recipients(env):
    s = config.load({**env, "ADMIN_CHAT_IDS": "-1, -2 3", "SHARESIGHT_HOLDING_NEWS_RECIPIENTS": "Bob:@Me"})
    assert s.admin_chats == {-1, -2, 3} and s.holding_news_recipients == {"bob": "me"}


def test_admin_only_is_on_unless_turned_off(env):
    assert not config.load(env).admin_only
    assert config.load({k: v for k, v in env.items() if k != "ADMIN_ONLY"}).admin_only


def test_no_names_in_code_defaults(env):
    s = config.load(env)
    assert s.holding_news_recipients == {} and s.movers_bots == frozenset() and s.portfolio_names == frozenset()


def test_portfolio_names_default_to_the_recipients_portfolios_and_can_be_overridden(env):
    base = {**env, "SHARESIGHT_HOLDING_NEWS_RECIPIENTS": "Bob:me, BobSMSF:me"}
    assert config.load(base).portfolio_names == {"bob", "bobsmsf"}
    assert config.load({**base, "PORTFOLIO_NAMES": "Family Trust, Bob"}).portfolio_names == {"family trust", "bob"}


def test_movers_bots_normalised(env):
    assert config.load({**env, "MOVERS_BOTS": "@FinBot, other"}).movers_bots == {"finbot", "other"}


def test_docs_document_every_env_var():
    doc = (Path(__file__).parent.parent / "docs" / "configuration.md").read_text()
    missing = [n for n in config.ENV_VARS if not re.search(rf"\b{n}\b", doc)]
    assert not missing, f"docs/configuration.md does not mention: {missing}"
