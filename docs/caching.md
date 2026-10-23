# Caching and token saving
[README](../README.md) · [all docs](../README.md#more)

## Caching Sharesight history
Questions like "best day / week / month / quarter / year since inception" are answered by the `period_returns` tool
(admins only), which asks Sharesight for one report per period (about 2.4 s each). Every **finished** period is
stored in the SQLite database (next to the chat history, so it survives upgrades with `DATA_DIR`) and **never
expires**: the current, unfinished period is always fetched fresh. Past history rarely changes; after a backdated
trade or correction in Sharesight, flush the cache with `/flushcache [portfolio_id] [year|month|day]` (an admin, in
Telegram) or `python bot.py --flush-cache [portfolio_id] [year|month|day]` (needs only `DB_PATH`/`DATA_DIR`, not the
bot's keys). With a year, month or day, only the periods overlapping it go (every day, week, month, quarter and year
that contains it), and the background fill re-fetches just those.

Without the cache, a request may fetch at most 64 days, 26 weeks or 120 months/quarters/years that aren't cached; a
longer range is refused with the earliest start that fits. Set `PERIOD_BACKFILL=<portfolio name>[,...]` (or `all`) to
fill the cache in the background: months first, then quarters, years, weeks and days (each type for every portfolio before the next), up to two Sharesight calls at a time,
never while a user's request is using the server (about 1,650 calls per portfolio, an hour or two the first time; progress is logged and it resumes after a restart). Once filled, any range is instant and the
limits above no longer matter. Every six hours it fetches whatever newly finished. If five fetches in a row fail (Sharesight down or throttling),
the pass stops and tries again after ten minutes; windows Sharesight simply has no data for are skipped until a restart.

## Caching and token saving
**Provider prompt caching** (xAI and OpenRouter keep a conversation on the server holding its cached
prompt; z.ai caches automatically and gets no key): xAI gets `prompt_cache_key` and the `x-grok-conv-id` header; OpenRouter gets `session_id`,
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
  points, Sharesight holdings flattened into tables, markdown tables (Yahoo) squeezed (padding and rule
  rows dropped, `1.20067e+11` shown as `120.067B`, NaN as `-`), results capped at 12,000 characters; an
  identical call within one request gets a short "same as earlier" note instead of a second copy;
  `cache_ttl` shares results between users: 30 seconds for Yahoo, 30 minutes for Sharesight (so a portfolio
  answer can be up to 30 minutes old). Sharesight's portfolio list and its single-day performance reports are refetched in the background
  just before they expire while people keep asking for them; a query nobody repeats is dropped.
- **Compact history:** stored HTML is shown to the model as plain text (ticker links become the
  symbol, tags and Yahoo URLs dropped), lines cut to 240 characters and the bot's own to 160.
- **Smaller prompts:** the system prompt is about half its previous size, the repeated instruction
  tail lives in the (cached) system prompt, images go at low detail.
- **Measure it:** every request is recorded in the `usage` table (`/credits`), and
  `make prompt-report` prints where a typical request's tokens go.
