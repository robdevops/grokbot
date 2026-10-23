# Configuration
[README](../README.md) · [all docs](../README.md#more)

## Providers
- `OPENROUTER_API_KEY` set -> OpenRouter (default model `z-ai/glm-5.3-flash`).
- `XAI_API_KEY` set -> xAI (default model `grok-4.3`).
- `ZAI_API_KEY` set -> z.ai directly (default model `glm-5.3-flash`). Prompts and chat history go to z.ai, a China-based provider.
  z.ai always thinks, so the bot sends `reasoning_effort` low unless `REASONING` says otherwise (medium maps to high).
  Cost is estimated from a price table in `lib/llm/zai.py`, because z.ai's usage reports none.
- **More than one key set, or none: the bot refuses to start** with a message saying so.
- `MODEL` and `REASONING` (low/medium/high) apply to every provider.
- The providers share all code except one adapter each (`lib/llm/xai.py` on the Responses API;
  `lib/llm/openrouter.py` and `lib/llm/zai.py` on chat completions, sharing `lib/llm/chat.py`).

## Environment variables
### Essential
Set `TELEGRAM_BOT_TOKEN` and exactly one of the three LLM keys.

| Variable | Meaning |
|---|---|
| `OPENROUTER_API_KEY` | OpenRouter key; selects the OpenRouter provider (default model `z-ai/glm-5.3-flash`). |
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather (required). |
| `XAI_API_KEY` | xAI key; selects the xAI provider (default model `grok-4.3`). |
| `ZAI_API_KEY` | z.ai key; selects the z.ai provider (default model `glm-5.3-flash`). |

### Common settings
| Variable | Meaning |
|---|---|
| `BOT_TZ` | Time zone for timestamps, e.g. Australia/Melbourne (default UTC). |
| `FAST_MODEL` | Optional cheaper/faster model used for simple requests (TOKEN_SAVER only). |
| `MODEL` | Model ID (default grok-4.3 on xAI, z-ai/glm-5.3-flash on OpenRouter, glm-5.3-flash on z.ai). |
| `REASONING` | Reasoning effort: low, medium or high; empty = the model's default (low on z.ai, which cannot turn thinking off). |
| `SEARCH` | on|off. Web search (and X search on xAI; z.ai's search API on z.ai). Default on. |

### Advanced and optional
| Variable | Meaning |
|---|---|
| `ADMIN_CHAT_IDS` | Telegram IDs of the bot's admins (comma/space separated). Your user ID gets a message when an MCP server goes down and may use `/credits`; a group's chat ID gets the down-server messages only. Admins of the groups the bot is in count as admins too. |
| `ADMIN_ONLY` | on|off. Only admins may DM the bot or get Sharesight data (default on). |
| `DATA_DIR` | Directory for the bot's data (the database); a relative `DB_PATH` resolves inside it, an absolute one is used as given. Default: systemd's `STATE_DIRECTORY` (see [Running under systemd](running.md#running-under-systemd)), else the working directory. In Docker, set `DATA_DIR=/data` and bind-mount a host directory there so history survives upgrades. |
| `DB_PATH` | SQLite file for chat history (default chat_log.db). Instances may share it. |
| `HISTORY_LIMIT` | Messages of chat history in the prompt; the window is HISTORY_LIMIT to 1.5x (default 20). |
| `LOG_WIDTH` | Longest line (characters) for the MCP tool lists in the log; a longer list is cut at a whole name and ends in `...` (default 95, 0 = no limit). |
| `MAX_TOKENS` | Reply cap in tokens, reasoning included (default 3000). |
| `MCP_CONFIG` | MCP server config file (default mcp_servers.json next to bot.py, whatever `DATA_DIR` is; missing = no MCP tools). |
| `MOVERS_BOTS` | Usernames of bots whose end-of-day big-movers lists get explained. Empty (default) = feature off. |
| `PORTFOLIO_NAMES` | Comma list of Sharesight portfolio names; a message naming one is treated as a portfolio question (default: the recipients' portfolio names). |
| `PERIOD_BACKFILL` | Comma list of Sharesight portfolio names (or `all`) whose returns per day, week, month, quarter and year are fetched into the local cache in the background (about 1,650 Sharesight calls per portfolio the first time, a few hours), so "best day since inception" answers instantly. Default `all`; list names to limit it, or `off` to disable. See [Caching Sharesight history](caching.md#caching-sharesight-history). |
| `POST_TO_GROUPS_FROM_DM` | on|off. A group admin can have the bot post in that group from a DM (default off). |
| `SEARCH_MODEL` | OpenRouter only: model that runs the searches (default xiaomi/mimo-v2.6-flash:online). |
| `SHARESIGHT_HOLDING_NEWS_RECIPIENTS` | Comma list of portfolio:telegram_username pairs to notify. Empty (default) = daily holding-news DM off. |
| `SHARESIGHT_HOLDING_NEWS_TIME` | HH:MM (BOT_TZ) for the daily holding-news check (default 08:00). |
| `TELEGRAM_DM_BUTTONS` | on|off. Preset-prompt buttons in private chats (default off). |
| `TOKEN_SAVER` | on|off. Master switch for the token-saving heuristics (default on). |

## Choosing a model
Figures from search results and Artificial Analysis at the time of writing (October 2026), so
treat them as rough; speed varies by provider.

| Model | $ per 1M in / out | Output speed | Quality index | Fast sibling for `FAST_MODEL` |
|---|---|---|---|---|
| `xiaomi/mimo-v2.6-pro` | 0.43 / 0.87 | ~28-46 tok/s | 46 (top open-weight model) | `xiaomi/mimo-v2.6-flash` |
| `xiaomi/mimo-v2.6-flash` | 0.14 / 0.28 | ~56 tok/s | 38 | itself (a flash model) |
| `z-ai/glm-5.3-flash` (default on OpenRouter) | 0.15 / 0.50 | ~50 tok/s (other hosts up to ~270) | 57 | itself (a flash model) |
| `grok-4.3` / `x-ai/grok-4.3` (default on xAI) | 1.25 / 2.50 | ~105-146 tok/s | 25 (at high reasoning) | none (see below) |
| `glm-5.3-flash` (default on z.ai) | 0.15 / 0.50 (cached input 0.03) | ~50 tok/s | 57 | itself (a flash model) |
| `glm-5.3-flashx` (z.ai) | 0.37 / 1.25 (cached input 0.075) | not found | not found | itself (a flash model) |
| `glm-5.3` (z.ai) | 1.40 / 4.40 (cached input 0.26) | not found | not found | `glm-5.3-flash` |
| `glm-5.2` (z.ai) | 1.40 / 4.40 (cached input 0.26) | not found | not found | `glm-5.3-flash` |
| `xiaomi/mimo-v2.5-pro` | 0.30 / 0.61 | ~29-46 tok/s | unreliable (retires 21 Oct 2026) | `xiaomi/mimo-v2.5` (also retiring) |
| `xiaomi/mimo-v2.5` | 0.12 / 0.24 | ~44-58 tok/s | not found (retires 21 Oct 2026) | itself |

Most of the delay is reasoning time before the first answer token, not typing speed: use
`REASONING=low`, and set `FAST_MODEL` so simple messages skip the big model. `FAST_MODEL` runs on the
same provider as your API key:
- **OpenRouter:** any model, for example `FAST_MODEL=xiaomi/mimo-v2.6-flash` or `z-ai/glm-5.3-flash`
  alongside a bigger `MODEL`.
- **z.ai:** a GLM model: for example `MODEL=glm-5.3` with `FAST_MODEL=glm-5.3-flash` (or `glm-5.3-flashx`).
  A z.ai model missing from the price table in `lib/llm/zai.py` shows $0 cost.
- **xAI:** a Grok model, and there is no fast one to pick: Grok 4 Fast and Grok 4.1 Fast were reportedly
  retired on 15 May 2026 and now redirect to `grok-4.3`.
