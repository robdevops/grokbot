# Development
[README](../README.md) · [all docs](../README.md#more)

## Development
```
pip install -r requirements-dev.txt
make check          # ruff + pytest, a few seconds, no network
make prompt-report  # token breakdown of a sample request
python -m tools.capture sharesight   # real MCP output for test fixtures (also: yahoo)
```
The code is the `lib/` package (a map is in `CLAUDE.md`); `bot.py` is a thin entry point. Tests
use fakes for Telegram, both providers and MCP, so nothing in the suite touches the network.
`tests/test_config.py` fails if an environment variable is missing from the tables in [Configuration](configuration.md).
`tests/fixtures/` holds real MCP output captured with it (Sharesight anonymised, Yahoo public) and
`tests/test_fixtures.py` checks the bot against those shapes. `lib/` holds only what the bot needs to run; developer helpers live in `tools/`. `tools.capture` runs a
server from `mcp_servers.json` with the service's environment and prints its tools and a real call to each
to stdout: the output contains your real holdings and IDs, so redact it before sharing or committing it.
GitHub Actions (`.github/workflows/check.yml`) runs `make check` on every pull request and push to `main`, weekly
(Mondays 03:17 UTC) and on demand (the **Run workflow** button on the Actions tab). When a run that isn't a pull request
fails, it sends a Telegram message if the repository secrets `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set (Settings,
Secrets and variables, Actions); without them it skips the message.
