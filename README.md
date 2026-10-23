# Stock-chat Telegram bot

A Telegram bot for a group chat.

It comes preprogrammed to chat about stocks and investing, but it can be easily retasked by editing `lib/prompts.py` and `mcp_servers.json`. MCPs can be customised by editing `mcp_servers.json`.

People @mention it (or reply to it, or DM it) and it answers with an LLM, using the group's recent messages as context, live market data from MCP servers (Yahoo Finance, Sharesight) and web search. It runs on **xAI (Grok)**, **OpenRouter** or **z.ai**, chosen by which API key you set.

## Quick start
1. Create a bot with @BotFather. Run `/setprivacy` and choose **Disable**, so the bot sees every
   message in a group (remove and re-add it to groups it is already in). Who may use the bot is
   controlled in Telegram/BotFather; the bot itself has no allow-list.
   - **IMPORTANT:** if the bot has access to Sharesight, enable "Restrict bot usage" in BotFather (this
     enables user whitelisting).
2. Python 3.13.5: `pip install -r requirements.txt` (plus Node.js for npx-based MCP servers such as Yahoo Finance).
3. Export `TELEGRAM_BOT_TOKEN` and **exactly one** of `XAI_API_KEY`, `OPENROUTER_API_KEY` or `ZAI_API_KEY`.
4. `python bot.py`

To keep it running, see [Running under systemd](docs/running.md#running-under-systemd).

## More
- [Configuration](docs/configuration.md): providers, every environment variable, choosing a model.
- [How it works](docs/how-it-works.md): how it answers, search, MCP data tools (`mcp_servers.json`), formatting.
- [Optional features and commands](docs/features.md): holding-news DM, movers replies, DM buttons, `/credits`.
- [Caching and token saving](docs/caching.md): the Sharesight history cache and the token-saving heuristics.
- [Running the bot](docs/running.md): systemd, several instances, logs, troubleshooting.
- [Development](docs/development.md): tests, fixtures and the code layout.
