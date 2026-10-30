"""Settings parsed once from the environment; the only module that knows which provider is in use."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

# Every environment variable the bot reads, with a one-line description. docs/configuration.md must
# mention each name (tests/test_config.py enforces it).
ENV_VARS: dict[str, str] = {
    # Essential
    "OPENROUTER_API_KEY": "OpenRouter key; selects the OpenRouter provider.",
    "TELEGRAM_BOT_TOKEN": "Bot token from @BotFather (required).",
    "XAI_API_KEY": "xAI key; selects the xAI provider. Set exactly one of the three keys.",
    # Common settings
    "BOT_TZ": "Time zone for timestamps, e.g. Australia/Melbourne (default UTC).",
    "MODEL": "Model ID (default grok-4.3 on xAI, z-ai/glm-5.3-flash on OpenRouter).",
    "REASONING": "Reasoning effort: low, medium or high; empty = the model's default.",
    "SEARCH": "on|off. Web search (and X search on xAI). Default on.",
    # Advanced and optional
    "ADMIN_CHAT_IDS": "Telegram IDs of the bot's admins (comma/space separated). Your user ID gets MCP-down alerts and may use /tokens; a group's chat ID gets the alerts only. Admins of the groups the bot is in count as admins too.",
    "ADMIN_ONLY": "on|off. Only admins may DM the bot or get Sharesight data (default on).",
    "DATA_DIR": "Directory for the bot's data (the database); Default: systemd's STATE_DIRECTORY, else the working directory.",
    "HISTORY_LIMIT": "Messages of chat history in the prompt; the window is HISTORY_LIMIT to 1.5x (default 20).",
    "LOG_WIDTH": "Longest line (characters) for the MCP tool lists in the log; longer lists end in \"…\" (default 80, 0 = no limit).",
    "MAX_TOKENS": "Reply cap in tokens, reasoning included (default 3000).",
    "MOVERS_BOTS": "Usernames of bots whose end-of-day big-movers lists get explained. Empty (default) = feature off.",
    "PORTFOLIO_NAMES": "Comma list of Sharesight portfolio names; a message naming one is treated as a portfolio question (default: the recipients' portfolio names).",
    "PERIOD_BACKFILL": "Comma list of Sharesight portfolio names (or all) whose daily/weekly/monthly/quarterly/yearly returns are fetched into the local cache in the background, so period questions answer instantly. Default all; list names to limit it, or off to disable.",
    "POST_TO_GROUPS_FROM_DM": "on|off. Lets an admin of a group the bot is in make it post there from a DM (default off).",
    "SEARCH_MODEL": "OpenRouter only: model that runs the searches (default xiaomi/mimo-v2.6-flash:online).",
    "SHARESIGHT_HOLDING_NEWS_RECIPIENTS": "Comma list of portfolio:telegram_username pairs to notify. Empty (default) = daily holding-news DM off.",
    "SHARESIGHT_HOLDING_NEWS_TIME": "HH:MM (BOT_TZ) for the daily holding-news check (default 08:00).",
    "TELEGRAM_DM_BUTTONS": "on|off. Preset-prompt buttons in private chats (default off).",
    "TELEGRAM_PRELOAD_CHAT": "Chat ID of a private chat the bot can post in. Chart periods are uploaded there ahead of time and reused when a period button is tapped. Empty (default) = off.",
}

DEFAULT_MODELS = {"xai": "grok-4.3", "openrouter": "z-ai/glm-5.3-flash"}

# Tuning constants (not configurable).
MAX_TOOL_ROUNDS = 6  # model <-> tool round trips per answer
MCP_TIMEOUT = 60  # seconds per MCP tool call
MAX_TOOL_OUTPUT = 12_000  # characters of one tool result sent to the model
HISTORY_LINE_MAX = 240  # characters of one history line
HISTORY_OWN_LINE_MAX = 160  # the bot's own earlier answers are the longest lines; cut them harder
MCP_STARTUP_WAIT = 30  # seconds a request waits for MCP servers that are still connecting
MAX_IMAGES = 2 # photos sent to the model per request
TEMPERATURE = 0.6
SEARCH_ENGINE = "auto"
SEARCH_RESULTS = 20
SEARCH_TOKENS = 400  # cap on each search write-up (OpenRouter search runner)
MAX_SEARCHES = 5  # search calls run per round
MAX_TG_MESSAGE = 4000  # Telegram's limit is 4096
HOLDING_NEWS_RETRY_EVERY = 30 * 60  # seconds between retries of a holding-news check the provider was too busy for
HOLDING_NEWS_RETRY_FOR = 12 * 3600  # seconds after which it gives up


class ConfigError(Exception):
    """The environment can't be turned into a working configuration."""


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    provider: str  # "xai" | "openrouter"
    api_key: str
    model: str
    reasoning: str
    search: bool
    search_model: str
    max_tokens: int
    history_limit: int
    log_width: int
    db_path: str
    tz: ZoneInfo
    admin_chats: frozenset[int]  # user IDs (alerts + admin commands) and group chat IDs (alerts only)
    admin_only: bool
    movers_bots: frozenset[str]
    dm_buttons: bool
    post_to_groups: bool
    preload_chat: int | None  # where chart periods are pre-uploaded
    holding_news_time: str
    holding_news_recipients: dict[str, str]
    portfolio_names: frozenset[str]
    period_backfill: frozenset[str]

    @property
    def holding_news(self) -> bool:
        """The daily holding-news DM is on when someone is listed to receive it."""
        return bool(self.holding_news_recipients)

    @property
    def movers_explain(self) -> bool:
        """Explaining another bot's movers lists is on when a bot is listed."""
        return bool(self.movers_bots)


