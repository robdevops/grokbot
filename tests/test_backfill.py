import asyncio
import json
from datetime import date, timedelta

import pytest

from lib import config
from lib.context import Ctx
from lib.features.periods import backfill
from lib.features.periods.windows import key, period_windows

from .conftest import FakeBot
from .fakes import FakeMcp, ScriptedBackend, registry


class Sharesight(FakeMcp):
    def __init__(self, *a, fail=False, inception="2026-01-15", **kw):
        super().__init__(*a, **kw)
        self.fail = fail
        self.fail_text = "Error: timed out"
        self.data_from = ""
        self.inception = inception
        self.busy_for = 0  # polls during which the server counts as busy
        self.polls = 0
        self.delay = 0
        self.running = self.peak = 0
        self.order: list[tuple[str, str]] = []

    def user_calls(self):  # a user's request is running for the first few polls
        self.polls += 1
        return ["get_performance_report portfolio_id=7 2024-01-01..2024-12-31"] if self.polls <= self.busy_for else []

    async def call(self, tool, args):
        self.calls.append((tool, args))
        if tool == "list_portfolios":
            return json.dumps({"portfolios": [{"id": 7, "name": "RobSMSF", "inception_date": self.inception},
                                              {"id": 8, "name": "Other", "inception_date": "2026-01-15"}]})
        self.order.append((args["start_date"], args["end_date"]))
        self.running += 1
        self.peak = max(self.peak, self.running)
        await asyncio.sleep(self.delay)
        self.running -= 1
        if self.fail:
            return self.fail_text
        if args["end_date"] < self.data_from:  # nothing in Sharesight before this date
            return "Tool error: no data for this period"
        return json.dumps({"report": {"total_gain_percent": 1.0, "total_gain": 10.0, "currency": "AUD"}})


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(backfill, "BUSY_POLL", 0)
    monkeypatch.setattr(backfill, "_wake", None)
    backfill.BAD.clear()


def make(env, store, ss, **extra):
    st = config.load({**env, "PERIOD_BACKFILL": "robsmsf", **extra})
    return Ctx(st, store, ScriptedBackend([]), registry(ss), FakeBot()), st


def test_only_finished_canonical_windows_are_wanted_months_first():
    w = backfill.wanted(date(2026, 1, 15), date(2026, 3, 3))  # yesterday = 2026-03-02
    labels = [x[0] for x in w]
    assert labels[:2] == ["2026-01", "2026-02"] and "2026-03" not in labels  # March isn't over
    assert labels.index("2026-W03") < labels.index("2026-01-15")  # weeks before days
    assert [x[0] for x in w if x[0].startswith("2026-W")][-1] == "2026-W09"  # week ending Sun 1 Mar; not the next
    assert w[0][1] == date(2026, 1, 15)  # the first window starts at inception
    assert not any(x[0] == "2026-Q1" or x[0] == "2026" for x in w)  # quarter and year not finished
    assert backfill.wanted(date(2026, 3, 5), date(2026, 3, 3)) == []


def test_portfolios_are_selected_by_name_or_all(env):
    listing = json.dumps({"portfolios": [{"id": 7, "name": "RobSMSF", "inception_date": "2026-01-15"},
                                         {"id": 8, "name": "Other", "inception_date": "2026-01-15"},
                                         {"id": 9, "name": "NoDate"}]})
    pick = lambda **e: [p[1] for p in backfill.selected(config.load({**env, "TELEGRAM_BOT_TOKEN": "1:x", **e}), listing)]  # noqa: E731
    assert pick(PERIOD_BACKFILL="robsmsf") == ["RobSMSF"]
    assert pick(PERIOD_BACKFILL="all") == ["RobSMSF", "Other"]  # a portfolio without an inception date is skipped
    assert pick() == ["RobSMSF", "Other"]  # on by default
    assert pick(PERIOD_BACKFILL="off") == []


async def test_a_pass_fetches_what_is_missing_then_only_what_is_new(env, store):
    ss = Sharesight("sharesight", tools=("list_portfolios", "get_performance_report"))
    ctx, st = make(env, store, ss)
    portfolios = backfill.selected(st, await ss.call("list_portfolios", {}))
    want = backfill.wanted(date(2026, 1, 15), date(2026, 3, 3))
    assert await backfill.fill_once(ctx, ss, portfolios, date(2026, 3, 3)) == len(want)
    assert ss.order[0] == (want[0][1].isoformat(), want[0][2].isoformat())  # the first month, from inception
    assert store.period_count(7, "2026-01-01", "2026-03-31") == len(want) and store.period_count(8, "2000-01-01", "2100-01-01") == 0
    ss.order.clear()
    assert await backfill.fill_once(ctx, ss, portfolios, date(2026, 3, 3)) == 0 and ss.order == []
    assert await backfill.fill_once(ctx, ss, portfolios, date(2026, 3, 4)) == 1  # yesterday, Tue 3 Mar, newly finished
    assert ss.order == [("2026-03-03", "2026-03-03")]


