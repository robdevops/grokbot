# Go recode plan (saved for later, not started)
Written 2026-10-23 against commit a221d98. Before starting:
- re-check the library versions;
- re-read the Python modules named under "Behaviour-defining files", since the code may have moved on.

## Open questions to settle first
- **Verification effort:** the full set is oracle goldens, end-to-end scenarios, fuzzing and a 1–2 week shadow run with
  a second bot. A lighter variant keeps the goldens and the shadow run only.
- **Byte-identical vs semantic parity:**
  - Byte-identical keeps the shared history and prompt-cache prefixes stable when Python and Go bots share a DB. It is
    what the `internal/py` compat layer is for.
  - Semantic parity (same meaning, not the same bytes) would drop most of that layer.
- **Regexes:** `regexp2` lets the Python patterns be copied verbatim. RE2 rewrites avoid the dependency, but every pattern
  needs a hand proof, and Unicode `\b` can't be expressed.

## Context
- **Today:** Python 3.13, ~4,650 lines in `lib/` + `tools/`, 297 tests. Importing `lib.app` alone takes ~2.5 s and 82 MB RSS
  (measured with Python 3.13.5). PTB runs up to 256 updates on one asyncio loop; every SQLite call hops to a thread
  under one RLock.
- **Goal:** one static binary that is a drop-in replacement:
  - same env vars and `/etc/grokbot.env`;
  - same `chat_log.db` schema, readable by both versions, so rollback = start the Python tag on the same DB;
  - same `mcp_servers.json`, and the systemd unit changes only `ExecStart`;
  - same prompts and the same reply and history bytes.
- **Targets:** binary ~20 MB, idle RSS 15–25 MB, start to polling < 100 ms, CPU work in parallel, bounded concurrency.
  - Reply latency stays LLM-bound.
  - The unit's total memory stays dominated by the two npx Node trees.
- **Decisions taken:** keep the Node MCP servers and a generic MCP client; same repo, Go replaces Python at parity;
  xAI + OpenRouter + z.ai at launch.
- **Branch:** one long-lived branch from `main` with a draft PR. Python stays runnable until the deletion commit.

