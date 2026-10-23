# Troubleshooting
[README](../README.md) · [all docs](../README.md#more)

- **"More than one API key is set"**: set only one of `XAI_API_KEY`, `OPENROUTER_API_KEY`, `ZAI_API_KEY`.
- **The bot ignores messages in a group**: run `/setprivacy` -> Disable in @BotFather and re-add it.
- **An MCP server shows as down**: the alert includes the server's own error (often a missing
  environment variable or `npx` not installed). Fix it and restart the bot.
- **Holding news never arrives**: the recipient must have messaged the bot once (so their user ID
  is known), `SHARESIGHT_HOLDING_NEWS_RECIPIENTS` must match the Sharesight portfolio names, and the
  Sharesight server must be connected.
- **Cache percentage stays at 0**: the first request of a conversation is always cold; if later ones
  stay at 0 the model's provider may not support prompt caching.
