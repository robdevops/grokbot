"""Real MCP output, captured with tools/capture.py. Sharesight data is anonymised (two small made-up portfolios
built from one real report's shape); Yahoo data is public. Checks the bot's code against real shapes."""

import json
from pathlib import Path

import pytest

from lib.features import holding_news
from lib.mcp.results import slim_result, table_records
from lib.mcp.schema import compact_description, compact_schema

from .fakes import FakeMcp

FIX = Path(__file__).parent / "fixtures"
REPORTS = ("alpha", "beta")


def sharesight(name: str) -> dict:
    return json.loads((FIX / "sharesight" / name).read_text())


def test_sharesight_fixtures_are_two_distinct_small_portfolios():
    ports = sharesight("list_portfolios.json")["portfolios"]
    assert [p["name"] for p in ports] == ["Alpha SMSF", "Beta SMSF"] and len({p["id"] for p in ports}) == 2
    reports = {k: sharesight(f"performance_report_{k}.json")["report"] for k in REPORTS}
    assert [reports[k]["portfolio_id"] for k in REPORTS] == [p["id"] for p in ports]
    codes = {k: {h["instrument"]["code"] for h in r["holdings"]} for k, r in reports.items()}
    assert codes["alpha"] != codes["beta"]
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
    assert len(slim) < len(text) / 5
    rows = table_records(report["holdings"])
    assert [(r["code"], r["quantity"], r["value"]) for r in rows] == [
        (h["instrument"]["code"], h["quantity"], h["value"]) for h in original["holdings"]]
    assert report["value"] == original["value"] and report["total_gain"] == original["total_gain"]
    assert "logo" not in slim and "light_url" not in slim  # the bulky per-instrument extras are gone


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
    holdings = await holding_news.current_holdings(
        SharesightFixtures("sharesight", tools=("list_portfolios",)), ["alpha smsf", "beta smsf"])
    assert holdings["GNP (ASX)"] == "Genusplus Group" and "NVDA (NASDAQ)" in holdings
    only_beta = await holding_news.current_holdings(SharesightFixtures("sharesight"), ["beta smsf"])
    assert set(only_beta) < set(holdings) and "GNP (ASX)" not in only_beta
    assert await holding_news.current_holdings(SharesightFixtures("sharesight"), ["no such portfolio"]) == {}


@pytest.mark.parametrize("server", ["sharesight", "yahoo"])
def test_real_tool_definitions_survive_the_schema_diet(server):
    tools = json.loads((FIX / server / "tools.json").read_text())
    assert tools
    for t in tools:
        slim = compact_schema(t["inputSchema"])
        assert slim.get("required") == t["inputSchema"].get("required")
        assert set(slim["properties"]) <= set(t["inputSchema"]["properties"])
        assert 0 < len(compact_description(t["description"])) <= len(t["description"])


def test_real_yahoo_results_are_markdown_so_slimming_leaves_them_alone():
    files = sorted((FIX / "yahoo").glob("*.txt"))
    assert len(files) >= 8
    for f in files:
        text = f.read_text().rstrip("\n")
        assert slim_result(text, "yahoo") == text  # not JSON, so passed through unchanged


def test_real_yahoo_error_and_empty_news_results():
    error = (FIX / "yahoo" / "get_stock_quote__ZZZZNOTREAL.txt").read_text()
    assert error.startswith("Tool error: ") and "validation error" in error
    assert "No recent news" in (FIX / "yahoo" / "get_market_news__NVDA.txt").read_text()
