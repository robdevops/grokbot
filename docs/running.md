# Running the bot
[README](../README.md) · [all docs](../README.md#more)

## Running under systemd
The unit in `deploy/systemd/grokbot.service` assumes the checkout is in `/opt/grokbot` with a virtualenv in
`/opt/grokbot/.venv`. Edit those paths if yours differ.

**1. Settings file.** Keys and settings go in `/etc/grokbot.env`, one `KEY=value` per line (no quotes needed):

```
TELEGRAM_BOT_TOKEN=123456:ABC...
XAI_API_KEY=xai-...
```

systemd reads this file as root, so the bot never needs access to it. Keep it private, since it holds your keys:

```
sudo install -m 0600 -o root -g root /dev/null /etc/grokbot.env
sudoedit /etc/grokbot.env
```

**2. Data directory.** Nothing to create. The unit sets `StateDirectory=grokbot`, so systemd makes
`/var/lib/grokbot`, owned by the service's temporary user, and passes its path to the bot. The bot keeps its database
there as `chat_log.db` (set `DATA_DIR` in the env file to put it elsewhere). The code directory stays read-only, and
`mcp_servers.json` is read from the checkout.

**3. Install and start.**

```
sudo cp deploy/systemd/grokbot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now grokbot
journalctl -u grokbot -f
```

**Keeping an old database.** Stop the old bot first (a clean stop folds its `-wal` and `-shm` files into the
database), then copy `chat_log.db` to `/var/lib/grokbot/` before the first start.

**Optional sandboxing** is a separate drop-in. Delete the file to undo it:

```
sudo mkdir -p /etc/systemd/system/grokbot.service.d
sudo cp deploy/systemd/grokbot.service.d/hardening.conf /etc/systemd/system/grokbot.service.d/
sudo systemctl daemon-reload
sudo systemctl restart grokbot
```

## Running several instances
Instances can run side by side with different environments (for example one on xAI and one on
OpenRouter). Start them with the same `DATA_DIR` to share group history (SQLite in WAL mode, safe for
concurrent use) or with different ones to keep them apart; DMs are always per bot. Start each with a
different label (`python bot.py grok`, `python bot.py mimo`) to tell them apart in `ps`/`top`.

## Logs
Under systemd the log is in the journal:

```
journalctl -u grokbot -f           # follow
journalctl -u grokbot --since today
journalctl -u grokbot -p info      # without the debug detail
journalctl -u grokbot -p warning   # warnings and errors only
```

Every line reaches the journal at its own priority, so journalctl colours it: errors red, warnings yellow, debug
grey. Run by hand (`python bot.py`), the log goes to the terminal with a timestamp on each line.

| Level | What |
|---|---|
| error | Something failed: a reply, a holding-news check, an MCP server, a background pass. Includes the traceback. |
| warning | Handled, but worth a look: a retry, a fallback, a busy provider, a setting that matches nothing. |
| info | Startup and connections, one line per message the bot answers, and one summary per request. |
| debug | The detail: each MCP call and its result size, search queries, tokens per round, attached images, backfill progress. |

A message line looks like `[Jane Doe (@jane) 1234567890] first 60 characters of the message ...` (groups add
` @ <chat id>`). The summary has the time, rounds, tool calls, searches, tokens, cached % and cost; the debug line
`Round 2: in 14158 (7400 cached) out 738, stop` gives each round's share. Each tool server logs its tools on one
line (cut at `LOG_WIDTH`), and startup logs the token size of the tool definitions. The `Starting ...` line shows the
git commit the checkout is on.
