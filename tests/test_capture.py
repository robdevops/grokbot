from datetime import date
from types import SimpleNamespace as N

from tools.capture import fill_args, result_text

TODAY = date(2026, 10, 6)


def schema(props, required):
    return {"type": "object", "properties": props, "required": required}


def test_fill_args_guesses_tickers_lists_dates_enums_and_defaults():
    s = schema({"ticker": {"type": "string"}, "tickers": {"type": "array"}, "start_date": {"type": "string"},
                "end_date": {"type": "string"}, "period": {"type": "string", "enum": ["1mo", "1y"]},
                "count": {"type": "integer"}, "flag": {"type": "boolean"}, "n": {"type": "integer", "default": 3}},
               ["ticker", "tickers", "start_date", "end_date", "period", "count", "flag", "n"])
    assert fill_args(s, TODAY) == {
        "ticker": "NVDA", "tickers": ["NVDA", "MSFT"], "start_date": "2026-09-06", "end_date": "2026-10-06",
        "period": "1mo", "count": 5, "flag": False, "n": 3}
    assert fill_args(s, TODAY, days=7)["start_date"] == "2026-09-29"


def test_fill_args_skips_optional_params_uses_known_overrides_and_gives_up_on_unknown_required():
    s = schema({"portfolio_id": {"type": "integer"}, "format": {"type": "string"},
                "symbol": {"anyOf": [{"type": "string"}, {"type": "null"}]}}, ["portfolio_id", "symbol"])
    assert fill_args(s, TODAY) is None  # a portfolio ID can't be guessed
    assert fill_args(s, TODAY, known={"portfolio_id": 7, "symbol": "CBA.AX"}) == {"portfolio_id": 7, "symbol": "CBA.AX"}
    assert fill_args(schema({}, []), TODAY) == {}


def test_result_text_marks_errors_and_non_text_parts():
    text = N(type="text", text='{"a": 1}')
    assert result_text(N(content=[text], isError=False)) == '{"a": 1}'
    assert result_text(N(content=[text], isError=True)) == 'Tool error: {"a": 1}'
    assert result_text(N(content=[N(type="image")], isError=False)) == "[image content omitted]"
