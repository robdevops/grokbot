# Configuration
[README](../README.md) · [all docs](../README.md#more)

## Environment variables
### Essential
Set `TELEGRAM_BOT_TOKEN` and exactly one of the three LLM keys. With more than one, or none, the bot refuses to start.

| Variable | Default | Meaning |
|---|---|---|
| `OPENROUTER_API_KEY` | none | OpenRouter key; selects the OpenRouter provider (its default model is `deepseek/deepseek-v4.1-flash-1017`). |
| `TELEGRAM_BOT_TOKEN` | none (required) | Bot token from @BotFather (required). |
| `XAI_API_KEY` | none | xAI key; selects the xAI provider (its default model is `grok-4.3`). |

### Common settings
| Variable | Default | Meaning |
|---|---|---|
| `TZ` | `UTC` | Time zone for timestamps, e.g. `Australia/Melbourne`. |
| `MODEL` | `grok-4.3` on xAI, `deepseek/deepseek-v4.1-flash-1017` on OpenRouter | Model ID. |
| `REASONING` | the model's own | Reasoning effort: `low`, `medium` or `high`. |
| `SEARCH` | `on` | `on` or `off`. Web search (and X search on xAI). |
| `DARKMODE` | `off` | Chart theme: `off` draws light charts, `on` draws dark ones, `auto` draws dark ones from 9 pm to 7 am in `TZ`. Before this setting existed, charts were always `auto`. |

### Advanced and optional
| Variable | Default | Meaning |
|---|---|---|
| `ADMIN_CHAT_IDS` | empty | Telegram IDs of the bot's admins (comma/space separated). Your user ID gets a message when an MCP server goes down and may use `/tokens`; a group's chat ID gets the down-server messages only. Admins of the groups the bot is in count as admins too. |
| `ADMIN_ONLY` | `on` | `on`: only admins can DM the bot or get Sharesight (portfolio) data; anyone else asking about a portfolio is told it is for admins only. `off`: everyone can do both. Either way anyone can @mention the bot or reply to it in a group, and `/tokens` and `/flushcache` stay admin-only. Who counts as an admin: `ADMIN_CHAT_IDS` plus anyone who administers a group the bot is in (see [Features](features.md#commands)). |
| `DATA_DIR` | systemd's `STATE_DIRECTORY`, else the working directory | Directory for the bot's data (the SQLite database `chat_log.db`). Bots started with the same `DATA_DIR` share group history. With systemd the default is its `STATE_DIRECTORY` (see [Running under systemd](running.md#running-under-systemd)). In Docker, set `DATA_DIR=/data` and bind-mount a host directory there so history survives upgrades. |
| `HISTORY_MAX` | `10` | The most messages of chat history in the prompt. Once a chat is longer, the window holds half to all of that and its start only moves every half of it, so the prompt cache keeps working. |
| `MAX_TOKENS` | `3000` | Reply cap in tokens, reasoning included. |
| `MOVERS_BOTS` | empty (feature off) | Usernames of bots whose end-of-day big-movers lists get explained. |
| `NOT_TICKERS` | empty | Extra capitalised words, comma or space separated, that are never linked as stock tickers, e.g. `GAAP GM`. Added to a built-in list (`EPS`, `GDP`, `GPU`, ...), so it only needs the words that still get linked. Words that are real tickers (`GM`, `UPS`) then stop linking everywhere. |
| `PORTFOLIO_NAMES` | the holding-news recipients' portfolio names | Comma list of Sharesight portfolio names; a message naming one is treated as a portfolio question (default: the recipients' portfolio names). |
| `PERIOD_BACKFILL` | `all` | Comma list of Sharesight portfolio names (or `all`) whose returns per day, week, month, quarter and year are fetched into the local cache in the background (about 1,650 Sharesight calls per portfolio the first time, a few hours), so "best day since inception" answers instantly. List names to limit it, or `off` to disable. See [Caching Sharesight history](caching.md#caching-sharesight-history). |
| `POST_TO_GROUPS_FROM_DM` | `off` | `on` or `off`. A group admin can have the bot post in that group from a DM. |
| `SEARCH_MODEL` | `MODEL` with `:online` added | OpenRouter only: model that runs the searches (`:online` is what gives an OpenRouter model web search). Set but empty = no search model, so search is off. |
| `SHARESIGHT_HOLDING_NEWS_RECIPIENTS` | empty (daily DM off) | Comma list of portfolio:telegram_username pairs to notify. |
| `SHARESIGHT_HOLDING_NEWS_TIME` | `08:00` | HH:MM (in `TZ`) for the daily holding-news check. |
| `TELEGRAM_DM_BUTTONS` | `off` | `on` or `off`. Preset-prompt buttons in private chats. |
| `TELEGRAM_IMAGE_PRELOAD_CHATID` | empty (off) | Chat ID of a private chat the bot can post in (a private group it is in, or a DM with you). The other periods of a chart are uploaded there in the background and reused when a period button is tapped, so the tap swaps the picture without fetching or drawing. Empty = off, and a tap fetches and draws. See [Charts](features.md#charts). |

## Choosing a model
Figures from search results and Artificial Analysis at the time of writing (October 2026), so
treat them as rough; speed varies by provider.

| Model | $ per 1M in / out | Output speed | Quality index |
|---|---|---|---|
| `xiaomi/mimo-v2.6-pro` | 0.43 / 0.87 | ~28-46 tok/s | 46 (top open-weight model) |
| `xiaomi/mimo-v2.6-flash` | 0.14 / 0.28 | ~56 tok/s | 38 |
| `deepseek/deepseek-v4.1-flash-1017` (default on OpenRouter) | not measured | not measured | not measured |
| `z-ai/glm-5.3-flash` (the previous default) | 0.15 / 0.50 | ~50 tok/s (other hosts up to ~270) | 57 |
| `grok-4.3` / `x-ai/grok-4.3` (default on xAI) | 1.25 / 2.50 | ~105-146 tok/s | 25 (at high reasoning) |
| `xiaomi/mimo-v2.5-pro` | 0.30 / 0.61 | ~29-46 tok/s | unreliable (retires 21 Oct 2026) |
| `xiaomi/mimo-v2.5` | 0.12 / 0.24 | ~44-58 tok/s | not found (retires 21 Oct 2026) |

Most of the delay is reasoning time before the first answer token, not typing speed: use `REASONING=low`.