async def test_every_portfolios_cheap_periods_come_before_anyones_days(env, store):
    ss = Sharesight("sharesight", tools=("list_portfolios", "get_performance_report"))
    ctx, _ = make(env, store, ss)
    seen = []
    real = ss.call

    async def spy(tool, args):
        if tool == "get_performance_report":
            seen.append((args["portfolio_id"], args["start_date"], args["end_date"]))
        return await real(tool, args)

    ss.call = spy
    folios = [(7, "RobSMSF", date(2026, 1, 15)), (8, "Other", date(2026, 1, 15))]
    today = date(2026, 3, 3)
    total = await backfill.fill_once(ctx, ss, folios, today)
    assert total == 2 * len(backfill.wanted(date(2026, 1, 15), today)) and len(set(seen)) == len(seen)  # once each
    is_day = lambda x: x[1] == x[2]  # noqa: E731
    last_cheap = max(i for i, x in enumerate(seen) if not is_day(x))
    first_day = min(i for i, x in enumerate(seen) if is_day(x))
    assert last_cheap < first_day  # all weeks/months of both portfolios, then days
    assert {x[0] for x in seen[:last_cheap + 1]} == {7, 8}
    assert await backfill.fill_once(ctx, ss, folios, today) == 0


async def test_it_waits_while_a_user_request_is_using_the_server(env, store):
    ss = Sharesight("sharesight", tools=("list_portfolios", "get_performance_report"))
    ss.busy_for = 3
    ctx, st = make(env, store, ss)
    await backfill.fill_once(ctx, ss, [(7, "RobSMSF", date(2026, 3, 1))], date(2026, 3, 3))
    assert ss.polls > 3 and ss.order  # polled until free, then fetched


async def test_two_fetches_run_at_a_time_and_each_window_is_fetched_once(env, store):
    ss = Sharesight("sharesight", tools=("list_portfolios", "get_performance_report"))
    ss.delay = 0.01
    ctx, _ = make(env, store, ss)
    want = backfill.wanted(date(2026, 1, 15), date(2026, 3, 3))
    assert await backfill.fill_once(ctx, ss, [(7, "RobSMSF", date(2026, 1, 15))], date(2026, 3, 3)) == len(want)
    assert ss.peak == backfill.WORKERS == 2
    assert len(ss.order) == len(set(ss.order)) == len(want)  # none twice
    assert store.period_count(7, "2026-01-01", "2026-03-31") == len(want)


async def test_failures_end_the_pass_and_the_same_windows_are_tried_again_next_time(env, store):
    ss = Sharesight("sharesight", tools=("list_portfolios", "get_performance_report"), fail=True)
    ctx, _ = make(env, store, ss)
    args = ctx, ss, [(7, "RobSMSF", date(2026, 1, 15))], date(2026, 3, 3)
    assert await backfill.fill_once(*args) == 0
    assert backfill.MAX_FAILS <= len(ss.order) <= backfill.MAX_FAILS + backfill.WORKERS - 1  # gave up after five
    assert not backfill.BAD  # a blip doesn't mark the windows
    first_pass = list(ss.order)
    ss.order.clear()
    await backfill.fill_once(*args)
    assert ss.order[:backfill.MAX_FAILS] == first_pass[:backfill.MAX_FAILS]  # same windows again
    backfill.reset()
    assert not backfill.BAD and backfill.wake_event().is_set()


async def test_a_stopped_pass_is_retried_after_ten_minutes_not_six_hours(env, store, monkeypatch):
    ss = Sharesight("sharesight", tools=("list_portfolios", "get_performance_report"), fail=True)
    ctx, _ = make(env, store, ss)
    monkeypatch.setattr(backfill, "RECHECK", 3600)
    monkeypatch.setattr(backfill, "RETRY_AFTER_FAILURE", 0.05)
    task = asyncio.create_task(backfill.loop(ctx))
    for _ in range(200):
        await asyncio.sleep(0.02)
        if len(ss.order) >= 3 * backfill.MAX_FAILS:  # several passes, each stopped by failures
            break
    task.cancel()
    assert len(ss.order) >= 3 * backfill.MAX_FAILS


