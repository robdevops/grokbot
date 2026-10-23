# Running the bot
[README](../README.md) · [all docs](../README.md#more)

## Running under systemd
`deploy/systemd/grokbot.service` is a basic unit. With `StateDirectory=`, systemd sets `STATE_DIRECTORY` and the bot
keeps its database there (`DATA_DIR` defaults to it), so the code directory can stay read-only. `mcp_servers.json` is
read from the checkout unless `MCP_CONFIG` says otherwise. A new install needs nothing more: the bot creates its
database on the first start. Put the keys and settings in `/etc/grokbot.env` (`KEY=value` lines; owned by root, mode 0600: systemd reads it, so the
bot's own user never needs access). Edit the paths (`/opt/grokbot`, `/etc/grokbot.env`) to match yours.

To keep the history of an older install, stop the old bot first (a clean stop folds its `-wal`/`-shm` files into the
database), then copy `chat_log.db` to `/var/lib/grokbot/` before the first start.

```
sudo cp deploy/systemd/grokbot.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now grokbot
```

Optional sandboxing is a separate drop-in. Delete the file to undo it:

```
sudo mkdir -p /etc/systemd/system/grokbot.service.d
sudo cp deploy/systemd/grokbot.service.d/hardening.conf /etc/systemd/system/grokbot.service.d/
sudo systemctl daemon-reload && sudo systemctl restart grokbot
```

## Running several instances
Instances can run side by side with different environments (for example one on xAI and one on
OpenRouter). Point them at the same `DB_PATH` to share group history (SQLite in WAL mode, safe for
concurrent use) or at different files to keep them apart; DMs are always per bot. Start each with a
different label (`python bot.py grok`, `python bot.py mimo`) to tell them apart in `ps`/`top`.

## Logs
One line per event, without a timestamp when run under systemd (journald adds one):
`[Jane Doe (@jane) 1234567890] first 60 characters of the message ...` (groups add ` @ <chat id>`),
`Round 2: in 14158 (7400 cached) out 738, stop`, and a per-request summary with rounds, tool calls,
tokens, cached % and cost. Tool servers log their tools in short wrapped lines; startup logs the
token size of the tool definitions. The `Starting ...` line shows the git commit the checkout is on.

## Troubleshooting
- **"More than one API key is set"**: set only one of `XAI_API_KEY`, `OPENROUTER_API_KEY`, `ZAI_API_KEY`.
- **The bot ignores messages in a group**: run `/setprivacy` -> Disable in @BotFather and re-add it.
- **An MCP server shows as down**: the alert includes the server's own error (often a missing
  environment variable or `npx` not installed). Fix it and restart the bot.
- **Holding news never arrives**: the recipient must have messaged the bot once (so their user ID
  is known), `SHARESIGHT_HOLDING_NEWS_RECIPIENTS` must match the Sharesight portfolio names, and the
  Sharesight server must be connected.
- **Cache percentage stays at 0**: the first request of a conversation is always cold; if later ones
  stay at 0 the model's provider may not support prompt caching.
