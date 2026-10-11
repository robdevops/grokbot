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
        self.inception = inception
        self.busy_for = 0  # polls during which the server counts as busy
        self.polls = 0
        self.order: list[tuple[str, str]] = []

    @property
    def busy(self):
        self.polls += 1
        return self.polls <= self.busy_for

    async def call(self, tool, args):
        self.calls.append((tool, args))
        if tool == "list_portfolios":
            return json.dumps({"portfolios": [{"id": 7, "name": "RobSMSF", "inception_date": self.inception},
                                              {"id": 8, "name": "Other", "inception_date": "2026-01-15"}]})
        self.order.append((args["start_date"], args["end_date"]))
        if self.fail:
            return "Error: timed out"
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
    assert pick() == []


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


async def test_it_waits_while_a_user_request_is_using_the_server(env, store):
    ss = Sharesight("sharesight", tools=("list_portfolios", "get_performance_report"))
    ss.busy_for = 3
    ctx, st = make(env, store, ss)
    await backfill.fill_once(ctx, ss, [(7, "RobSMSF", date(2026, 3, 1))], date(2026, 3, 3))
    assert ss.polls > 3 and ss.order  # polled until free, then fetched


async def test_failures_end_the_pass_and_failed_windows_are_not_retried_until_reset(env, store):
    ss = Sharesight("sharesight", tools=("list_portfolios", "get_performance_report"), fail=True)
    ctx, _ = make(env, store, ss)
    args = ctx, ss, [(7, "RobSMSF", date(2026, 1, 15))], date(2026, 3, 3)
    assert await backfill.fill_once(*args) == 0 and len(ss.order) == backfill.MAX_FAILS  # gave up after five
    first_five = list(ss.order)
    ss.order.clear()
    await backfill.fill_once(*args)
    assert len(ss.order) == backfill.MAX_FAILS and not set(ss.order) & set(first_five)  # skipped those five
    backfill.reset()
    assert not backfill.BAD and backfill.wake_event().is_set()


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
