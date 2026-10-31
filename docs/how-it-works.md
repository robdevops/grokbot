# How it works
[README](../README.md) · [all docs](../README.md#more)

## How it answers
- **Groups:** the bot answers when it is @mentioned or when someone replies to one of its messages. Every message is logged;
  messages from other bots are logged as context but never answered (the one exception is the
  [movers feature](features.md#optional-features-off-by-default)). Edited messages are logged, not answered.
- **Private chats:** every message is answered, and the reply streams into a Telegram message
  draft ("Thinking..." then the text as it arrives). Replies are not quoted in DMs.
- **History:** the prompt carries a window of the chat's recent messages, oldest first. The window
  is `HISTORY_LIMIT` to 1.5x that many messages (20-29 by default): its start only moves every
  `HISTORY_LIMIT/2` messages, so the start of the prompt stays identical between requests and the
  provider's prompt cache keeps working. The bot's own earlier replies appear as "You" (its latest reply in full, so follow-up questions about it
  work; older ones shortened); a replied-to
  message is quoted even if it is older than the window.
- **`#nocontext`:** put `#nocontext` anywhere in a message that tags the bot (or in a DM) and the model gets that
  message alone: no chat history, and nothing quoted from the message it replies to (no photo from it either). The tag
  itself is removed before the model sees it. Useful for a fresh question that earlier chat would only confuse
  or make more expensive; the bot's own recent reply is not read either, so follow-ups need the full question.
- **Shared database:** group history lives in `messages`, shared by every bot started with the same
  `DATA_DIR` (so two bots in one group see each other's replies). Private chats live in
  `dm_messages`, keyed by bot, because each bot's DM with a person has its own message-ID sequence.
- **Images:** photos in the mention, or in the message being replied to, are sent to the model
  (up to 2). A mid-size copy is sent at low detail, unless the text asks the
  model to read it (read, text, chart, table, screenshot...).
  **Stickers** count too, when they are the message the bot answers (any sticker in a DM, or one sent as a reply to the
  bot) or the message being replied to ("@bot what does this sticker mean?"); other stickers in a group are never sent. A static
  sticker goes as the WebP image it is, with no conversion; an animated or video one goes as its preview frame. Older
  stickers in the history are still just their emoji.
- **Charts:** asked for a chart, graph or plot, the model can draw one and it is sent as a photo with the reply
  (see [Charts](features.md#charts)).
- **"What tools do you have?":** a message like "what tools do you have", "list your capabilities" or "what can you
  do" is answered from the actual tool list (about 400 tokens in the prompt, no tool offered): the data sources and
  their tools, web search, charts, and the other local tools. Tools the asker may not use (Sharesight for a
  non-admin, posting to groups when it is off) are left out.
- **Time:** the current date and time (in `BOT_TZ`) is included in every prompt.
- **Replies are final:** the prompt tells the model never to say it will "check later"; it makes its
  tool calls first (several at once) and answers in one go.
- **Errors:** a failed request replies with the error text; the bot never goes silent.

## Search
- **xAI:** server-side `web_search` and `x_search` (X/Twitter) tools.
- **OpenRouter:** OpenRouter's `web_search` tool; when it hands a search call back, the bot runs the
  query through `SEARCH_MODEL` (default `xiaomi/mimo-v2.6-flash:online`) and returns the write-up; that call's cost is added to the answer's cost.
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
off); `keep_warm` (tools whose cached results are refetched just before they expire, while people keep asking: a list of
tool names, or `{name: longest start_date..end_date window in days, 0 = a single day}` for tools with dates, so a longer or undated
call is cached but not refreshed); `slim` (result slimming, defaults to the server's name; `sharesight` flattens holdings);
`hide_params` (optional parameters kept out of the tool definition because the model never needs them,
saving tokens every round; a required parameter is never hidden); `gate` (`portfolio` = only offered when the question is about portfolios/holdings);
`current_holdings_only` (Sharesight: never include sold holdings);
`chat_commands` (slash commands for the server's tools, below).
- **Chat commands:** `"chat_commands": {"forecast": {"tool": "get_forecast", "require_arg": true}, "alerts": {"tool": "get_alerts"}}`
  makes `/forecast …` and `/alerts` commands, in groups and DMs, listed in the command menu (an optional `"description"`
  sets the menu text). The message is answered like a mention, with the chat history. The difference is that the model
  is offered only that one tool, has to call it on its first round, and is never retried without it (a failure is an
  error reply, not an answer from memory). `require_arg`: a bare `/forecast` gets
  "Usage: /forecast <what to look up>" without asking the model. A command whose server is down, or whose tool isn't
  enabled (blocked, or not read-only and missing from `allowed_tools`), says so. A command on a `gate: portfolio`
  server is for admins only. Names follow Telegram's rules (lowercase letters, digits and `_`, at most 32). A name the
  bot already uses (`/tokens`, `/clear`, …), a name used twice, or an entry without a `tool` is skipped, and the startup
  log warns about each, and about a tool that isn't enabled.
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
- **Slimming:** results are compacted before the model sees them (see [token saving](caching.md#caching-and-token-saving)).

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
