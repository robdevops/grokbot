import asyncio
import json
from types import SimpleNamespace as N

from tgbot.mcp.results import diet, slim_result, table_records, tidy_sharesight
from tgbot.mcp.schema import ToolDef, compact_description, compact_schema, looks_read_only
from tgbot.mcp.server import MCPServer, Registry


def test_compact_description_drops_return_docs():
    text = "Get a quote.\n\nReturns: lots of JSON\n\nUse for prices.\n\nExample: foo"
    assert compact_description(text) == "Get a quote. Use for prices."


def test_compact_schema_strips_noise():
    schema = {"title": "X", "type": "object", "properties": {
        "t": {"title": "T", "type": "string", "default": "AAPL", "description": "Ticker. Long extra text."},
        "response_format": {"type": "string"}}, "required": ["t"]}
    out = compact_schema(schema)
    assert out == {"type": "object", "properties": {"t": {"type": "string", "description": "Ticker."}},
                   "required": ["t"]}


def test_read_only_detection():
    tool = lambda name, hint=None: N(name=name, annotations=None if hint is None else N(readOnlyHint=hint))  # noqa: E731
    assert looks_read_only(tool("get_x")) and looks_read_only(tool("list_y"))
    assert not looks_read_only(tool("delete_z")) and not looks_read_only(tool("get_x", False))


def test_diet_rounds_and_downsamples():
    data = {"price": 12.3456789012, "history": [{"date": f"d{i}", "close": i + 0.123456789} for i in range(500)]}
    out = diet(data)
    assert out["price"] == 12.3457 and len(out["history"]) == 60
    assert out["history"][0]["date"] == "d0" and out["history"][-1]["date"] == "d499"
    short = {"history": [{"date": "d", "close": 1.0}] * 10}
    assert len(diet(short)["history"]) == 10


def test_sharesight_holdings_flattened_and_closed_dropped():
    raw = {"report": {"holdings": [
        {"instrument": {"code": "NVDA", "name": "Nvidia Corp", "market_code": "NASDAQ"}, "quantity": 5, "value": "100.5"},
        {"instrument": {"code": "OLD", "name": "Old Ltd"}, "quantity": 0, "value": "0"}],
        "id": 1, "grouping": "ungrouped"}, "api_transaction": {"x": 1}}
    out = json.loads(tidy_sharesight(json.dumps(raw), drop_closed=True))
    rows = table_records(out["report"]["holdings"])
    assert [r["code"] for r in rows] == ["NVDA"] and rows[0]["name"] == "Nvidia"
    assert "api_transaction" not in out and "id" not in out["report"]
    assert slim_result("not json", "sharesight") == "not json"


class FakeSession:
    def __init__(self):
        self.calls = []

    async def list_tools(self):
        mk = lambda n, ro=True: N(name=n, description=f"{n} does things.", inputSchema={"type": "object"},  # noqa: E731
                                  annotations=N(readOnlyHint=ro))
        return N(tools=[mk("get_b"), mk("get_a"), mk("delete_c", ro=False)])

    async def call_tool(self, tool, args):
        self.calls.append((tool, args))
        await asyncio.sleep(0.01)
        return N(isError=False, structuredContent=None,
                 content=[N(type="text", text=json.dumps({"p": 1.23456789, "tool": tool}))])


def make_server(**cfg):
    s = MCPServer("yahoo", cfg, timeout=5, max_output=10_000)
    s.session = FakeSession()
    return s


async def test_load_tools_sorted_and_not_duplicated_on_restart():
    s = make_server(blocked_tools=["get_b"])
    await s._load_tools(s.session)
    await s._load_tools(s.session)
    assert [t.name for t in s.tools] == ["yahoo__get_a"] and s.fn_names == {"yahoo__get_a": "get_a"}
    assert isinstance(s.tools[0], ToolDef)


async def test_call_slims_and_caches_identical_concurrent_calls():
    s = make_server(cache_ttl=30)
    a, b = await asyncio.gather(s.call("get_a", {"t": "X"}), s.call("get_a", {"t": "X"}))
    assert a == b == '{"p":1.23457,"tool":"get_a"}'
    await s.call("get_a", {"t": "X"})
    assert len(s.session.calls) == 1
    await s.call("get_a", {"t": "Y"})
    assert len(s.session.calls) == 2


async def test_no_cache_by_default_and_not_connected_message():
    s = make_server()
    await s.call("get_a", {})
    await s.call("get_a", {})
    assert len(s.session.calls) == 2
    s.session = None
    assert "isn't connected" in await s.call("get_a", {})


async def test_sharesight_holdings_only_flag_sets_include_sales():
    s = MCPServer("sharesight", {"current_holdings_only": True}, timeout=5, max_output=10_000)
    s.session = FakeSession()
    await s.call("get_performance_report", {"portfolio_id": 1})
    assert s.session.calls[0][1]["include_sales"] is False


async def test_output_cap():
    s = MCPServer("y", {}, timeout=5, max_output=20)
    s.session = FakeSession()
    out = await s.call("get_a", {})
    assert "truncated" in out and out.startswith('{"p":1.23457')


def test_registry_load_and_lookup(tmp_path):
    p = tmp_path / "m.json"
    p.write_text(json.dumps({"mcpServers": {"yahoo": {"command": "x"}, "off": {"command": "y", "disabled": True}}}))
    reg = Registry.load(str(p), timeout=5, max_output=100)
    assert list(reg.servers) == ["yahoo"] and reg.up() == [] and reg.lookup("yahoo__get_a") is None
    reg.servers["yahoo"].fn_names["yahoo__get_a"] = "get_a"
    assert reg.lookup("yahoo__get_a")[1] == "get_a"
    assert Registry.load(str(tmp_path / "missing.json"), timeout=5, max_output=100).servers == {}


async def test_start_is_idempotent():
    s = MCPServer("z", {"command": "sleep"}, timeout=5, max_output=100)
    started = []

    async def run():
        started.append(1)
        s._ready.set()
        await s._stop.wait()

    s._run = run
    s.start()
    first = s._task
    s.start()
    assert s._task is first
    await asyncio.sleep(0)
    await s.stop()
    assert started == [1]