async def test_windows_with_no_data_do_not_stop_the_pass_and_are_not_retried(env, store, caplog):
    caplog.set_level("INFO", logger="bot")
    ss = Sharesight("sharesight", tools=("list_portfolios", "get_performance_report"))
    ss.data_from = "2026-02-15"  # everything ending before this has no data: dozens of empty windows in a row
    ctx, _ = make(env, store, ss)
    args = ctx, ss, [(7, "RobSMSF", date(2026, 1, 15))], date(2026, 3, 3)
    stored = await backfill.fill_once(*args)
    assert stored > 0 and store.period_count(7, "2026-01-01", "2026-03-31") == stored  # later windows still got stored
    assert "had no data" in caplog.text and "fetches in a row failed" not in caplog.text
    shown = [r.message for r in caplog.records if "has no data:" in r.message]
    assert 0 < len(shown) <= 3 * 5 and "no data for this period" in shown[0]  # a few samples per step, with Sharesight's words
    ss.order.clear()
    assert await backfill.fill_once(*args) == 0 and ss.order == []  # the empty ones are skipped until reset()


async def test_throttling_text_counts_as_a_transient_failure_and_is_logged(env, store, caplog):
    ss = Sharesight("sharesight", tools=("list_portfolios", "get_performance_report"), fail=True)
    ss.fail_text = "Tool error: HTTP 429 Too Many Requests"
    ctx, _ = make(env, store, ss)
    assert await backfill.fill_once(ctx, ss, [(7, "RobSMSF", date(2026, 1, 15))], date(2026, 3, 3)) == 0
    assert "fetches in a row failed" in caplog.text and "429 Too Many Requests" in caplog.text


async def test_the_journal_says_when_it_is_held_back_or_a_fetch_is_slow(env, store, monkeypatch, caplog):
    caplog.set_level("INFO", logger="bot")
    monkeypatch.setattr(backfill, "WAIT_LOG", 0)
    monkeypatch.setattr(backfill, "SLOW_FETCH", 0.005)
    ss = Sharesight("sharesight", tools=("list_portfolios", "get_performance_report"))
    ss.busy_for = 3
    ss.delay = 0.01
    ctx, _ = make(env, store, ss)
    await backfill.fill_once(ctx, ss, [(7, "RobSMSF", date(2026, 3, 1))], date(2026, 3, 3))
    assert "held back for 0 s by: get_performance_report portfolio_id=7" in caplog.text and "was slow" in caplog.text


async def test_progress_is_logged_every_n_fetches(env, store, monkeypatch, caplog):
    caplog.set_level("INFO", logger="bot")
    monkeypatch.setattr(backfill, "PROGRESS_EVERY", 5)
    ss = Sharesight("sharesight", tools=("list_portfolios", "get_performance_report"))
    ctx, _ = make(env, store, ss)
    await backfill.fill_once(ctx, ss, [(7, "RobSMSF", date(2026, 1, 15))], date(2026, 3, 3))
    assert "RobSMSF days 5/" in caplog.text and "RobSMSF days 10/" in caplog.text


async def test_the_loop_runs_a_pass_then_sleeps_until_woken(env, store, monkeypatch):
    recent = (date.today() - timedelta(days=4)).isoformat()
    ss = Sharesight("sharesight", tools=("list_portfolios", "get_performance_report"), inception=recent)
    ctx, _ = make(env, store, ss)
    monkeypatch.setattr(backfill, "RECHECK", 3600)
    task = asyncio.create_task(backfill.loop(ctx))
    expected = len(backfill.wanted(date.today() - timedelta(days=4), date.today()))
    for _ in range(500):  # the store runs in a thread, so give it real time
        await asyncio.sleep(0.01)
        if store.period_count(7, recent, "2100-01-01") == expected:
            break
    assert expected and store.period_count(7, recent, "2100-01-01") == expected
    assert store.period_count(8, "2000-01-01", "2100-01-01") == 0
    n = len(ss.order)
    backfill.reset()  # as after a flush: runs again straight away (nothing new, so nothing fetched)
    for _ in range(50):
        await asyncio.sleep(0.01)
        if not backfill.wake_event().is_set():
            break
    await asyncio.sleep(0.05)
    assert len(ss.order) == n and not backfill.wake_event().is_set()
    task.cancel()


def test_keys_match_the_tool_windows():
    w = period_windows("month", date(2026, 1, 15), date(2026, 3, 2))[0]
    assert key(w) == ("2026-01-15", "2026-01-31")
