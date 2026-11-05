# How it works
[README](../README.md) · [all docs](../README.md#more)

## How it answers
- **Groups:** the bot answers when it is @mentioned or when someone replies to one of its messages. Every message is logged;
  messages from other bots are logged as context but never answered (the one exception is the
  [movers feature](features.md#optional-features-off-by-default)). Edited messages are logged, not answered.
- **Private chats:** every message is answered, and the reply streams into a Telegram message
  draft ("Thinking..." then the text as it arrives). Replies are not quoted in DMs.
- **History:** the prompt carries a window of the chat's recent messages, oldest first. The window
  is at most `HISTORY_MAX` messages (6 to 10 by default, once the chat is longer than that): its start only moves every
  `HISTORY_MAX/2` messages, so the start of the prompt stays identical between requests and the
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
- **Time:** the current date and time (in `TZ`) is included in every prompt.
- **Replies are final:** the prompt tells the model never to say it will "check later"; it makes its
  tool calls first (several at once) and answers in one go.
- **Errors:** a failed request replies with the error text; the bot never goes silent.

## Search
- **xAI:** server-side `web_search` and `x_search` (X/Twitter) tools.
- **OpenRouter:** OpenRouter's `web_search` tool; when it hands a search call back, the bot runs the
  query through `SEARCH_MODEL` (default: `MODEL` with `:online` added) and returns the write-up; that call's cost is added to the answer's cost.
  Some models print a search call as text instead of making it; the bot recovers those queries too.
  In a chat answer the model is told to cite a sentence that rests on a search result as `[1]`, the number linking to the
  page (its exact URL from the results). The bot keeps only links to pages the search really returned. OpenRouter's
  `url_citation` annotations say which pages were used but not which sentence each backs (the positions are 0), so each
  page the model did not cite gets a `[n]` after the sentence its snippet fits best: the figures and distinctive words
  of the sentence also appear in the snippet. A sentence that fits no snippet, such as an opinion, gets none. All the
  numbers follow the order they appear in; there is no list of sources at the end. `DEBUG` logs show each page's best score.
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
- **Yahoo Finance links:** only a ticker written in a marked form is linked to its Yahoo Finance page: `$NVDA`,
  an exchange suffix (`NXT.AX`, `000660.KS`), an exchange prefix (`ASX:NXT`, `NASDAQ: NVDA`, mapped to Yahoo's suffix:
  `ASX:NXT` -> `NXT.AX`), `Name (TICKER)` (bold, name and ticker together, unless already bold), Sharesight's
  `NXT (ASX)`, `^GSPC` and `BTC-USD`. The system prompt asks the model for these forms. A bare word is linked only
  when it is a known ticker: the S&P 500, NASDAQ-100 and S&P/ASX 200 constituents (read through Wikipedia's API) and
  the Sharesight holdings, refreshed once a day in the background and kept in the database, so a reply never waits for
  them. An ASX code links with its suffix (`BHP` -> `BHP.AX`); a code in a US index and in the ASX 200 names two
  companies, so it isn't linked bare unless a holding says which. Any other capitalised word stays plain (`GAAP`, `EBITDA`, `AFR`, `M3`), so there is no stoplist to maintain. An
  exchange suffix settles it on its own (`ALL.AX` is Aristocrat);
  single letters and index tickers that are also everyday words or acronyms (`IT`, `ALL`, `GM`, `UPS`, `HD`) need `$`,
  an exchange prefix or `Name (TICKER)`. A part of the refresh that fails is tried again in half an hour. A bare
  code in brackets names no exchange, so `Goodman Group (GMG)` is linked only if `GMG` is known (it is `GMG.AX`).
  A holding-news digest also links the holdings' own codes, and a movers explanation the listed codes with the list's
  exchange. The visible text drops `$`, the exchange prefix and the suffix (`NXT.AX` shows as `NXT`; `BRK.B` and
  number codes such as `9988` stay as they are) while the URL keeps it; crypto gets `-USD` in the URL. Text inside a
  link, `<code>` or `<pre>` is left alone.
- **Citations:** a raw link in a reply (a bare URL, or a link labelled with its own URL) becomes a
  numbered link, `[1]`, `[2]`, in order of appearance, the same URL keeping its number. Links with a
  real label, and anything in `<code>` or `<pre>`, are left alone.
- Long answers are split under Telegram's limit without cutting a tag or entity; tags still open at
  a split are closed and re-opened in the next message. If Telegram rejects the markup the reply is
  re-sent as plain text.
- Replies are sent silently, without link previews. The bot never @-tags the movers bots.
