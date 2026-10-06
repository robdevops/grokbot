# Stock-chat Telegram bot

A Telegram bot for a group chat about stocks and investing. People @mention it (or reply to it, or
DM it) and it answers with an LLM, using the group's recent messages as context, live market data
from MCP servers (Yahoo Finance, Sharesight) and web search. It runs on **xAI (Grok)** or
**OpenRouter**, chosen by which API key you set.

## Contents
- [Quick start](#quick-start) · [Providers](#providers) · [How it answers](#how-it-answers)
- [Search](#search) · [MCP data tools](#mcp-data-tools) · [Formatting and ticker links](#formatting-and-ticker-links)
- [Optional features](#optional-features-off-by-default) · [Commands](#commands)
- [Caching and token saving](#caching-and-token-saving) · [Running several instances](#running-several-instances)
- [Logs](#logs) · [Environment variables](#environment-variables) · [Choosing a model](#choosing-a-model)
- [Development](#development) · [Troubleshooting](#troubleshooting)

## Quick start
1. Create a bot with @BotFather. Run `/setprivacy` and choose **Disable**, so the bot sees every
   message in a group (remove and re-add it to groups it is already in). Who may use the bot is
   controlled in Telegram/BotFather; the bot itself has no allow-list.
2. `pip install -r requirements.txt` (plus Node.js for npx-based MCP servers such as Yahoo Finance).
3. Export `TELEGRAM_BOT_TOKEN` and **exactly one** of `XAI_API_KEY` or `OPENROUTER_API_KEY`.
4. Optional: edit `mcp_servers.json` (see [MCP data tools](#mcp-data-tools)).
5. `python bot.py [label]`

The optional `label` is ignored; it only appears in `ps`/`top` so you can tell instances apart.
A systemd unit just needs `ExecStart=/usr/bin/python3 /path/to/bot.py mimo` and an `Environment=`
or `EnvironmentFile=` for the variables below.

## Providers
- `OPENROUTER_API_KEY` set -> OpenRouter (default model `z-ai/glm-5.3-flash`).
- `XAI_API_KEY` set -> xAI (default model `grok-4.3`).
- **Both set, or neither: the bot refuses to start** with a message saying so.
- `MODEL` and `REASONING` (low/medium/high) apply to either provider.
- The two providers share all code except one adapter each (`lib/llm/xai.py` on the Responses
  API, `lib/llm/openrouter.py` on chat completions).

## How it answers
- **Groups:** the bot answers when it is @mentioned or when someone replies to one of its messages. Every message is logged;
  messages from other bots are logged as context but never answered (the one exception is the
  movers feature below). Edited messages are logged, not answered.
- **Private chats:** every message is answered, and the reply streams into a Telegram message
  draft ("Thinking..." then the text as it arrives). Replies are not quoted in DMs.
- **History:** the prompt carries a window of the chat's recent messages, oldest first. The window
  is `HISTORY_LIMIT` to 1.5x that many messages (20-29 by default): its start only moves every
  `HISTORY_LIMIT/2` messages, so the start of the prompt stays identical between requests and the
  provider's prompt cache keeps working. The bot's own earlier replies appear as "You" (its latest reply in full, so follow-up questions about it
  work; older ones shortened); a replied-to
  message is quoted even if it is older than the window.
- **Shared database:** group history lives in `messages`, shared by every bot pointed at the same
  `DB_PATH` (so two bots in one group see each other's replies). Private chats live in
  `dm_messages`, keyed by bot, because each bot's DM with a person has its own message-ID sequence.
- **Images:** photos in the mention, or in the message being replied to, are sent to the model
  (up to 2). With `TOKEN_SAVER` on a mid-size copy is sent at low detail, unless the text asks the
  model to read it (read, text, chart, table, screenshot...).
- **Time:** the current date and time (in `BOT_TZ`) is included in every prompt.
- **Replies are final:** the prompt tells the model never to say it will "check later"; it makes its
  tool calls first (several at once) and answers in one go.
- **Errors:** a failed request replies with the error text; the bot never goes silent.

## Search
- **xAI:** server-side `web_search` and `x_search` (X/Twitter) tools.
- **OpenRouter:** OpenRouter's `web_search` tool; when it hands a search call back, the bot runs the
  query through `SEARCH_MODEL` (default `xiaomi/mimo-v2.6-flash:online`) and returns the write-up.
  Some models print a search call as text instead of making it; the bot recovers those queries too.
- At most 5 searches per round; extras get a "skipped" note.
- `SEARCH=off` turns search off (the prompt then says the model has no web search).
- **Forced search:** movers lists, DM preset buttons and the holding-news digest *require* a
  search: the first round is forced (`tool_choice=required`), the model gets only the search tool,
  and these requests are never retried without it (so news is never made up from memory).

## MCP data tools
MCP servers listed in `mcp_servers.json` are started by the bot itself and their tools are offered
to the model as function tools. The bot runs the calls locally, so servers never need to be
reachable from the internet and credentials stay on the machine. Servers connect in the
background: the bot starts immediately, and a message that arrives while a server is still connecting
waits for it (up to 30 s) so it isn't answered without its tools.
```json
{"mcpServers": {"yahoo": {"command": "npx", "args": ["-y", "yahoo-finance-mcp-server@1.3.1"],
   "description": "what it is and how to use it (goes into the prompt)",
   "blocked_tools": ["get_options_chain"], "cache_ttl": 30}}}
```
Per-server keys: `command`/`args`/`env` (local, stdio) or `url`/`headers` (remote HTTP);
`${VAR}` in `env`/`headers` is taken from the environment; `description`; `blocked_tools`;
`allowed_tools` (an explicit allow-list); `disabled`; `max_concurrent` (default 4, stops a 20-stock
request hammering Yahoo); `cache_ttl` seconds during which identical calls share one result (0 =
off); `slim` (result slimming, defaults to the server's name; `sharesight` flattens holdings);
`gate` (`portfolio` = only offered when the question is about portfolios/holdings);
`current_holdings_only` (Sharesight: never include sold holdings).
- **Tool names are exact:** `blocked_tools` and `allowed_tools` take the server's real tool names, as
  printed in the startup log (`get_analyst_estimates`, not `analyst_estimates`). An entry that matches
  no tool is ignored, with a warning that suggests the closest real name.
- **Read-only by default:** tools are only offered if the server marks them read-only or their name
  starts with get/list/search/fetch/find/lookup/show/read, so nobody in the group can talk the bot
  into changing your data. Use `allowed_tools` to opt others in.
- **Everyone who can reach the bot can use every enabled tool**, including Sharesight portfolio
  data (there is no per-user tool restriction; Telegram/BotFather
  controls who may use the bot). Restrict the bot in BotFather, or leave Sharesight out.
- **Down servers:** if a server dies, chats in `ADMIN_CHAT_IDS` get a message with the error, and the
  model is told the source is down (and quotes the error) instead of claiming it has no access. A
  dead server is not restarted until the bot restarts.
- **Slimming:** results are compacted before the model sees them (see token saving below).

## Formatting and ticker links
- Replies are Telegram HTML. Markdown the model slips in (`**bold**`, links, backticks) is
  converted; trailing "not advice"/"NFA"/"DYOR" disclaimers are stripped.
- **Yahoo Finance links:** the model writes plain tickers (with the exchange suffix for non-US
  listings: `SQX.AX`, `000660.KS`) and the bot links each one to its Yahoo Finance page. The visible text drops the suffix (`SQX.AX` shows as `SQX`, `BRK.B` stays) while the URL
  keeps it; crypto gets `-USD` in the URL (`BTC` -> `BTC-USD`). Words like CEO, ETF, FY26 and Q3 are
  not linked, a lone letter only counts next to a price or move (`F 5.2`), and text already inside a
  link, `<code>` or `<pre>` is left alone. A company name written as `Name (TICKER)` is bold,
  name and ticker together (`Micron (MU)`), unless it is already bold.
- **Citations:** a raw link in a reply (a bare URL, or a link labelled with its own URL) becomes a
  numbered link, `[1]`, `[2]`, in order of appearance, the same URL keeping its number. Links with a
  real label, and anything in `<code>` or `<pre>`, are left alone.
- Long answers are split under Telegram's limit without cutting a tag or entity; tags still open at
  a split are closed and re-opened in the next message. If Telegram rejects the markup the reply is
  re-sent as plain text.
- Replies are sent silently, without link previews. The bot never @-tags the movers bots.

## Optional features (off by default)
Each is off by default. The holding-news DM and the movers reply are switched on by setting the names they need (recipients, bots); the DM buttons have a flag.
- **Daily holding-news DM** (switched on by listing recipients in `SHARESIGHT_HOLDING_NEWS_RECIPIENTS`, at `SHARESIGHT_HOLDING_NEWS_TIME` in
  `BOT_TZ`, `portfolio:username` pairs such as
  `MyPortfolio:alice,MySMSF:alice`; empty, the default, means off):
  reads each person's current Sharesight holdings, searches for *major* news from the past 24 hours
  (a forced search), and DMs only what qualifies (most days: nothing). Stories already reported in
  the last 3 days are not repeated. Each message has an **Unsubscribe** button: per ticker, or all
  holding news, with an **Undo** button afterwards. People must have messaged the bot (or a group it
  is in) once so it knows their user ID. `/holdingnews` in a DM runs the check now and always replies.
- **Reply to a movers bot** (switched on by listing the bots in `MOVERS_BOTS`; empty, the default, means off): when such a
  bot posts an end-of-day list ("≥ 5.0% at close (ASX):" followed by stocks with % changes) the bot
  explains each move with a forced search, once, without tagging the other bot, ignoring its image,
  and dropping the closing wrap-up paragraph.
- **DM preset buttons** (`TELEGRAM_DM_BUTTONS=on`): a keyboard under the message box in private chats
  (News, News (AU), Finance, AI, Sci-fi, SpaceX & Tesla). A tap is a standalone request (no chat
  history) with a forced search. The keyboard is attached to `/start` and every DM reply, and **pushed
  on startup**: if the button set changed since the last run, everyone the bot has a private chat
  with gets one short "Buttons updated." message carrying the new keyboard (nothing is sent when it
  is unchanged; people who blocked the bot are skipped).
- **Post to a group from a DM** (`POST_TO_GROUPS_FROM_DM=on`): in a private chat, "say hello in the
  <group name> group" makes the bot post there, but only if the person asking is the creator or an
  admin of that group (checked with Telegram each time) and the bot is a member. Groups only, not
  channels. Telegram can't list a bot's chats, so the bot learns them from the messages it sees and
  from being added: a group it joined earlier is unknown until someone posts there. If the name
  matches none of your groups the reply is the same whether or not the group exists.

## Commands
| Command | Who | What |
|---|---|---|
| `/start` | DM | Greeting (and the buttons if enabled) |
| `/holdingnews` | DM, if holding news is on | Run the holding-news check now |
| `/credits` | `OWNER_USER_ID` | Provider balance (OpenRouter; xAI has none to show) |
| `/usage` | `OWNER_USER_ID` | Requests, tokens (and % cached) and cost per model, last 24 h and 7 days |

## Caching and token saving
**Provider prompt caching** (both providers keep a conversation on the server holding its cached
prompt): xAI gets `prompt_cache_key` and the `x-grok-conv-id` header; OpenRouter gets `session_id`,
the `x-session-id` header and `prompt_cache_key`. The key is `<bot name>-chat-<chat id>` (ASCII,
at most 128 characters). Prompts are ordered so the cacheable part comes first: system prompt,
tools (sorted), history (stepped window), and the volatile part last (time, down-server notice).
Each answer's log line shows the cached percentage.

**`TOKEN_SAVER`** (on by default; set `off` to switch all of these off at once):
- **Tool gate:** chit-chat gets no data tools and no search (the model answers on its own). Market
  questions (a ticker, or words like price, earnings, ETF, market) get the market servers plus search;
  news-style questions ("latest", "today", "who won") get search. Servers marked `"gate": "portfolio"`
  (Sharesight) are only offered for **portfolio questions**: portfolio, holdings, Sharesight, SMSF, super
  fund, net worth, "what do I own", "how am I doing", "am I up or down", "what did I make"; "my" or "our"
  followed by stocks, shares, positions, account, cash, balance, returns, gains, performance, dividends,
  winners, losers, P&L, investments, funds, ETFs, trades or watchlist ("my biggest winner" counts);
  weaker words (performance, gains, positions, winners, losers, P&L) when no ticker is named; and
  any name in `PORTFOLIO_NAMES` (default: the portfolio names in `SHARESIGHT_HOLDING_NEWS_RECIPIENTS`),
  matched as a whole word, so listing a first name makes every mention of it a portfolio question.
  If a message got fewer tools than exist and the model needs more, it replies `NEEDS_TOOLS` and the
  bot asks again with everything on (the marker is never shown to anyone).
- **`FAST_MODEL`:** if set, used instead of `MODEL` for requests the gate judged simple.
- **Smaller tool definitions:** descriptions cut to 220 characters, parameter descriptions to their
  first sentence, titles/defaults/examples dropped.
- **Smaller results:** floats rounded to 6 significant digits, long price series thinned to 60
  points, Sharesight holdings flattened into tables, results capped at 12,000 characters; an
  identical call within one request gets a short "same as earlier" note instead of a second copy;
  `cache_ttl` shares results between concurrent users.
- **Compact history:** stored HTML is shown to the model as plain text (ticker links become the
  symbol, tags and Yahoo URLs dropped), lines cut to 240 characters and the bot's own to 160.
- **Smaller prompts:** the system prompt is about half its previous size, the repeated instruction
  tail lives in the (cached) system prompt, images go at low detail.
- **Measure it:** every request is recorded in the `usage` table (`/usage`), and
  `make prompt-report` prints where a typical request's tokens go.

## Running several instances
Instances can run side by side with different environments (for example one on xAI and one on
OpenRouter). Point them at the same `DB_PATH` to share group history (SQLite in WAL mode, safe for
concurrent use) or at different files to keep them apart; DMs are always per bot. Start each with a
different label (`python bot.py grok`, `python bot.py mimo`) to tell them apart in `ps`/`top`.

## Logs
One line per event, without a timestamp when run under systemd (journald adds one):
`[Jane Doe (@jane) 5467329077] first 60 characters of the message ...` (groups add ` @ <chat id>`),
`Round 2: in 14158 (7400 cached) out 738, stop`, and a per-request summary with rounds, tool calls,
tokens, cached % and cost. Tool servers log their tools in short wrapped lines; startup logs the
token size of the tool definitions. The `Starting ...` line shows the git commit the checkout is on.

## Environment variables
| Variable | Meaning |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather (required). |
| `XAI_API_KEY` | xAI key; selects the xAI provider. Set exactly one of the two keys. |
| `OPENROUTER_API_KEY` | OpenRouter key; selects the OpenRouter provider. |
| `MODEL` | Model ID (default grok-4.3 on xAI, z-ai/glm-5.3-flash on OpenRouter). |
| `FAST_MODEL` | Optional cheaper/faster model used for simple requests (TOKEN_SAVER only). |
| `REASONING` | Reasoning effort: low, medium or high; empty = the model's default. |
| `SEARCH` | on|off. Web search (and X search on xAI). Default on. |
| `SEARCH_MODEL` | OpenRouter only: model that runs the searches (default xiaomi/mimo-v2.6-flash:online). |
| `MAX_TOKENS` | Reply cap in tokens, reasoning included (default 3000). |
| `HISTORY_LIMIT` | Messages of chat history in the prompt; the window is HISTORY_LIMIT to 1.5x (default 20). |
| `DB_PATH` | SQLite file for chat history (default chat_log.db). Instances may share it. |
| `BOT_TZ` | Time zone for timestamps, e.g. Australia/Melbourne (default UTC). |
| `MCP_CONFIG` | MCP server config file (default mcp_servers.json; missing = no MCP tools). |
| `ADMIN_CHAT_IDS` | Chat IDs told when an MCP server goes down (comma/space separated; empty = no alerts). |
| `OWNER_USER_ID` | Telegram user ID allowed to use /credits and /usage. |
| `MOVERS_BOTS` | Usernames of bots whose end-of-day big-movers lists get explained. Empty (default) = feature off. |
| `POST_TO_GROUPS_FROM_DM` | on|off. A group admin can have the bot post in that group from a DM (default off). |
| `TELEGRAM_DM_BUTTONS` | on|off. Preset-prompt buttons in private chats (default off). |
| `SHARESIGHT_HOLDING_NEWS_TIME` | HH:MM (BOT_TZ) for the daily holding-news check (default 08:00). |
| `SHARESIGHT_HOLDING_NEWS_RECIPIENTS` | Comma list of portfolio:telegram_username pairs to notify. Empty (default) = daily holding-news DM off. |
| `PORTFOLIO_NAMES` | Comma list of Sharesight portfolio names; a message naming one is treated as a portfolio question (default: the recipients' portfolio names). |
| `TOKEN_SAVER` | on|off. Master switch for the token-saving heuristics (default on). |

## Choosing a model
Figures from search results and Artificial Analysis at the time of writing (October 2026), so
treat them as rough; speed varies by provider.

| Model | $ per 1M in / out | Output speed | Quality index |
|---|---|---|---|
| `xiaomi/mimo-v2.6-pro` | 0.43 / 0.87 | ~28-46 tok/s | 46 (top open-weight model) |
| `xiaomi/mimo-v2.6-flash` | 0.14 / 0.28 | ~56 tok/s | 38 |
| `z-ai/glm-5.3-flash` (default on OpenRouter) | 0.15 / 0.50 | ~50 tok/s (other hosts up to ~270) | 57 |
| `grok-4.3` / `x-ai/grok-4.3` (default on xAI) | 1.25 / 2.50 | ~105-146 tok/s | 25 (at high reasoning) |
| `xiaomi/mimo-v2.5-pro` | 0.30 / 0.61 | ~29-46 tok/s | unreliable (retires 21 Oct 2026) |
| `xiaomi/mimo-v2.5` | 0.12 / 0.24 | ~44-58 tok/s | not found (retires 21 Oct 2026) |

Most of the delay is reasoning time before the first answer token, not typing speed: use
`REASONING=low`, and `FAST_MODEL=xiaomi/mimo-v2.6-flash` so simple messages skip the big model.

## Development
```
pip install -r requirements-dev.txt
make check          # ruff + pytest, a few seconds, no network
make prompt-report  # token breakdown of a sample request
```
The code is the `lib/` package (a map is in `CLAUDE.md`); `bot.py` is a thin entry point. Tests
use fakes for Telegram, both providers and MCP, so nothing in the suite touches the network.
`tests/test_config.py` fails if an environment variable is missing from the table above.

## Troubleshooting
- **"Both XAI_API_KEY and OPENROUTER_API_KEY are set"**: set only one.
- **The bot ignores messages in a group**: run `/setprivacy` -> Disable in @BotFather and re-add it.
- **An MCP server shows as down**: the alert includes the server's own error (often a missing
  environment variable or `npx` not installed). Fix it and restart the bot.
- **Holding news never arrives**: the recipient must have messaged the bot once (so their user ID
  is known), `SHARESIGHT_HOLDING_NEWS_RECIPIENTS` must match the Sharesight portfolio names, and the
  Sharesight server must be connected.
- **Cache percentage stays at 0**: the first request of a conversation is always cold; if later ones
  stay at 0 the model's provider may not support prompt caching.
