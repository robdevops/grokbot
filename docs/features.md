# Features and commands
[README](../README.md) · [all docs](../README.md#more)

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
  If the AI provider is at capacity, the daily check is retried every 30 minutes for up to 12 hours
  (nothing is sent to anyone meanwhile).
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

## Charts
Ask for a chart, graph or plot ("chart NVDA against AMD over a year, AMD on the right axis, inverted") and the bot
replies once: the chart as a photo with its answer as the caption (2 or 3 charts come as one album). An answer too
long for a caption (about 1,000 characters) continues in a text message. The model gets the `make_chart` tool only
when the message asks for one (chart, graph, plot, visualise), so other requests don't pay for its definition.
- **Style:** a bold title on the left, the legend under it, no frame, no vertical rules, light horizontal rules and
  a darker line at zero when the values cross it. Tick labels share their decimals, shorten big numbers (120M, 1.5B)
  and show % for % changes.
- **Axes:** a left and a right y axis, each with as many series as needed (lines, bars or points). Either axis or
  both can be inverted; an inverted axis is labelled "(inverted)".
- **Data:** a series is either a Yahoo symbol (NVDA, CBA.AX, ^GSPC, AUDUSD=X, GC=F, BTC-USD), whose closing prices
  the bot fetches itself from the Yahoo MCP server's `get_historical_prices`, or values the model passes from other
  tools or the chat (Sharesight, `period_returns`, search results). `percent_change` rebases a series to its % change,
  to compare tickers on one axis.
- **Price history:** the interval follows the period so the history fits in one Yahoo response (daily up to 3 months,
  weekly for 6 months and a year, monthly for 2 and 5 years, quarterly beyond), at most 60 points. The model is told
  each series' first and last value, change, low and high, so it can comment on a chart whose prices it never saw.
- **Cost:** matplotlib loads on the first chart and keeps its font cache in `matplotlib/` inside `DATA_DIR`. A chart
  takes about half a second to draw (about a second for the first), and after the first one the bot uses about 50 MB
  more memory.

## Commands
| Command | Who | What |
|---|---|---|
| `/start` | DM | Greeting (and the buttons if enabled) |
| `/holdingnews` | DM, if holding news is on | Run the holding-news check now |
| `/credits` | an admin (see below), in a group or a DM | Provider balance (OpenRouter; xAI and z.ai have none to show), then requests, tokens (and % cached) and cost per model for the last 24 h and 7 days |
| `/flushcache [portfolio_id] [year or month or day]` | an admin | Forget cached Sharesight period returns: all, or of one portfolio, or those overlapping a year (`2024`), month (`2024-05`) or day (`2024-05-17`), e.g. `/flushcache 684141 2024`; see [Caching Sharesight history](caching.md#caching-sharesight-history) |

**Admins** are the user IDs in `ADMIN_CHAT_IDS` plus anyone who administers a group the bot is in (checked with
Telegram, cached for five minutes). The bot only knows a group once someone has written in it, or it was added, since
it started tracking groups. Admins who post anonymously (as the group) can't be recognised. With `ADMIN_ONLY` on (the
default), only admins can DM the bot, and Sharesight data is for admins only: anyone else asking about a portfolio
gets "Portfolio data is for admins only." Holding-news DMs, buttons and down-server messages the bot sends itself are
unaffected. With nobody in `ADMIN_CHAT_IDS` and no known group, nobody can DM the bot or reach Sharesight: set
`ADMIN_ONLY=off` to allow everyone.
