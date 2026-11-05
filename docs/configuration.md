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
| `ADMIN_CHAT_IDS` | empty | Telegram IDs of the bot's admins (comma/space separated). Your user ID gets a message when an MCP server goes down and may use `/usage`; a group's chat ID gets the down-server messages only. Admins of the groups the bot is in count as admins too. |
| `ADMIN_ONLY` | `on` | `on`: only admins can DM the bot or get Sharesight (portfolio) data; anyone else asking about a portfolio is told it is for admins only. `off`: everyone can do both. Either way anyone can @mention the bot or reply to it in a group, and `/usage` and `/flushcache` stay admin-only. Who counts as an admin: `ADMIN_CHAT_IDS` plus anyone who administers a group the bot is in (see [Features](features.md#commands)). |
| `DATA_DIR` | systemd's `STATE_DIRECTORY`, else the working directory | Directory for the bot's data (the SQLite database `chat_log.db`). Bots started with the same `DATA_DIR` share group history. With systemd the default is its `STATE_DIRECTORY` (see [Running under systemd](running.md#running-under-systemd)). In Docker, set `DATA_DIR=/data` and bind-mount a host directory there so history survives upgrades. |
| `FINANCE_LINK_TARGET` | `yahoo` | Where ticker links go: `yahoo` (Yahoo Finance), `google` (Google Finance, `CODE:EXCHANGE`: `NXT.AX` opens `NXT:ASX`, `JPM` opens `JPM:NYSE`) or `tradingview` (`EXCHANGE-CODE`: `ASX-NXT`, `NYSE-BRK.A`). A US ticker's exchange comes from the text (`NYSE:JPM`, `JPM (NYSE)`), the holdings, the NASDAQ-100 or the S&P 500 list; one the bot can't place opens a Google Finance search, or TradingView's page for the bare code. |
| `HISTORY_MAX` | `10` | The most messages of chat history in the prompt. Once a chat is longer, the window holds half to all of that and its start only moves every half of it, so the prompt cache keeps working. |
| `MAX_TOKENS` | `3000` | Reply cap in tokens, reasoning included. |
| `MOVERS_BOTS` | empty (feature off) | Usernames of bots whose end-of-day big-movers lists get explained. |
| `PORTFOLIO_NAMES` | the holding-news recipients' portfolio names | Comma list of Sharesight portfolio names; a message naming one is treated as a portfolio question (default: the recipients' portfolio names). |
| `PERIOD_BACKFILL` | `all` | Comma list of Sharesight portfolio names (or `all`) whose returns per day, week, month, quarter and year are fetched into the local cache in the background (about 1,650 Sharesight calls per portfolio the first time, a few hours), so "best day since inception" answers instantly. List names to limit it, or `off` to disable. See [Caching Sharesight history](caching.md#caching-sharesight-history). |
| `POST_TO_GROUPS_FROM_DM` | `off` | `on` or `off`. A group admin can have the bot post in that group from a DM. |
| `SEARCH_MODEL` | `MODEL` with `:online` added | OpenRouter only: model that runs the searches (`:online` is what gives an OpenRouter model web search). Set but empty = no search model, so search is off. |
| `SHARESIGHT_HOLDING_NEWS_RECIPIENTS` | empty (daily DM off) | Comma list of portfolio:telegram_username pairs to notify. |
| `SHARESIGHT_HOLDING_NEWS_TIME` | `08:00` | HH:MM (in `TZ`) for the daily holding-news check. |
| `TELEGRAM_DM_BUTTONS` | `off` | `on` or `off`. Preset-prompt buttons in private chats. |
| `TELEGRAM_IMAGE_PRELOAD_CHATID` | empty (off) | Chat ID of a private chat the bot can post in (a private group it is in, or a DM with you). The other periods of a chart are uploaded there in the background and reused when a period button is tapped, so the tap swaps the picture without fetching or drawing. Empty = off, and a tap fetches and draws. See [Charts](features.md#charts). |
