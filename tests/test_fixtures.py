"""Real MCP output, captured with tools/capture.py. Sharesight data is anonymised (two small made-up portfolios
built from one real report's shape); Yahoo data is public. Checks the bot's code against real shapes."""

import json
from pathlib import Path

import pytest

from lib import config
from lib import holdings as sharesight_holdings
from lib.features import charts
from lib.features.charts import future
from lib.mcp.results import slim_result, table_records
from lib.mcp.schema import compact_description, compact_schema

from .fakes import FakeMcp

FIX = Path(__file__).parent / "fixtures"
REPORTS = ("bob", "alice")


def sharesight(name: str) -> dict:
    return json.loads((FIX / "sharesight" / name).read_text())


def test_sharesight_fixtures_are_two_distinct_small_portfolios():
    ports = sharesight("list_portfolios.json")["portfolios"]
    assert [p["name"] for p in ports] == ["BobSMSF", "AliceSMSF"] and len({p["id"] for p in ports}) == 2
    reports = {k: sharesight(f"performance_report_{k}.json")["report"] for k in REPORTS}
    assert [reports[k]["portfolio_id"] for k in REPORTS] == [p["id"] for p in ports]
    codes = {k: {h["instrument"]["code"] for h in r["holdings"]} for k, r in reports.items()}
    assert codes["bob"] != codes["alice"]
    ids = [h["id"] for r in reports.values() for h in r["holdings"]]
    assert len(ids) == len(set(ids))
    for r in reports.values():
        assert 4_000 < r["value"] < 5_500
        held, cash = sum(h["value"] for h in r["holdings"]), sum(c["value"] for c in r["cash_accounts"])
        assert r["value"] == pytest.approx(held + cash, abs=0.05)
        assert sum(s["value"] for s in r["sub_totals"]) == pytest.approx(held, abs=0.05)


@pytest.mark.parametrize("key", REPORTS)
def test_real_performance_report_is_flattened_to_a_fraction_of_its_size(key):
    text = (FIX / "sharesight" / f"performance_report_{key}.json").read_text()
    slim = slim_result(text, "sharesight", drop_closed=True)
    report, original = json.loads(slim)["report"], json.loads(text)["report"]
    assert len(slim) < len(text) / 8
    rows = table_records(report["holdings"])
    assert [(r["code"], r["quantity"], r["value"]) for r in rows] == [
        (h["instrument"]["code"], h["quantity"], h["value"]) for h in original["holdings"]]
    assert report["value"] == original["value"] and report["total_gain"] == original["total_gain"]
    assert "logo" not in slim and "light_url" not in slim  # the bulky per-instrument extras are gone
    holdings = json.loads(slim)["report"]["holdings"]
    assert "id" not in holdings["columns"] and "group_name" not in holdings["columns"]  # internal ID; market again
    assert {r["type"] for r in rows if r.get("type")} == {"ETF", "ADR"}  # shortened; plain shares say nothing
    assert all("group_id" not in s for s in json.loads(slim)["report"]["sub_totals"])


class SharesightFixtures(FakeMcp):
    """Answers list_portfolios and get_performance_report as the real server does, slimmed as the bot does."""

    async def call(self, tool, args):
        if tool == "list_portfolios":
            name = "list_portfolios.json"
        else:
            name = {p["id"]: f"performance_report_{k}.json" for p, k in zip(
                sharesight("list_portfolios.json")["portfolios"], REPORTS, strict=True)}[args["portfolio_id"]]
        return slim_result((FIX / "sharesight" / name).read_text(), "sharesight", drop_closed=True)


async def test_holding_news_reads_current_holdings_from_real_shaped_reports():
    holdings = await sharesight_holdings.current_holdings(
        SharesightFixtures("sharesight", tools=("list_portfolios",)), ["bobsmsf", "alicesmsf"])
    assert holdings["GNP (ASX)"] == "Genusplus Group" and "NVDA (NASDAQ)" in holdings
    only_alice = await sharesight_holdings.current_holdings(SharesightFixtures("sharesight"), ["alicesmsf"])
    assert set(only_alice) < set(holdings) and "GNP (ASX)" not in only_alice
    assert await sharesight_holdings.current_holdings(SharesightFixtures("sharesight"), ["no such portfolio"]) == {}
    every = await sharesight_holdings.current_holdings(SharesightFixtures("sharesight"), None)  # no names: all of them
    assert set(holdings) <= set(every)


