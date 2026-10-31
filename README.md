# Telegram group-chat bot

A Telegram bot for a group chat.

It is a general chat bot: the personality is in `lib/prompts.py` (the prompts) and what it can look up is in `mcp_servers.json` (the MCP servers and their tools). The example servers are finance data, so it answers questions about stocks and portfolios when asked, but the prompts don't steer a conversation towards them.

## Features
- Answers @mentions, replies and DMs with an LLM, using the group's recent messages as context.
- Live data from MCP servers (Yahoo Finance, Sharesight) and web search.
- Draws charts from that data when asked: two y axes, several series on each, either axis inverted.
- Reads images posted in the chat, if the model supports images.
- Runs on **xAI (Grok)** or **OpenRouter**, chosen by which API key you set.

## Quick start
1. Create a bot with @BotFather. Run `/setprivacy` and choose **Disable**, so the bot sees every
   message in a group (remove and re-add it to groups it is already in). Who may use the bot is
   controlled in Telegram/BotFather; the bot itself has no allow-list.
   - If the bot has access to Sharesight, enable "Restrict bot usage" in BotFather to enable user whitelisting.
     (Sharesight access is always read-only, but still relatively private.)
2. Python 3.13.5: `pip install -r requirements.txt` (plus Node.js for npx-based MCP servers such as Yahoo Finance).
3. Export `TELEGRAM_BOT_TOKEN` and **exactly one** of `XAI_API_KEY` or `OPENROUTER_API_KEY`.
4. `python bot.py`

To keep it running, see [Running under systemd](docs/running.md#running-under-systemd).

## More
- [Configuration](docs/configuration.md): providers, every environment variable, choosing a model.
- [How it works](docs/how-it-works.md): how it answers, search, MCP data tools (`mcp_servers.json`), formatting.
- [Features and commands](docs/features.md): charts, holding-news DM, movers replies, DM buttons, `/tokens`.
- [Caching and token saving](docs/caching.md): the Sharesight history cache and the token-saving heuristics.
- [Running the bot](docs/running.md): systemd, several instances, logs.
- [Troubleshooting](docs/troubleshooting.md): startup errors, silent bots, MCP servers down.
- [Development](docs/development.md): tests, fixtures and the code layout.
