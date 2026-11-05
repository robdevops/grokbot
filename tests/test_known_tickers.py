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
    assert kt.symbols_from_html(page(["BHP", "A2M", "bad"], header="Code"), "AX") == ["BHP", "A2M"]  # ASX codes
    assert kt.symbols_from_html(page(["NVDA", "BRK.B", "bad one", "GOOGL"])) == ["NVDA", "BRK-B", "GOOGL"]
    assert kt.symbols_from_html(page(["AAPL"], table_id="", header="Ticker")) == ["AAPL"]  # no id: by its header
    assert kt.symbols_from_html("<table><tr><th>Name</th></tr><tr><td>x</td></tr></table>") == []
    footnote = "<table id='constituents'><tr><th>Ticker<sup>[12]</sup></th></tr><tr><td>MSFT</td></tr></table>"
    assert kt.symbols_from_html(footnote) == ["MSFT"]


class Sharesight(FakeMcp):
    async def call(self, tool, args):
        if tool == "list_portfolios":
            return json.dumps({"portfolios": [{"name": "Bob", "id": 7}]})
        rows = [["GM", "NYSE", "General Motors"], ["NXT", "ASX", "NextDC"], ["QBE", "ASX", "QBE Insurance"],
                ["ZZZ", "MOON", "Unknown exchange"]]
        return json.dumps({"report": {"holdings": {"columns": ["code", "market", "name"], "rows": rows}}})


def wikipedia(pages: dict[str, str], seen: list | None = None) -> httpx.AsyncClient:
    """Wikipedia's action API: the page HTML inside {"parse": {"text": ...}}; a page not given answers 403."""
    def reply(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        title = request.url.params.get("page")
        if title not in pages:
            return httpx.Response(403, text="Please set a user-agent and respect our robot policy")
        return httpx.Response(200, json={"parse": {"title": title, "text": pages[title]}})
    return httpx.AsyncClient(transport=httpx.MockTransport(reply))


def small_lists(monkeypatch):
    lists = tuple((name, title, suffix, 2, 5) for name, title, suffix, _, _ in config.INDEX_LISTS)
    monkeypatch.setattr(config, "INDEX_LISTS", lists)
    return [title for _, title, _, _, _ in lists]


async def test_refresh_reads_the_api_with_a_contact_and_links_asx_codes_to_ax(env, store, monkeypatch, caplog):
    sp, ndx, asx = small_lists(monkeypatch)
    st = config.load({**env, "PORTFOLIO_NAMES": "Bob"})
    ctx = Ctx(st, store, ScriptedBackend([]), registry(Sharesight("sharesight", gate="portfolio")), FakeBot())
    pages = {sp: page(["NVDA", "BRK.B", "QBE", "ALL"]), ndx: page(["NVDA", "AMD"]),
             asx: page(["BHP", "A2M", "ALL", "QBE"], header="Code")}
    seen: list = []
    before = dict(tickers.KNOWN)
    try:
        saved = await kt.refresh(ctx, {}, wikipedia(pages, seen))
        assert seen[0].url.params["action"] == "parse" and "https://" in seen[0].headers["user-agent"]
        assert saved["indexes"]["ASX 200"] == ["BHP", "A2M", "ALL", "QBE"]
        assert saved["held"] == {"GM": "GM", "NXT": "NXT.AX", "QBE": "QBE.AX"}  # MOON is no exchange we know
        k = tickers.KNOWN
        assert k["BHP"] == "BHP.AX" and k["A2M"] == "A2M.AX" and k["NVDA"] == "NVDA" and k["BRK.B"] == "BRK-B"
        assert "ALL" not in k and k["QBE"] == "QBE.AX"  # on both exchanges: left out, unless a holding settles it
        assert saved["ts"] > time.time() - 5  # every part worked: due tomorrow
        # the next day the S&P page refuses and the NASDAQ page looks wrong: both keep yesterday's list
        again = await kt.refresh(ctx, saved, wikipedia({ndx: page(["ONLY"]), asx: pages[asx]}))
        assert again["indexes"] == saved["indexes"]
        assert "HTTP 403: Please set a user-agent" in caplog.text  # why it was refused
        assert again["ts"] + config.KNOWN_TICKERS_REFRESH - time.time() <= config.KNOWN_TICKERS_RETRY + 5
        tickers.set_known({})
        assert kt.load(ctx) == again and tickers.KNOWN["BHP"] == "BHP.AX"  # a restart has them from the kv table
    finally:
        tickers.set_known(before)


def test_lists_saved_by_the_earlier_version_still_load():
    codes, ambiguous = kt.known({"indexes": {"S&P 500": ["NVDA"]}, "held": ["GM", "BRK.B"]})
    assert codes == {"NVDA": "NVDA", "GM": "GM", "BRK-B": "BRK-B", "BRK.B": "BRK-B"} and ambiguous == set()


async def test_without_sharesight_the_held_list_stays_as_it_was(env, store):
    ctx = Ctx(config.load(env), store, ScriptedBackend([]), registry(), FakeBot())
    assert await kt.held_symbols(ctx) is None


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


def test_the_asx_200_page_layout_reads_number_codes_too():
    html = ('<p>As of 5 April 2026, the constituent stocks of the ASX 200</p><table class="wikitable sortable"><tbody>'
            '<tr><th>Code</th><th>Company</th><th>Sector</th></tr>'
            '<tr><td>360</td><td><a href="/wiki/Life360">Life360</a></td><td>Information Technology</td></tr>'
            '<tr><td>4DX</td><td><a href="/wiki/4DMedical">4DMedical</a></td><td>Healthcare</td></tr>'
            '<tr><td>A2M</td><td><a>a2 Milk Company</a></td><td>Consumer Staples</td></tr></tbody></table>')
    codes = kt.symbols_from_html(html, "AX")
    assert codes == ["360", "4DX", "A2M"]
    out = tickers.link_tickers("Life360 (360) and 4DX up; 360 degrees", {c: f"{c}.AX" for c in codes})
    assert 'quote/360.AX">360</a>)</b> and <a href' in out and out.endswith("; 360 degrees")  # a bare number never links
