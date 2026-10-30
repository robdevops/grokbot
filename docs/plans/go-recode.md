# Go recode plan (saved for later, not started)
Written 2026-10-23 against commit a221d98; updated 2026-10-30 against 71ce0c6 (the head of PR #45, not yet on main), which
adds the **modular design requirement** below plus everything built since: charts, the revenue-breakdown fast path,
`/segments`, `/narrative`, `/events`, `/tokens`, the command menu and the 24 h text cache. Before starting:
- re-check the library versions;
- re-read the Python modules named under "Behaviour-defining files", since the code may have moved on.

**Key requirement: modular design.** A feature is a self-contained unit that can be added, changed or deleted without
touching the core or another feature, so tweaking one feature never bloats the rest. See "Modular design" (it shapes the
layout, the order of work and the checks).

## Open questions to settle first
- **Seams first, in Python?** Carving the feature seams in Python before the port (see "Order of work", step A) improves
  the code now and lets the oracle goldens be sliced per feature. The plan recommends it; the Go port would then
  reproduce an already-proven structure.
- **Chart rendering:** the charts feature draws with matplotlib (line, bar, scatter and donut, light and dark themes, end labels,
  two y axes). A single static binary needs a pure-Go renderer.
  - Recommended: a small hand-written renderer (`internal/kit/chart`, `golang/freetype` or `fogleman/gg`, with DejaVu Sans
    embedded) behind a one-function interface, so it can be swapped.
  - Alternatives: `gonum.org/v1/plot` (less control of the pie/donut and the end labels), or a Python sidecar (breaks the
    one-binary goal).
  - Pixel parity is impossible. Goldens pin the **scene** (the list of draw operations and the text drawn), plus a few
    reference images judged by eye.
- **Verification effort:** the full set is oracle goldens, end-to-end scenarios, fuzzing and a 1–2 week shadow run with
  a second bot. A lighter variant keeps the goldens and the shadow run only.
- **Byte-identical vs semantic parity:**
  - Byte-identical keeps the shared history and prompt-cache prefixes stable when Python and Go bots share a DB. It is
    what the `internal/py` compat layer is for.
  - Semantic parity (same meaning, not the same bytes) would drop most of that layer.
- **Regexes:** `regexp2` lets the Python patterns be copied verbatim. RE2 rewrites avoid the dependency, but every pattern
  needs a hand proof, and Unicode `\b` can't be expressed.

## Context
- **Today:** Python 3.13, ~6,400 lines in `lib/` + `tools/` (6,165 + 244), 439 tests. Features are 2,476 lines of that (charts
  1,304, periods 483, holding news 275, filings 199, post 105, DM buttons 66, movers 44); the biggest single module is
  `features/charts/__init__.py` at 466 lines, past the "< ~300 lines" rule. Importing `lib.app` alone takes ~2.5 s and 82 MB RSS
  (measured with Python 3.13.5). PTB runs up to 256 updates on one asyncio loop; every SQLite call hops to a thread
  under one RLock.
- **Goal:** one static binary that is a drop-in replacement, built as a small core plus self-contained features:
  - same env vars and `/etc/grokbot.env`;
  - same `chat_log.db` schema, readable by both versions, so rollback = start the Python tag on the same DB;
  - same `mcp_servers.json`, and the systemd unit changes only `ExecStart`;
  - same prompts and the same reply and history bytes.
- **Targets:** binary ~20 MB, idle RSS 15–25 MB, start to polling < 100 ms, CPU work in parallel, bounded concurrency.
  - Reply latency stays LLM-bound.
  - The unit's total memory stays dominated by the two npx Node trees.
- **Decisions taken:** keep the Node MCP servers and a generic MCP client; same repo, Go replaces Python at parity;
  xAI + OpenRouter at launch (z.ai was removed).
- **Branch:** one long-lived branch from `main` with a draft PR. Python stays runnable until the deletion commit.

## Modular design (key requirement)
**Aim:** adding, changing or deleting a feature touches one directory and one line. Tweaking a feature (the TickerScout
breakdown took ten rounds of heuristics) grows only that feature.

### Where the Python bot leaks today (measured on 71ce0c6; the Go design must not reproduce these)
| Hot spot | What leaks |
|---|---|
| `telegram/handlers.py` (349 lines) | `_generate` and `_answer` know presets, movers lists, the periods and post tools, and charts (the hint, `prefer_pie`, the photo reply, pages, period buttons, `/segments`); 29 mentions of charts |
| `app.py` | registers each feature's callbacks and handlers, starts their background loops, hard-codes the command menu and `allowed_updates` |
| `store.py` | the schema and methods of charts, chart_files, text_cache, period_cache and the holding-news state |
| `config.py`, `docs/configuration.md` | every feature's settings in one registry and one table |
| `prompts.py` | every feature's prompts (presets, movers, holding news, filings) |
| `telegram/commands.py` | `/tokens` and `/flushcache` beside the filings commands, their rendering and their button callback |
| `llm/gate.py` | feature phrase lists (portfolio words, "what tools do you have") |
| `features/charts/` | the model's chart tool, validation, pie rules, buttons, pages and the TickerScout parsing (`breakdown.py`) in one package, the first at 466 lines |
| data-source heuristics | what TickerScout returns (units, nesting, profit-like slices, "unavailable" breakdowns) is mixed with how it is presented, so each data-shape surprise edits a feature |

### Rules
1. **The core knows no feature.** The kernel (`tg` transport, dispatch, `ask` + `llm`, `mcp`, core store, `reply`,
   `history`, `textfmt`, core `config`) has no import of `internal/features/*`. The one list of features is
   `cmd/grokbot/features.go`, one line per feature: adding a feature adds a line, removing one deletes it.
2. **A feature talks to the world only through ports** handed to it (below), never to the kernel's internals or to another feature.
3. **A feature owns everything it needs:** its settings, tables and migrations, commands, callback prefixes, tools, prompts,
   docs fragment and tests, all in `internal/features/<name>/`.
4. **Shared code is a library, not a feature.** Reusable pure code (chart drawing, pie geometry, HTML helpers) lives in
   `internal/kit/*`; any feature may import it. Features still never import each other.
5. **Data-source shape lives in adapters.** `internal/sources/tickerscout` turns `get_segments`, `get_statements`, `get_narrative` and
   `get_events` results into neutral types (`Grouping{Label, Slices}`, `Section{Heading, Text, Items}`), with its own
   fixtures and goldens. A new quirk in the source is an adapter change, tested there, and no feature file changes.
6. **Small files, reported sizes.** One role per file, each at most ~200 lines, hard limit 300 (a lint fails over it). A
   package over ~1,500 lines must split into sub-features (charts → tool, buttons, pages, segments). `make sizes` prints lines
   per package, and CI posts the change in each PR so growth is visible.

### The feature contract (`internal/feature`)
```go
type Feature interface {
    Name() string
    Register(r Registrar)   // once at startup, in the order of cmd/grokbot/features.go
}

type Registrar interface {
    Setting(Setting)                        // env var: name, default, doc line, parse/validate; feeds the settings docs
    Migrate(sql string)                     // CREATE TABLE IF NOT EXISTS ...; table names start with the feature's name
    Command(Command)                        // name + aliases, help line, who (anyone|admins), handler; fills the /command menu
    Callback(prefix string, h CallbackFunc) // owns every callback whose data starts with the prefix ("ch:", "fl:")
    Trigger(Trigger)                        // may claim a message before the model: Priority, Match(msg), Handle (fast paths)
    Tool(Tool)                              // local model tool: Def, Offer(text) bool, Hint(text), Make(call) Result, Compose(...)
    Decorate(ReplyDecorator)                // e.g. the DM keyboard under every private reply
    Update(kind string, h UpdateFunc)       // extra update kinds ("my_chat_member"); adds to allowed_updates
    Background(name string, run func(context.Context))
    Prompt(PromptPart)                      // text with an id and a token budget, owned by the feature
    Doc(fs embed.FS, path string)           // the feature's docs fragment
}

type Ports struct {                         // what a handler receives; no feature sees the whole store or the app
    Settings Settings     // read-only view of this feature's settings
    DB       FeatureDB    // only this feature's tables, plus generic KV and a TTL text cache (namespaced keys)
    History  HistoryRead  // narrow read access to chat history
    LLM      Asker        // ask.Ask: one entry point, options for route, tools, max tokens and the usage kind
    MCP      Tools        // find a tool by name across servers, call it, list servers and their gate tags
    Reply    Replier      // send, edit, photo, typing, split and save; Query abstraction for button taps
    Access   Access       // IsAdmin(user)
    Clock    Clock
    Log      Logger       // prefixed with the feature name
}
```
- **Replies with attachments are generic.** A tool's `Result` can carry attachments (a photo, its title, markup, an
  `AfterSent(sent)` hook). The kernel sends "the answer as the caption of the first photo" and runs the hook. When several
  attachments come back, the owning tool's `Compose` decides the final reply: charts sends one image plus the page keyboard
  and remembers it. The kernel has no idea what a chart is.