## Stack
- **Go:** `go 1.25` in go.mod (the MCP SDK's minimum) plus `toolchain go1.27.3`; CI runs `stable`.
  - Build: `CGO_ENABLED=0 go build -trimpath -ldflags "-s -w" -o grokbot ./cmd/grokbot`.
  - Embed `time/tzdata`. The commit hash comes from build info, falling back to reading `.git` like `lib/app.py` `git_hash`.
- **Four dependencies** (versions as of Oct 2026):
  - `github.com/go-telegram/bot` v1.27: zero deps, Bot API types and methods incl. `SendMessageDraft`. We use only its
    types and calls; the poller is our own.
  - `github.com/modelcontextprotocol/go-sdk` v1.8: `CommandTransport`, `StreamableClientTransport`, in-memory transports for tests.
  - `modernc.org/sqlite`: pure Go, no cgo.
  - `github.com/dlclark/regexp2` v1.12: zero deps, backtracking with lookaround like Python's `re`.
- **LLM providers** run on `net/http` plus our own SSE reader (no openai-go). We replicate the openai 3.31 rules:
  - Up to 2 retries before the stream starts, on connection errors, timeouts, 408, 409, 429 and 5xx.
  - Backoff `min(0.5·2ⁿ, 8 s)` with up to 25% jitter.
  - `retry-after(-ms)` honoured up to 120 s, and `x-should-retry` respected.
  - 180 s per phase (dial, headers, each read), with no overall deadline.

## Layout (Python → Go, each file ≤ ~300 lines; conventions from CLAUDE.md carry over)
| Python | Go |
|---|---|
| `bot.py`, `lib/app.py`, `lib/context.py` | `cmd/grokbot/main.go` (label, `--flush-cache` before config load, unknown args ignored); `internal/app/{app,shutdown,logging,githash}.go`; `internal/appctx` (Ctx + clock) |
| `lib/config.py` | `internal/config` (`ENV_VARS`, `Settings`, constants, `DatabasePath`) |
| `lib/store.py` | `internal/store/{store,history,period}.go` (same schema script and startup DELETEs) |
| PTB parsing, `lib/msgtext.py` | `internal/tg/{poll,types,html,api,errors}.go`: `html.go` = PTB `text_html`/`caption_html` from entities (history bytes must match: UTF-16 offsets, nesting, escaping) |
| `lib/history.py`, `lib/textfmt.py`, `lib/tickers*.py` | `internal/history`, `internal/textfmt/{textfmt,split}.go`, `internal/tickers` |
| `lib/mcp/*` | `internal/mcp/{server,proc,tap,cache,registry,schema,results,sharesight}.go` |
| `lib/llm/*` | `internal/llm/{base,errors,httpc,sse,chat,openrouter,zai,xai,runner,calls,policy,gate}.go` (provider differences only in the three adapters) |
| `lib/prompts.py`, `lib/ask.py` | `internal/prompts`, `internal/ask` |
| `lib/telegram/send.py`, `draft.py` | `internal/reply` (separate package, otherwise features ↔ handlers would form an import cycle) |
| `lib/telegram/{handlers,commands,access}.py` | `internal/telegram/{dispatch,trigger,handlers,generate,commands,access}.go` |
| `lib/features/*` | `internal/features/{holdingnews,movers,dmbuttons,post,periods}` (features never import each other) |
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
- **Ported tests:** every pytest case gets a Go twin with the same name next to its package.
  - FakeBot → fakeapi via `bot.WithServerURL`; ScriptedBackend → llmtest; FakeMcp → an in-memory go-sdk server built
    from `testdata/fixtures`; fake SDK clients → httptest SSE servers that assert the request JSON.
  - A CI script fails while any pytest test lacks a twin.
  - `go test -race` always.
- **Fuzzing:** native fuzz targets for split_html invariants, link_tickers, pyjson round-trips, slim_result, text_html
  offsets and the SSE decoder, seeded from the goldens. Optionally diffed against a Python worker.

## Order of work (each step leaves `make check` green: ruff + pytest + oracle-check + gofmt/vet + `go test -race`)
0. **Scaffolding:** go.mod, a `cmd` stub, Makefile and CI (setup-go stable alongside the Python steps). Move
   `tests/fixtures` → `testdata/fixtures` (update the Python paths). Oracle skeleton and `internal/py/*`.
1. **Leaf packages:**
   - `config` (keeps the docs env-var test), and `store`, plus a cross test: Python and Go open one DB file, in both directions.
   - `tg` types and HTML, `msgtext`, `history`, `textfmt`, `tickers`, `prompts`, `gate`, `periods/windows`.
2. **MCP:** `mcp` and `mcptest`, plus a manual smoke test against the real npx servers.
3. **LLM:** `httpc`, `sse`, the three providers, `runner`, `policy`, `ask`.
4. **App:** `reply`, `telegram`, the features, `app` (logging, `--flush-cache`, shutdown), `tools/promptreport` with
   `tests/test_budgets.py`'s limits (`len//4` in code points), and `tools/capture`.
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
       `docs/development.md`, `docs/how-it-works.md`, `docs/configuration.md` (z.ai price table path), and the
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
  1. An `error` inside an OpenRouter/z.ai stream chunk gets the `raise_provider_error` handling it was meant to have
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

## Verification
- **Parity:**
  - oracle, trace and scenario tests all green, and every pytest case has a Go twin;
  - ≥ 1 h of fuzzing per target with no unexplained diff;
  - DIVERGENCES.md reviewed;
  - Python reads every row Go writes, and vice versa.
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
`lib/telegram/handlers.py`, `lib/telegram/draft.py`, `lib/tickers.py`, `lib/textfmt.py`, `lib/store.py`, `lib/features/periods/*`,
`tests/fakes.py`, `tests/conftest.py`, and `Message._parse_html` in the installed python-telegram-bot (22.8 at the time of writing).
