# Telegram stock-chat bot

(README is completed in the last phase.)

## Environment variables

| Variable | Meaning |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather (required). |
| `XAI_API_KEY` | xAI key; selects the xAI provider. Set exactly one of the two keys. |
| `OPENROUTER_API_KEY` | OpenRouter key; selects the OpenRouter provider. |
| `MODEL` | Model ID (default grok-4.7 on xAI, xiaomi/mimo-v2.6-pro on OpenRouter). |
| `FAST_MODEL` | Optional cheaper/faster model used for simple requests (TOKEN_SAVER only). |
| `REASONING` | Reasoning effort: low, medium or high; empty = the model's default. |
| `SEARCH` | on|off. Web search (and X search on xAI). Default on. |
| `SEARCH_MODEL` | OpenRouter only: model that runs the searches (default xiaomi/mimo-v2.6-flash:online). |
| `MAX_TOKENS` | Reply cap in tokens, reasoning included (default 1500, 4000 when TOKEN_SAVER=off). |
| `HISTORY_LIMIT` | Messages of chat history in the prompt; the window is HISTORY_LIMIT to 1.5x (default 20). |
| `DB_PATH` | SQLite file for chat history (default chat_log.db). Instances may share it. |
| `BOT_TZ` | Time zone for timestamps, e.g. Australia/Melbourne (default UTC). |
| `MCP_CONFIG` | MCP server config file (default mcp_servers.json; missing = no MCP tools). |
| `ALERT_CHAT_IDS` | Chat IDs told when an MCP server goes down (comma/space separated; empty = no alerts). |
| `OWNER_USER_ID` | Telegram user ID allowed to use /credits and /usage. |
| `MOVERS_EXPLAIN` | on|off. Explain another bot's end-of-day big-movers lists (default off). |
| `MOVERS_BOTS` | Usernames of the bots whose movers lists are explained (default finbotibot). |
| `TELEGRAM_DM_BUTTONS` | on|off. Preset-prompt buttons in private chats (default off). |
| `SHARESIGHT_HOLDING_NEWS` | on|off. Daily Sharesight holding-news DM (default off). |
| `SHARESIGHT_HOLDING_NEWS_TIME` | HH:MM (BOT_TZ) for the daily holding-news check (default 08:00). |
| `SHARESIGHT_HOLDING_NEWS_RECIPIENTS` | Comma list of Sharesight portfolio:telegram_username pairs to notify. |
| `TOKEN_SAVER` | on|off. Master switch for the token-saving heuristics (default on). |