def test_sharesight_tool_definitions_hide_the_parameters_the_model_never_needs():
    cfg = json.loads((FIX.parent.parent / "mcp_servers.json").read_text())["mcpServers"]["sharesight"]
    tools = {t["name"]: t for t in json.loads((FIX / "sharesight" / "tools.json").read_text())}
    hide = frozenset(cfg["hide_params"])
    report = compact_schema(tools["get_performance_report"]["inputSchema"], hide=hide)
    assert set(report["properties"]) == {"portfolio_id", "start_date", "end_date", "grouping", "benchmark_code"}
    assert report["required"] == ["portfolio_id"]
    assert compact_schema(tools["list_portfolios"]["inputSchema"], hide=hide)["properties"] == {}
    sizes = [len(json.dumps(compact_schema(t["inputSchema"], hide=hide))) for t in tools.values()]
    assert sum(sizes) < 700  # was ~1,300 characters


@pytest.mark.parametrize("server", ["sharesight", "yahoo"])
def test_real_tool_definitions_survive_the_schema_diet(server):
    tools = json.loads((FIX / server / "tools.json").read_text())
    assert tools
    for t in tools:
        slim = compact_schema(t["inputSchema"])
        assert slim.get("required") == t["inputSchema"].get("required")
        assert set(slim["properties"]) <= set(t["inputSchema"]["properties"])
        assert 0 < len(compact_description(t["description"])) <= len(t["description"])


def yahoo(name: str) -> str:
    return (FIX / "yahoo" / name).read_text().rstrip("\n")


def test_yahoo_tool_definitions_hide_module_options_and_allow_only_what_the_bot_needs():
    cfg = json.loads((FIX.parent.parent / "mcp_servers.json").read_text())["mcpServers"]["yahoo"]
    tools = {t["name"]: t for t in json.loads(yahoo("tools.json"))}
    # Names like quote and chart don't look read-only, so they are listed (which also leaves the rest out).
    assert set(cfg["allowed_tools"]) <= set(tools)  # names the server really has
    assert {charts.HISTORY_TOOL, future.ESTIMATES} <= set(cfg["allowed_tools"])
    hide = frozenset(cfg["hide_params"])
    assert set(compact_schema(tools["chart"]["inputSchema"], hide=hide)["properties"]) == {"symbol", "queryOptions"}


def test_real_yahoo_json_results_are_slimmed():
    for name in ("chart__NXT-AX.txt", "quote__CBA-AX.txt", "quoteSummary__AMD.txt"):
        text = yahoo(name)
        assert len(slim_result(text, "yahoo")) < len(text) < config.MAX_TOOL_OUTPUT


def test_ten_real_quotes_fit_one_result_with_what_the_model_needs():
    one = json.loads(yahoo("quote__CBA-AX.txt"))["result"]
    many = json.dumps({"result": [{**one, "symbol": f"S{i}"} for i in range(10)]})
    assert len(many) > config.MAX_TOOL_OUTPUT * 2  # as sent, ten quotes would be cut after three
    slim = slim_result(many, "yahoo")
    assert len(slim) < config.MAX_TOOL_OUTPUT * 0.8
    first = json.loads(slim)["result"][0]
    assert {"regularMarketPrice", "trailingPE", "marketCap", "currency"} <= set(first)
    assert "messageBoardId" not in first and "corporateActions" not in first  # empty ones go too
    assert json.loads(slim_result(yahoo("quoteSummary__AMD.txt"), "yahoo"))["result"]["earningsTrend"]  # left alone


def test_a_real_chart_result_gives_a_price_series_in_sydney_time():
    x, y = charts.closes(json.loads(yahoo("chart__NXT-AX.txt")), "1y")
    assert len(x) == len(y) == 7 and x[0] == "2026-09-28T00:00:00+10:00" and y[-1] == pytest.approx(10.4)
    assert charts.as_date(x[0]).date().isoformat() == "2026-09-28"


@pytest.mark.parametrize("name, years, revenue", [
    ("quoteSummary__AMD.txt", ["FY2025", "FY2026", "FY2027"], (34_639_000_000, 61_257_000_000, "USD")),
    ("quoteSummary__NXT-AX.txt", ["FY2026", "FY2027", "FY2028"], (496_500_000, 889_400_000, "AUD"))])
def test_real_earnings_trends_give_both_years_estimates(name, years, revenue):
    payload = json.loads(slim_result(yahoo(name), "yahoo"))  # as the bot gets it
    assert future.fiscal_years(payload) == years
    actual, (this, nxt) = future.metric(future.rows(payload, "revenueEstimate"), "yearAgoRevenue")
    assert (actual, nxt["low"], this["currency"]) == revenue
    assert future.metric(future.rows(payload, "earningsEstimate"), "yearAgoEps") is not None


def test_a_real_yahoo_error_for_a_symbol_that_doesnt_exist():
    assert yahoo("chart__ZZZZNOTREAL.txt").startswith("Tool error: ")
