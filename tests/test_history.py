from zoneinfo import ZoneInfo

from tgbot.history import compact, format_rows
from tgbot.store import HistoryRow

UTC = ZoneInfo("UTC")


def test_compact_drops_tags_and_yahoo_urls():
    html = ('Buy <a href="https://finance.yahoo.com/quote/NVDA"><b>NVDA</b></a> &amp; '
            '<a href="https://ex.com/a">news</a>\n\n up')
    assert compact(html) == "Buy NVDA & news (https://ex.com/a) up"


def test_format_rows_labels_you_and_cuts_long_lines():
    rows = [HistoryRow(1, "Rob", "hi", 0, None),
            HistoryRow(2, "Bot (@b)", "x" * 500, 60, 1)]
    out = format_rows(rows, "Bot (@b)", UTC, line_max=100, compact_text=True)
    first, second = out.split("\n")
    assert first == "[#1] Thu 00:00 Rob: hi"
    assert second.startswith("[#2] Thu 00:01 You (replying to #1): xxx") and second.endswith("…[cut]")


def test_raw_mode_keeps_html():
    rows = [HistoryRow(1, "Rob", "<b>x</b>", 0, None)]
    assert format_rows(rows, "z", UTC, line_max=100, compact_text=False).endswith("<b>x</b>")