- **Triggers replace the `if` ladder in `_generate`.** Core order: commands → triggers by priority (movers list, DM preset
  tap, the breakdown fast path) → the model path, where each tool's `Offer(text)` decides whether it is offered. `gate` keeps only
  generic routing; feature phrase lists move to the features' `Offer`.
- **Disabled features** are not registered (no commands in the menu, no loops). Their migrations still run, so the schema
  stays identical to Python's and rollback is unchanged.
- **Docs are assembled, not hand-merged.** `docs/configuration.md`'s settings tables and `docs/features.md` are generated from the
  features' declared settings and docs fragments (`make docs`; CI fails on a diff). The test that every env var is
  documented becomes true by construction.

### How today's features map
| Feature | Hooks it uses | Tables | Sources and kit |
|---|---|---|---|
| `admin` (`/tokens`, `/usage`, `/credits`) | Command (admins), menu scope | reads core `usage` | none |
| `periods` (`period_returns`, backfill, `/flushcache`) | Tool, Background, Command (admins) | `period_cache` | Sharesight MCP |
| `holdingnews` (daily DM, `/holdingnews`, `hn:` buttons) | Background, Command, Callback, Prompt | its state keys | Sharesight MCP |
| `movers` | Trigger, Prompt | none | none |
| `dmbuttons` (preset keyboard, startup push) | Trigger, Decorate, Background | its kv state | none |
| `post` (post to groups) | Tool, Update (`my_chat_member`) | reads core `chats` | none |
| `charts` (`make_chart`, `ch:` period buttons, `cp:` pages, preload chat) | Tool (+Compose), Callback ×2, Setting | `charts`, `chart_files` | `kit/chart`, Yahoo MCP |
| `segments` (`/segments`, the plain "TICKER revenue breakdown" fast path) | Command, Trigger | none | `kit/chart`, `sources/tickerscout` |
| `filings` (`/narrative`, `/events`, `fl:` buttons) | Command ×2, Callback, Prompt | uses the generic text cache (24 h) | `sources/tickerscout`, LLM port |

