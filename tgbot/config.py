"""Settings parsed once from the environment; the only module that knows which provider is in use."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from zoneinfo import ZoneInfo

# Every environment variable the bot reads, with a one-line description. README.md must
# mention each name (tests/test_config.py enforces it).
ENV_VARS: dict[str, str] = {
    "TELEGRAM_BOT_TOKEN": "Bot token from @BotFather (required).",
    "XAI_API_KEY": "xAI key; selects the xAI provider. Set exactly one of the two keys.",
    "OPENROUTER_API_KEY": "OpenRouter key; selects the OpenRouter provider.",
    "MODEL": "Model ID (default grok-4.7 on xAI, xiaomi/mimo-v2.6-pro on OpenRouter).",
    "FAST_MODEL": "Optional cheaper/faster model used for simple requests (TOKEN_SAVER only).",
    "REASONING": "Reasoning effort: low, medium or high; empty = the model's default.",
    "SEARCH": "on|off. Web search (and X search on xAI). Default on.",
    "SEARCH_MODEL": "OpenRouter only: model that runs the searches (default xiaomi/mimo-v2.6-flash:online).",
    "MAX_TOKENS": "Reply cap in tokens, reasoning included (default 1500, 4000 when TOKEN_SAVER=off).",
    "HISTORY_LIMIT": "Messages of chat history in the prompt; the window is HISTORY_LIMIT to 1.5x (default 20).",
    "DB_PATH": "SQLite file for chat history (default chat_log.db). Instances may share it.",
    "BOT_TZ": "Time zone for timestamps, e.g. Australia/Melbourne (default UTC).",
    "MCP_CONFIG": "MCP server config file (default mcp_servers.json; missing = no MCP tools).",
    "ALERT_CHAT_IDS": "Chat IDs told when an MCP server goes down (comma/space separated; empty = no alerts).",
    "OWNER_USER_ID": "Telegram user ID allowed to use /credits and /usage.",
    "MOVERS_BOTS": "Usernames of bots whose end-of-day big-movers lists get explained. Empty (default) = feature off.",
    "TELEGRAM_DM_BUTTONS": "on|off. Preset-prompt buttons in private chats (default off).",
    "SHARESIGHT_HOLDING_NEWS_TIME": "HH:MM (BOT_TZ) for the daily holding-news check (default 08:00).",
    "SHARESIGHT_HOLDING_NEWS_RECIPIENTS": "Comma list of portfolio:telegram_username pairs to notify. Empty (default) = daily holding-news DM off.",
    "PORTFOLIO_NAMES": "Comma list of Sharesight portfolio names; a message naming one is treated as a portfolio question (default: the recipients' portfolio names).",
    "TOKEN_SAVER": "on|off. Master switch for the token-saving heuristics (default on).",
}

# Tuning constants (not configurable).
MAX_TOOL_ROUNDS = 6  # model <-> tool round trips per answer
MCP_TIMEOUT = 60  # seconds per MCP tool call
MAX_IMAGES = 2  # photos sent to the model per request
TEMPERATURE = 0.6
SEARCH_ENGINE = "auto"
SEARCH_RESULTS = 20
SEARCH_TOKENS = 400  # cap on each search write-up (OpenRouter search runner)
MAX_SEARCHES = 5  # search calls run per round
MAX_TG_MESSAGE = 4000  # Telegram's limit is 4096


class ConfigError(Exception):
    """The environment can't be turned into a working configuration."""


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    provider: str  # "xai" | "openrouter"
    api_key: str
    model: str
    fast_model: str
    reasoning: str
    search: bool
    search_model: str
    max_tokens: int
    history_limit: int
    db_path: str
    tz: ZoneInfo
    mcp_config: str
    alert_chats: frozenset[int]
    owner_id: int
    movers_bots: frozenset[str]
    dm_buttons: bool
    holding_news_time: str
    holding_news_recipients: dict[str, str]
    portfolio_names: frozenset[str]
    token_saver: bool

    @property
    def holding_news(self) -> bool:
        """The daily holding-news DM is on when someone is listed to receive it."""
        return bool(self.holding_news_recipients)

    @property
    def movers_explain(self) -> bool:
        """Explaining another bot's movers lists is on when a bot is listed."""
        return bool(self.movers_bots)

    @property
    def max_tool_output(self) -> int:
        """Characters of one tool result sent to the model."""
        return 12_000 if self.token_saver else 50_000

    @property
    def history_line_max(self) -> int:
        return 240 if self.token_saver else 400

    @property
    def history_own_line_max(self) -> int | None:
        """The bot's own earlier answers are the longest lines; cut them harder."""
        return 160 if self.token_saver else None


