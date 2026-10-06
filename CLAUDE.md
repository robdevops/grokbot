# CLAUDE.md

Telegram bot for a stock-chat group: answers @mentions/replies/DMs with an LLM (xAI or OpenRouter),
MCP data tools and web search. User-facing docs are in `README.md`; this file is the map.

## Commands
- `make check`: ruff + pytest (no network, a few seconds). Run it before every commit.
- `make prompt-report`: token breakdown of a sample request. `python bot.py [label]` runs the bot.
- Tests use fakes (`tests/conftest.py`: FakeBot, user_msg; `tests/fakes.py`: ScriptedBackend,
  FakeMcp, fake SDK clients). Add a test next to the module you change; don't write scratch scripts.

## Map (`lib/`, each module < ~300 lines)
- `config.py`: `Settings` from env (`load()`), `ENV_VARS` registry, tuning constants. The only place that
  knows the provider. Every env var must appear in README.md (a test enforces it).
- `store.py`: SQLite `Store`: history (`messages` shared by groups, `dm_messages` per bot), users, kv,
  holding-news state, `usage` ledger. Thread-safe; call through `asyncio.to_thread`.
- `msgtext.py` sender_name/describe. `history.py` transcript lines. `textfmt.py` HTML helpers
  (md_to_html, split_html, plain_text, TG_TAG_RE). `tickers.py` Yahoo links (+ `tickers_data.py`).
- `mcp/`: `server.py` (MCPServer lifecycle, TTL cache, `Registry`), `schema.py` (neutral `ToolDef`,
  schema diet), `results.py` (result slimming).
- `llm/`: `base.py` (Request/Step/Answer/Backend), `xai.py` + `openrouter.py` (the only provider-specific
  code), `runner.py` (THE tool loop), `policy.py` (THE retry ladder), `gate.py` (which tools a message needs).
- `prompts.py` all prompts. `ask.py` one entry point: route -> Request -> policy.ask -> usage -> NEEDS_TOOLS rerun.
- `telegram/`: `handlers.py` (trigger -> request -> reply), `send.py`, `draft.py` (typing + streaming
  drafts), `commands.py` (/credits, /usage). `features/`: `holding_news.py`, `movers.py`, `dm_buttons.py`.
- `app.py` wiring/startup; `context.py` `Ctx` (settings, store, backend, registry, bot); `report.py`.

## Request flow
`handlers.on_message` logs the message -> `_trigger` (mention / reply-to-bot / DM / movers list) ->
`_generate` builds the prompt (`history.format_rows` + `prompts.chat_prompt`) -> `gate.route` picks tools ->
`ask.ask` builds a `Request` -> `policy.ask` -> `runner.run` loops `backend.step` / `run_calls` ->
`format_answer` (md_to_html, link_tickers) -> `reply_chunks` (split_html, save each sent message).

## Conventions
- 4-space indent, ruff-clean, type hints, one-line module docstrings. `features/` modules never import
  each other. Provider differences live only in `llm/xai.py` and `llm/openrouter.py`.
- Parts sent to the model are neutral dicts: `{"type": "text", ...}` / `{"type": "image", "url", "detail"}`;
  tools are `ToolDef`; each backend converts. Don't add `if provider ==` anywhere else.
- No usernames, portfolio names or other personal names in code or docs: they come from env (README uses placeholders).
- New env var: add it to `config.ENV_VARS`, `Settings`, and the README table. Optional features stay off by default.
- Don't make a prompt change without checking `make prompt-report`; budgets are pinned in `tests/test_budgets.py`.

## Gotchas
- DMs: Telegram chat id == user id (positive); each bot's DM has its own message-id sequence, hence
  `dm_messages` keyed by bot. `Store.bot_id` is set in `post_init`; DM reads/writes before that raise.
- Prompt cache: keep stable things first (system, tools sorted, stepped history window); put time and
  the down-servers note last. Changing the system prompt or tool order invalidates every cached prefix.
- xAI must stay on the Responses API (X search); it replays the conversation each round and falls back to
  `previous_response_id` if replay is rejected. OpenRouter errors can arrive inside a 200 body or stream chunk.
- A reply that hits MAX_TOKENS while thinking about tool results is re-asked in the same conversation (2x cap, low
  reasoning), not retried without tools: that would answer from memory and deny having any data.
- `must_search` requests are never retried without tools (answering from memory would invent news).
- Telegram HTML: only b/i/u/s/code/pre/a/blockquote/tg-spoiler. `Draft` strips tags (half-written HTML is rejected).
- No live Telegram/xAI/OpenRouter/MCP access in tests or in this sandbox; say so when behaviour can't be checked.
