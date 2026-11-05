import json
import time

import httpx

from lib import config, tickers
from lib.context import Ctx
from lib.features import known_tickers as kt

from .conftest import FakeBot
from .fakes import FakeMcp, ScriptedBackend, registry


def page(rows, table_id="constituents", header="Symbol"):
    body = "".join(f"<tr><td><a href='/wiki/{s}'>{s}</a></td><td>Co {s}</td></tr>" for s in rows)
    return (f"<table class='infobox'><tr><th>Founded</th></tr></table><table id='{table_id}'>"
            f"<tr><th>{header}</th><th>Security</th></tr>{body}</table>")


def test_the_ticker_column_is_read_and_share_classes_use_a_hyphen():
    assert kt.symbols_from_html(page(["NVDA", "BRK.B", "bad one", "GOOGL"])) == ["NVDA", "BRK-B", "GOOGL"]
    assert kt.symbols_from_html(page(["AAPL"], table_id="", header="Ticker")) == ["AAPL"]  # no id: by its header
    assert kt.symbols_from_html("<table><tr><th>Name</th></tr><tr><td>x</td></tr></table>") == []
    footnote = "<table id='constituents'><tr><th>Ticker<sup>[12]</sup></th></tr><tr><td>MSFT</td></tr></table>"
    assert kt.symbols_from_html(footnote) == ["MSFT"]


class Sharesight(FakeMcp):
    async def call(self, tool, args):
        if tool == "list_portfolios":
            return json.dumps({"portfolios": [{"name": "Bob", "id": 7}]})
        rows = [["GM", "NYSE", "General Motors"], ["NXT", "ASX", "NextDC"], ["ZZZ", "NASDAQ", "Zed"]]
        return json.dumps({"report": {"holdings": {"columns": ["code", "market", "name"], "rows": rows}}})


def client(pages: dict[str, str]) -> httpx.AsyncClient:
    def reply(request: httpx.Request) -> httpx.Response:
        text = pages.get(str(request.url))
        return httpx.Response(200, text=text) if text is not None else httpx.Response(503)
    return httpx.AsyncClient(transport=httpx.MockTransport(reply))


async def test_refresh_merges_the_indexes_and_the_us_holdings_and_keeps_a_list_that_fails(env, store, monkeypatch):
    (sp, sp_url, _, _), (ndx, ndx_url, _, _) = config.INDEX_LISTS
    monkeypatch.setattr(config, "INDEX_LISTS", ((sp, sp_url, 2, 5), (ndx, ndx_url, 2, 5)))
    st = config.load({**env, "PORTFOLIO_NAMES": "Bob"})
    ctx = Ctx(st, store, ScriptedBackend([]), registry(Sharesight("sharesight", gate="portfolio")), FakeBot())
    before = dict(tickers.KNOWN)
    try:
        saved = await kt.refresh(ctx, {}, client({sp_url: page(["NVDA", "BRK.B"]), ndx_url: page(["NVDA", "AMD"])}))
        assert saved["indexes"] == {sp: ["NVDA", "BRK-B"], ndx: ["NVDA", "AMD"]} and saved["held"] == ["GM", "ZZZ"]
        assert set(tickers.KNOWN) == {"NVDA", "BRK-B", "BRK.B", "AMD", "GM", "ZZZ"}  # NXT is ASX: not a bare US code
        assert tickers.KNOWN["BRK.B"] == "BRK-B" and saved["ts"] > time.time() - 5  # all parts worked: due tomorrow
        # the next day the S&P page is down and the NASDAQ page looks wrong: both keep yesterday's list
        again = await kt.refresh(ctx, saved, client({ndx_url: page(["ONLY"])}))
        assert again["indexes"] == saved["indexes"]
        due = again["ts"] + config.KNOWN_TICKERS_REFRESH - time.time()
        assert due <= config.KNOWN_TICKERS_RETRY + 5  # a part failed: tried again in half an hour, not a day
        tickers.set_known({})
        assert kt.load(ctx) == again and "BRK-B" in tickers.KNOWN  # a restart has them from the kv table at once
    finally:
        tickers.set_known(before)


async def test_without_sharesight_the_held_list_stays_as_it_was(env, store):
    ctx = Ctx(config.load(env), store, ScriptedBackend([]), registry(), FakeBot())
    assert await kt.us_holdings(ctx) is None


async def test_the_loop_waits_for_the_mcp_when_overdue_and_otherwise_until_due(env, store, monkeypatch):
    import asyncio
    import time
    ctx = Ctx(config.load(env), store, ScriptedBackend([]), registry(), FakeBot())
    waits, refreshed = [], []

    async def sleep(seconds):
        waits.append(seconds)
        if len(waits) > 1:
            raise asyncio.CancelledError

    async def refresh(ctx, saved, client):
        refreshed.append(saved)
        return {**saved, "ts": int(time.time())}

    monkeypatch.setattr(kt.asyncio, "sleep", sleep)
    monkeypatch.setattr(kt, "refresh", refresh)
    before = dict(tickers.KNOWN)
    try:
        try:
            await kt.loop(ctx)
        except asyncio.CancelledError:
            pass
    finally:
        tickers.set_known(before)
    assert waits[0] == config.KNOWN_TICKERS_START_DELAY and refreshed == [{}]  # nothing saved yet: refresh soon
    assert config.KNOWN_TICKERS_REFRESH - 5 < waits[1] <= config.KNOWN_TICKERS_REFRESH  # then a day later
