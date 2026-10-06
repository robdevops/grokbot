"""Token budgets: these fail if a change makes the prompts fatter."""

import json

from lib.history import compact, format_rows
from lib.llm.gate import Route
from lib.mcp.schema import compact_description, compact_schema
from lib.prompts import chat_prompt, system_prompt
from tools import report


def test_system_prompt_stays_small():
    full = report.tokens(system_prompt("Stock", Route([], True), "the web"))
    assert full < 560, full  # was ~860 before the recode
    lean = report.tokens(system_prompt("Stock", Route([], False, simple=True), "the web"))
    assert lean < 540


def test_history_encoding_trims_link_heavy_bot_lines():
    from zoneinfo import ZoneInfo
    rows = report.sample_rows()
    tz = ZoneInfo("UTC")
    raw = format_rows(rows, "Stock (@stockbot)", tz, line_max=400, compact_text=False)
    slim = format_rows(rows, "Stock (@stockbot)", tz, line_max=240, compact_text=True, own_line_max=160)
    assert report.tokens(slim) < report.tokens(raw) * 0.8  # the latest bot reply is kept whole
    assert "finance.yahoo.com" not in slim and "<b>" not in slim and "NVDA" in slim


def test_tool_definition_diet():
    tool = report.SAMPLE_TOOL
    before = json.dumps({"name": tool["name"], "description": tool["description"], "parameters": tool["schema"]})
    after = json.dumps({"name": tool["name"], "description": compact_description(tool["description"]),
                        "parameters": compact_schema(tool["schema"])})
    assert report.tokens(after) < report.tokens(before) * 0.55
    assert '"default"' not in after and '"title"' not in after and "Returns" not in after


def test_volatile_text_comes_last_so_the_prefix_can_be_cached():
    base = dict(transcript="[#1] hi", sender="Alex", private=False, reply_quote=None, msg_id=2)
    a = chat_prompt(now="10:00", down="", **base)
    b = chat_prompt(now="10:01", down="\n\nThese data sources are DOWN right now", **base)
    prefix = a.split("It's now")[0]
    assert b.startswith(prefix) and a.endswith("message (#2).") and "DOWN" in b.split("It's now")[1]


def test_system_prompt_is_identical_for_everyone_with_the_same_route():
    assert system_prompt("Stock", Route([], True), "the web") == system_prompt("Stock", Route([], True), "the web")


def test_system_prompt_explains_cut_lines():
    assert "…[cut]" in system_prompt("Stock", Route([], True), "the web")


def test_compact_text_helper():
    assert compact('<a href="https://finance.yahoo.com/quote/PME.AX"><b>PME</b></a>') == "PME"
