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
            HistoryRow(2, "Bot (@b)", "x" * 500, 60, 1),
            HistoryRow(3, "Bot (@b)", "latest", 120, None)]  # the older reply is cut; the latest isn't
    out = format_rows(rows, "Bot (@b)", UTC, line_max=100, compact_text=True)
    first, second, third = out.split("\n")
    assert first == "[#1] Thu 00:00 Rob: hi" and third.endswith("You: latest")
    assert second.startswith("[#2] Thu 00:01 You (replying to #1): xxx") and second.endswith("…[cut]")


def test_latest_own_reply_is_kept_whole_older_ones_are_cut():
    long_answer = "Markets: " + "x" * 800 + " the training incident was at Mount Bundey"
    rows = [HistoryRow(1, "Bot (@b)", long_answer, 0, None), HistoryRow(2, "Rob (@rob)", "thanks", 60, None),
            HistoryRow(3, "Bot (@b)", long_answer, 120, None), HistoryRow(4, "Rob (@rob)", "what incident?", 180, None)]
    for compact_text, own in ((True, 160), (False, None)):
        lines = format_rows(rows, "Bot (@b)", UTC, line_max=240, compact_text=compact_text,
                            own_line_max=own).split("\n")
        assert lines[0].endswith("…[cut]") and "Mount Bundey" not in lines[0]  # an earlier reply: cut
        assert lines[2].endswith("Mount Bundey") and "…[cut]" not in lines[2]  # the latest: whole


def test_latest_own_reply_has_a_generous_cap_and_no_own_reply_is_fine():
    from tgbot.history import LAST_REPLY_MAX
    rows = [HistoryRow(1, "Bot (@b)", "y" * (LAST_REPLY_MAX + 500), 0, None)]
    assert format_rows(rows, "Bot (@b)", UTC, line_max=240, compact_text=True).endswith("…[cut]")
    people = [HistoryRow(1, "Rob (@rob)", "z" * 500, 0, None)]
    assert format_rows(people, "Bot (@b)", UTC, line_max=240, compact_text=True).endswith("…[cut]")


def test_raw_mode_keeps_html():
    rows = [HistoryRow(1, "Rob", "<b>x</b>", 0, None)]
    assert format_rows(rows, "z", UTC, line_max=100, compact_text=False).endswith("<b>x</b>")