def _flag(env: Mapping[str, str], name: str, default: bool = False) -> bool:
    value = env.get(name, "").strip().lower()
    if not value:
        return default
    return value in ("1", "true", "yes", "on")


def _ids(text: str) -> frozenset[int]:
    return frozenset(int(x) for x in text.replace(",", " ").split())


def _pairs(text: str) -> dict[str, str]:
    return {
        name.strip().lower(): user.strip().lstrip("@").lower()
        for name, user in (pair.split(":", 1) for pair in text.split(",") if ":" in pair)
    }


def _names(text: str) -> frozenset[str]:
    return frozenset(n.strip().lower() for n in re.split(r"[,\n]", text) if n.strip())


def load(env: Mapping[str, str] | None = None) -> Settings:
    """Build Settings from `env` (default: os.environ). Raises ConfigError if unusable."""
    env = os.environ if env is None else env
    token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
    xai = env.get("XAI_API_KEY", "").strip()
    openrouter = env.get("OPENROUTER_API_KEY", "").strip()
    if not token:
        raise ConfigError("TELEGRAM_BOT_TOKEN is not set.")
    if xai and openrouter:
        raise ConfigError(
            "Both XAI_API_KEY and OPENROUTER_API_KEY are set; set only one, to choose the provider."
        )
    if not (xai or openrouter):
        raise ConfigError("Set XAI_API_KEY (xAI) or OPENROUTER_API_KEY (OpenRouter).")
    provider = "openrouter" if openrouter else "xai"
    saver = _flag(env, "TOKEN_SAVER", default=True)
    model = env.get("MODEL", "").strip() or (
        "xiaomi/mimo-v2.6-pro" if provider == "openrouter" else "grok-4.7"
    )
    search_model = env.get("SEARCH_MODEL", "xiaomi/mimo-v2.6-flash:online").strip()
    search = env.get("SEARCH", "on").strip().lower() != "off"
    if provider == "openrouter" and not search_model:
        search = False  # nothing to run the searches with
    recipients = _pairs(env.get("SHARESIGHT_HOLDING_NEWS_RECIPIENTS", ""))
    return Settings(
        telegram_token=token,
        provider=provider,
        api_key=openrouter or xai,
        model=model,
        fast_model=env.get("FAST_MODEL", "").strip() if saver else "",
        reasoning=env.get("REASONING", "").strip().lower(),
        search=search,
        search_model=search_model,
        max_tokens=int(env.get("MAX_TOKENS") or (1500 if saver else 4000)),
        history_limit=int(env.get("HISTORY_LIMIT") or 20),
        db_path=env.get("DB_PATH", "chat_log.db"),
        tz=ZoneInfo(env.get("BOT_TZ", "UTC")),
        mcp_config=env.get("MCP_CONFIG", "mcp_servers.json"),
        alert_chats=_ids(env.get("ALERT_CHAT_IDS", "")),
        owner_id=int(env.get("OWNER_USER_ID") or 0),
        movers_bots=frozenset(
            u.lower().lstrip("@") for u in env.get("MOVERS_BOTS", "").replace(",", " ").split()
        ),
        dm_buttons=_flag(env, "TELEGRAM_DM_BUTTONS"),
        holding_news_time=env.get("SHARESIGHT_HOLDING_NEWS_TIME", "08:00"),
        holding_news_recipients=recipients,
        portfolio_names=_names(env.get("PORTFOLIO_NAMES", "")) or frozenset(recipients),
        token_saver=saver,
    )