ROOT = Path(__file__).resolve().parent.parent  # the checkout, where bot.py is
MCP_CONFIG = ROOT / "mcp_servers.json"  # the MCP servers, next to bot.py (not under DATA_DIR)


def database_path(env: Mapping[str, str]) -> str:
    """The SQLite file, chat_log.db inside DATA_DIR, which defaults to systemd's STATE_DIRECTORY (first entry
    if it lists several), else the working directory."""
    data_dir = env.get("DATA_DIR", "").strip() or env.get("STATE_DIRECTORY", "").split(":")[0].strip()
    return os.path.join(data_dir, "chat_log.db") if data_dir else "chat_log.db"



def _flag(env: Mapping[str, str], name: str, default: bool = False) -> bool:
    value = env.get(name, "").strip().lower()
    if not value:
        return default
    return value in ("1", "true", "yes", "on")


def _chat_id(env: Mapping[str, str], name: str) -> int | None:
    value = env.get(name, "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        raise ConfigError(f"{name} must be a Telegram chat ID (a number), not {value!r}.") from None


def _ids(text: str) -> frozenset[int]:
    return frozenset(int(x) for x in text.replace(",", " ").split())


def _pairs(text: str) -> dict[str, str]:
    return {
        name.strip().lower(): user.strip().lstrip("@").lower()
        for name, user in (pair.split(":", 1) for pair in text.split(",") if ":" in pair)
    }


def _names(text: str) -> frozenset[str]:
    return frozenset(n.strip().lower() for n in re.split(r"[,\n]", text) if n.strip())


def _backfill(text: str) -> frozenset[str]:
    names = _names(text)
    return frozenset() if names & {"off", "none", "false", "0"} else names


def load(env: Mapping[str, str] | None = None) -> Settings:
    """Build Settings from `env` (default: os.environ). Raises ConfigError if unusable."""
    env = os.environ if env is None else env
    token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
    keys = {"xai": env.get("XAI_API_KEY", "").strip(),
            "openrouter": env.get("OPENROUTER_API_KEY", "").strip()}
    if not token:
        raise ConfigError("TELEGRAM_BOT_TOKEN is not set.")
    given = [p for p, k in keys.items() if k]
    if len(given) > 1:
        raise ConfigError("More than one API key is set (XAI_API_KEY, OPENROUTER_API_KEY); "
                          "set only one, to choose the provider.")
    if not given:
        raise ConfigError("Set XAI_API_KEY (xAI) or OPENROUTER_API_KEY (OpenRouter).")
    provider = given[0]
    model = env.get("MODEL", "").strip() or DEFAULT_MODELS[provider]
    search_model = env.get("SEARCH_MODEL", "xiaomi/mimo-v2.6-flash:online").strip()
    search = env.get("SEARCH", "on").strip().lower() != "off"
    if provider == "openrouter" and not search_model:
        search = False  # nothing to run the searches with
    recipients = _pairs(env.get("SHARESIGHT_HOLDING_NEWS_RECIPIENTS", ""))
    return Settings(
        telegram_token=token,
        provider=provider,
        api_key=keys[provider],
        model=model,
        reasoning=env.get("REASONING", "").strip().lower(),
        search=search,
        search_model=search_model,
        max_tokens=int(env.get("MAX_TOKENS") or 3000),
        history_limit=int(env.get("HISTORY_LIMIT") or 20),
        log_width=int(env.get("LOG_WIDTH") or 80),
        db_path=database_path(env),
        tz=ZoneInfo(env.get("BOT_TZ", "UTC")),
        admin_chats=_ids(env.get("ADMIN_CHAT_IDS", "")),
        admin_only=_flag(env, "ADMIN_ONLY", default=True),
        movers_bots=frozenset(
            u.lower().lstrip("@") for u in env.get("MOVERS_BOTS", "").replace(",", " ").split()
        ),
        dm_buttons=_flag(env, "TELEGRAM_DM_BUTTONS"),
        post_to_groups=_flag(env, "POST_TO_GROUPS_FROM_DM"),
        preload_chat=_chat_id(env, "TELEGRAM_PRELOAD_CHAT"),
        holding_news_time=env.get("SHARESIGHT_HOLDING_NEWS_TIME", "08:00"),
        holding_news_recipients=recipients,
        portfolio_names=_names(env.get("PORTFOLIO_NAMES", "")) or frozenset(recipients),
        period_backfill=_backfill(env.get("PERIOD_BACKFILL", "all")),
    )