`segments` is split out of `charts` so the model's chart tool and the no-model breakdown no longer share a package;
both draw through `kit/chart`.

### Enforcement (CI, not goodwill)
- **Import-graph test:** the kernel imports no feature; a feature imports only `internal/feature`, `internal/kit/*` and
  `internal/sources/*`, never `internal/features/*` or `internal/telegram`.
- **Feature-removal matrix:** one CI job per feature deletes its directory and its line in `features.go`, then builds and
  runs the remaining tests. A leak fails the job, which proves isolation on every PR.
- **`featuretest` harness:** boots the kernel with a fake Bot API, scripted LLM and fake MCP, registers only the feature under
  test and drives messages and taps. A feature's tests live in its package and need nothing else.
- **Scaffold:** `make feature NAME=x` creates the skeleton (`feature.go` of registration only, `settings.go`, `store.go`,
  `commands.go`, `doc.md`, a test), so a new feature starts modular.
- **Goldens per feature:** each feature has its own `testdata/oracle/<feature>/`, so a change to one feature moves only its goldens.

## Stack
- **Go:** `go 1.25` in go.mod (the MCP SDK's minimum) plus `toolchain go1.27.3`; CI runs `stable`.
  - Build: `CGO_ENABLED=0 go build -trimpath -ldflags "-s -w" -o grokbot ./cmd/grokbot`.
  - Embed `time/tzdata`. The commit hash comes from build info, falling back to reading `.git` like `lib/app.py` `git_hash`.
- **Dependencies** (versions as of Oct 2026; the first four as before, plus the chart renderer, see the open question):
  - `github.com/go-telegram/bot` v1.27: zero deps, Bot API types and methods incl. `SendMessageDraft`. We use only its
    types and calls; the poller is our own.
  - `github.com/modelcontextprotocol/go-sdk` v1.8: `CommandTransport`, `StreamableClientTransport`, in-memory transports for tests.
  - `modernc.org/sqlite`: pure Go, no cgo.
  - `github.com/dlclark/regexp2` v1.12: zero deps, backtracking with lookaround like Python's `re`.
  - Chart renderer: `golang/freetype` or `fogleman/gg`, with DejaVu Sans embedded (about 750 KB; the binary target grows to ~21 MB).
  - Check that the Telegram library's `InlineKeyboardButton` has the `style` field the bot sets today (the shaded "primary" button),
    and `setMyCommands` scopes; otherwise send those through the raw API.
- **LLM providers** run on `net/http` plus our own SSE reader (no openai-go). We replicate the openai 3.31 rules:
  - Up to 2 retries before the stream starts, on connection errors, timeouts, 408, 409, 429 and 5xx.
  - Backoff `min(0.5·2ⁿ, 8 s)` with up to 25% jitter.
  - `retry-after(-ms)` honoured up to 120 s, and `x-should-retry` respected.
  - 180 s per phase (dial, headers, each read), with no overall deadline.

## Layout (Python → Go, each file ≤ ~300 lines; conventions from CLAUDE.md carry over)
| Python | Go |
|---|---|
| `bot.py`, `lib/app.py`, `lib/context.py` | `cmd/grokbot/main.go` (label, `--flush-cache` before config load, unknown args ignored); `internal/app/{app,shutdown,logging,githash}.go`; `internal/appctx` (Ctx + clock) |
| `lib/config.py` | `internal/config` (core settings only: Telegram token, provider keys, model, reasoning, search, timezone, admin IDs, MCP config, `DatabasePath`); every other setting is declared by its feature and aggregated for the docs |
| `lib/store.py` | `internal/store/{store,history,usage,kv,cache}.go` for the core tables plus a generic TTL text cache; each feature keeps its own tables and migrations in `internal/features/<name>/store.go` (the combined schema script stays identical to Python's) |
| PTB parsing, `lib/msgtext.py` | `internal/tg/{poll,types,html,api,errors}.go`: `html.go` = PTB `text_html`/`caption_html` from entities (history bytes must match: UTF-16 offsets, nesting, escaping) |
| `lib/history.py`, `lib/textfmt.py`, `lib/tickers*.py` | `internal/history`, `internal/textfmt/{textfmt,split}.go`, `internal/tickers` |
| `lib/mcp/*` | `internal/mcp/{server,proc,tap,cache,registry,schema,results,sharesight}.go` |
| `lib/llm/*` | `internal/llm/{base,errors,httpc,sse,chat,openrouter,xai,runner,calls,policy,gate}.go` (provider differences only in the two adapters) |
| `lib/prompts.py`, `lib/ask.py` | `internal/prompts` (the shared persona and system prompts only; feature prompts live in their features), `internal/ask` |
| `lib/telegram/send.py`, `draft.py` | `internal/reply` (separate package, otherwise features ↔ handlers would form an import cycle) |
| `lib/telegram/{handlers,access}.py` | `internal/telegram/{dispatch,trigger,generate,access}.go`: generic only (commands, triggers by priority, the model path, callbacks by prefix); no feature name appears here |
| `lib/telegram/commands.py`, `lib/features/*` | `internal/features/{admin,periods,holdingnews,movers,dmbuttons,post,charts,segments,filings}`, each with the same file roles; `internal/feature` (the contract), `internal/kit/{chart,textfmt,pie}` (shared libraries), `internal/sources/tickerscout` (the adapter) |
| `tools/report.py`, `capture.py` | `tools/promptreport`, `tools/capture` (Go); `period_cache_without_rowid.py` stays on the Python tag |

## Python-compat layer (`internal/py/*`, only what the code uses, each pinned by goldens)
- **`pyre`:** Python patterns are copied verbatim and compiled on regexp2. A small rewriter maps `\w \s \d \b \Z` to
  CPython's Unicode classes (tables generated from CPython). It implements `Sub/Split(maxsplit)/Search/FullMatch` with
  Python semantics and a match timeout, which matters because messages are untrusted.
  - This covers the 8 lookaround patterns (MD_BOLD, ONE_LETTER_RE, the Name (TICKER) pattern, CITE_ONLY_RUN_RE,
    `(?<=</a>)`, NO_CONTEXT, mentions, `(?<=[.;])\s`) and the ~24 that rely on Unicode `\b`.
- **`pystr`:**
  - rune-based length, slicing and find, because every cut and budget counts code points;
  - Python's whitespace set for `split()`/`strip()`, and `splitlines()`;
  - `lower()` special cases where Go differs.
- **`pyjson`:**
  - ordered values, with ints kept as text;
  - `json.loads` rules (NaN/Infinity, duplicate keys);
  - three dump profiles: compact with `ensure_ascii=False` (results), the default spacing (`period_cache.movers`), and
    `sort_keys` (cache keys);
  - Python float repr; never escapes `<>&` or U+2028.
  - Also used to read `mcp_servers.json`, whose key order sets the server order in prompts.
- **`pyfmt`:** `+.2f`, `+,.0f`, `.6g` with T/B/M suffixes (keeping the `infT` quirk).
- **`pyhtml`:** `Escape` like `html.escape` (`&#x27;`, `&quot;`). `Unescape` is Go's `html.UnescapeString`, with
  CPython's rules ported only where the goldens differ.
- **`pydifflib`:** `SequenceMatcher` and `get_close_matches(n=3, cutoff=0.75)`, used by `post.py` and the
  tool-name suggestions.
- **`pydate`:** the `fromisoformat`/strptime grammar used by periods and `/flushcache`.

## Concurrency and footprint
- **Updates:**
  - Our own long poller: 50 s timeout, the same `allowed_updates` as `lib/app.py` `allowed_updates`.
  - 256 workers read from an unbuffered channel. That matches PTB's `concurrent_updates`, and adds backpressure instead of
    an unbounded task list.
  - No per-chat lock, as today.
  - `dispatch.go` keeps PTB's handler order:
    1. commands, including edited messages;
    2. group and private messages that are not status updates;
    3. `^hn:` callbacks;
    4. `my_chat_member`.
  - Replies quote in groups only and keep `message_thread_id`.
- **SQLite:**
  - One writer `*sql.DB` with a single connection; writes are single statements or `BEGIN IMMEDIATE`, so `period_put` is
    one transaction.
  - One reader `*sql.DB` with 4 `query_only` connections. The history COUNT and SELECT run in one snapshot.
  - Pragmas WAL, `synchronous=NORMAL`, `busy_timeout=30000`. No new tables, pragmas or `user_version`.
  - Python and Go bots can share one file.
- **MCP:**
  - One go-sdk session per server, with concurrent `CallTool`.
  - A call counts as active before it waits on the per-server semaphore (default 4), so the backfill sees queued calls;
    the 60 s timeout starts inside the slot.
  - TTL cache plus an in-flight map. The shared fetch runs on a detached context, like `asyncio.shield`.
  - keep_warm cadence `min(30, max(1, ttl/10))` s, as in `lib/mcp/server.py`.
  - `tap.go` wraps the connection to keep the raw `tools/list` JSON. go-sdk decodes `inputSchema` into a map, which would
    lose key order, and turns `readOnlyHint` into a bool, losing "absent".
- **Stdio children:**
  - Env is HOME, LOGNAME, PATH, SHELL, TERM, USER plus the expanded `env`, as the Python SDK does. Go would otherwise
    hand the children the API keys.
  - `Setsid`.
  - stderr goes to a ring buffer; its last 800 chars go into the down alert.
  - Close: shut stdin, wait 2 s, SIGTERM the process group, wait 2 s, SIGKILL.
  - A watcher on `session.Wait()` marks the server down.
- **LLM:**
  - One shared `http.Transport` (HTTP/2, keep-alive).
  - A round's tool calls run concurrently; results come back in call order.
  - Atomics for the xAI stateless flag.
  - xAI replays the raw output-item JSON from `response.completed`, with null fields stripped to match `model_dump(exclude_none=True)`.
- **Background loops** (holding news, retries, backfill × 2 workers, DM-button push, keep-warm) run under one root context.
- **SIGTERM:**
  1. Stop polling.
  2. Confirm the offset with `getUpdates(offset=last+1, timeout=0)` like PTB, so a restart never re-answers.
  3. Wait up to 80 s for running handlers.
  4. Cancel the loops.
  5. Close MCP, then the DB (which checkpoints the WAL).
- **Runtime defaults** (overridable from the env file): GOMAXPROCS ≤ 4 and a 64 MiB soft memory limit.
- **Logging:** the same message texts and levels; debug/info to stdout, warning/error to stderr. When a stream is the
  journal stream, every line carries its `<N>` priority (as `lib/app.py` `JournalFormatter` does); otherwise each
  line gets a timestamp.

## Parity: Python is the oracle until it is deleted
- **Generator `tools/oracle/`** (Python, imports `lib`, deleted at cut-over).
  - `make oracle` writes `testdata/oracle/<pkg>/<func>.jsonl`: a meta line with versions and seed, then one
    `{"id","args","out"|"raises"}` per case.
  - CI's `make oracle-check` regenerates and diffs, so fixes merged from `main` can't drift silently.
  - Corpus: every literal from the existing tests, the fixtures plus mutations, seeded random Unicode text and entities,
    float bit patterns, and env-var matrices.
- **What the goldens cover:**
  - every compat helper;
  - `config.load` and its error texts;
  - history window sizes;
  - `text_html`/`describe`/`sender_name`, `compact`/`format_rows` (incl. DST);
  - `md_to_html`, `split_html`, `plain_text`, `Draft._render`;
  - tickers, and gate/route over phrase lists;
  - every prompt for every Route and provider;
  - the schema diet and `slim_result` over all fixtures;
  - cache keys and `_warmable`;
  - provider request bodies for scripted Requests;
  - xAI `answer_text` over event sequences;
  - the period windows and the tool's text;
  - `keyboard_hash`. These bytes must match, or every DM gets "Buttons updated." at cut-over.
- **Traces:** scripted backend and fake MCP scripts run through `runner.run`, `policy.ask` and `ask.ask`, recording
  requests, tool results, answer, usage and errors. Go replays them through `internal/llm/llmtest` and `internal/mcp/mcptest`.
- **End-to-end scenarios (~20):**
  - Go fakes in `internal/testkit/fakeapi`: a Bot API, scripted SSE providers, and a stdio MCP server over the fixtures.
  - The Python app (base URLs patched, frozen clock) and the Go app both run against them.
  - Compared: the outbound requests and the final `sqlite3 .dump`.
- **Ported tests:** every pytest case gets a Go twin with the same name next to its package (a feature's twins live in the feature's directory).
  - FakeBot → fakeapi via `bot.WithServerURL`; ScriptedBackend → llmtest; FakeMcp → an in-memory go-sdk server built
    from `testdata/fixtures`; fake SDK clients → httptest SSE servers that assert the request JSON.
  - A CI script fails while any pytest test lacks a twin.
  - `go test -race` always.
- **Fuzzing:** native fuzz targets for split_html invariants, link_tickers, pyjson round-trips, slim_result, text_html
  offsets and the SSE decoder, seeded from the goldens. Optionally diffed against a Python worker.

## Order of work (each step leaves `make check` green: ruff + pytest + oracle-check + gofmt/vet + `go test -race`)
A. **Carve the seams in Python first (recommended; settle the open question).** Add `lib/feature.py` with a small
   registrar, then move each feature behind it, one commit each: its tables and migrations out of `store.py`, its settings and
   docs out of the central tables, its prompts out of `prompts.py`, its commands and callbacks out of `app.py` and `commands.py`,
   its tool offer out of `_generate`, and the photo reply out of `_answer` (generic attachments). Split `charts` into tool, buttons,
   pages and `segments`; extract the TickerScout parsing into `lib/sources/tickerscout.py`. Add the import-graph test and the
   feature-removal check in Python. Behaviour must not change (the existing tests are the check).
0. **Scaffolding:** go.mod, a `cmd` stub, Makefile and CI (setup-go stable alongside the Python steps). Move
   `tests/fixtures` → `testdata/fixtures` (update the Python paths). Oracle skeleton and `internal/py/*`.
1. **Leaf packages:**
   - `config` (keeps the docs env-var test), and `store`, plus a cross test: Python and Go open one DB file, in both directions.
   - `tg` types and HTML, `msgtext`, `history`, `textfmt`, `tickers`, `prompts`, `gate`.
   - `internal/feature` (the contract and registrar), `featuretest`, the import-graph test and the removal-matrix CI job:
     they come before any feature is ported.
2. **MCP:** `mcp` and `mcptest`, plus a manual smoke test against the real npx servers.
3. **LLM:** `httpc`, `sse`, the three providers, `runner`, `policy`, `ask`.
4. **App:** `reply`, `telegram` (generic dispatch), `app` (logging, `--flush-cache`, shutdown), `tools/promptreport` with
   `tests/test_budgets.py`'s limits (`len//4` in code points, now per feature plus a total), and `tools/capture`.
   Then **the features, one at a time, each complete before the next** (code, settings, tables, docs fragment, goldens, tests):
   `admin`, `movers`, `dmbuttons`, `post`, `periods`, `holdingnews`, `filings` (with the `sources/tickerscout` adapter),
   then `kit/chart` with `charts` and `segments`. The simple ones first prove the contract before the large one uses it.
5. **Parity checks:** scenarios, fuzzing, benchmarks. Then each chosen fix as its own commit, updating the goldens and
   `testdata/oracle/DIVERGENCES.md`.
6. **Shadow run, 1–2 weeks:**
   - A second BotFather bot in the same groups.
   - `grokbot-shadow.service` is the same unit plus `User=grokbot` (same dynamic UID, so it shares
     `/var/lib/grokbot/chat_log.db`), `EnvironmentFile=/etc/grokbot-shadow.env` and `ExecStart=/opt/grokbot/grokbot shadow`.
   - The shadow env turns off holding news, movers, buttons, posting and backfill, and sets `ADMIN_CHAT_IDS` to the maintainer.
   - What to compare:
     - ask both bots the same things;
     - diff the summary lines;
     - kill a Node server;
     - restart under traffic;
     - watch `systemd-cgtop`.
   - Record anonymised real SSE into `testdata/fixtures/llm`.
7. **Cut-over:**
   - Point `ExecStart` at `/opt/grokbot/grokbot` and restart. Check the Starting hash, "MCP … connected", a group
     mention, a DM draft and `/credits`.
   - Rollback = the old `ExecStart` line, same checkout/DB/env.
   - Then tag `python-final` and make the deletion commit:
     - delete `lib/`, `bot.py`, `tests/*.py`, `tools/*.py` + `tools/oracle`, `requirements*.txt`, `pyproject.toml`, and
       the Python lines in `.gitignore`;
     - update the Makefile (`check`, `build`, `prompt-report`), `.github/workflows/check.yml` (Go + Node, keeping the
       weekly run and the Telegram ping), CLAUDE.md (Go map + compat gotchas), README.md quick start, `docs/running.md`,
       `docs/development.md`, `docs/how-it-works.md`, `docs/configuration.md`, and the
       `ExecStart` line of `deploy/systemd/grokbot.service`.
   - The goldens stay frozen in `testdata/` and change only on purpose (`-update`).

## Preserve vs fix
- **Preserve** (goldens pin these):
  - Unicode `\w`/`\b`; cuts in code points; `infT`; NaN shown as `-`;
  - Python bool-as-int checks;
  - tools sorted per server, `label__tool`[:64];
  - one `tools/list` page;
  - OpenRouter annotations counted as searches; the process-wide xAI stateless flag;
  - edited `/credits` re-runs;
  - no retry on Telegram 429;
  - INSERT OR REPLACE and the startup DELETEs;
  - `SEARCH` off only for exactly `off`.
- **Fix after parity, one commit each** (the ones also present in the Python bot can be fixed there first):
  1. An `error` inside an OpenRouter stream chunk gets the `raise_provider_error` handling it was meant to have
     (429/503/529 → busy reply). Today the openai SDK raises `APIError` first, so it bypasses the ladder.
  2. xAI `add_user_message` also sets `pending`, so the MAX_TOKENS re-ask works after the `previous_response_id` fallback.
  3. `seconds_until` across DST, so holding news fires on time on change days.
  4. Don't cache `_bg` backfill calls: today keep_warm re-fetches each one once.
  5. An MCP child that exits without an SDK error is marked down. Confirm first with a scenario.
- **Accepted divergences:** library and OS error text inside error strings, tracebacks, and lone surrogates becoming U+FFFD.

## Risks → mitigations
- Text and HTML drift splits the shared history or the prompt cache → goldens from PTB/CPython, fuzzing, a shadow week on the shared DB.
- go-sdk differences (ordering, annotations, process handling) → tap, process tests, pinned version, npx smoke test.
- Real provider streams differ from the fakes → SSE recorded during the shadow run becomes fixtures.
- Races and leaks → `-race`, goroutine-leak checks, a 500-update burst test.
- Rollback breaks → no schema change; the Python↔Go DB cross test.
- Prompts become compiled in, so retasking needs a rebuild → documented in README.
- The feature contract is too thin or too wide and features work around it → port the small features first and change the contract
  while it has few users; the contract is a few hooks, and anything a feature needs twice becomes a port.
- Modularity erodes as features grow → the import-graph test, the removal matrix, the 300-line limit and the per-PR size report fail the build.
- Chart looks differ from matplotlib → scene goldens plus reference images judged by eye; the renderer sits behind one function so it can be swapped.
- TickerScout output keeps changing shape → fixtures per ticker in `sources/tickerscout`, so a new quirk is one adapter change with its test.

## Verification
- **Parity:**
  - oracle, trace and scenario tests all green, and every pytest case has a Go twin;
  - ≥ 1 h of fuzzing per target with no unexplained diff;
  - DIVERGENCES.md reviewed;
  - Python reads every row Go writes, and vice versa.
- **Modularity** (checked on every PR):
  - the import-graph test passes;
  - each feature-removal job builds and passes without that feature;
  - a feature's directory is the only place its settings, tables, prompts, commands and docs are defined;
  - `make sizes` shows no file over 300 lines and no unexplained jump in a package;
  - adding the first new feature after the port (try one small one) touches its directory and one line in `features.go`.
- **Speed and footprint** (numbers go in the PR):
  - `hyperfine` of `--flush-cache 99999 2024` (same path, no network), Python vs Go;
  - VmRSS/VmHWM of the bot process alone, idle and after a 500-update burst against fakeapi with 2 s of scripted LLM latency;
  - in production, `systemd-cgtop`/`cpu.stat` for both units over the shadow week;
  - `go test -bench` on link_tickers, slim_result and format_rows;
  - stripped binary size.
- **Not checkable in a sandbox without live access** (Telegram/xAI/OpenRouter/MCP): live behaviour. It is covered by the
  shadow run.

## Behaviour-defining files to read while porting
`lib/mcp/server.py`, `lib/mcp/results.py`, `lib/mcp/schema.py`, `lib/llm/runner.py`, `lib/llm/xai.py`, `lib/llm/chat.py`,
`lib/telegram/handlers.py`, `lib/telegram/commands.py`, `lib/telegram/draft.py`, `lib/tickers.py`, `lib/textfmt.py`, `lib/store.py`,
`lib/features/periods/*`, `lib/features/charts/*` (the tool, pie rules, buttons, pages, `breakdown.py` and `render.py`),
`lib/features/filings.py`,
`tests/fakes.py`, `tests/conftest.py`, and `Message._parse_html` in the installed python-telegram-bot (22.8 at the time of writing).
