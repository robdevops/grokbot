from lib import tickers
from lib.tickers import link_tickers, ticker_label, yahoo_symbol, yahoo_url

Y = "https://finance.yahoo.com/quote/"


def a(symbol, label):
    return f'<a href="{Y}{symbol}">{label}</a>'


def test_bare_words_are_never_linked_so_acronyms_need_no_stoplist():
    for text in ("non-GAAP EBITDA; AFR says GM, UPS, M3, VWAP, XJO, AT&amp;T, S&amp;P 500", "NVDA up 3%",
                 "AI and CEO, Q3 FY26", "F 5.2 today and vitamin C", "BTC and LINK bare", "I.T. staff in the U.S.",
                 "FY2026.T and Q3.AX", "NXT:ASX and NXT.ASX"):
        assert link_tickers(text) == text


def test_dollar_amounts_and_currencies_are_not_cashtags():
    for text in ("$600, $12.AX, $30.5T, US$1.2T, $A1.2bn and $US2bn", "the $A fell; $AUD and $US weak"):
        assert link_tickers(text) == text


def test_cashtags_link_without_the_dollar_or_bold():
    assert link_tickers("$NVDA up 3%") == f"{a('NVDA', 'NVDA')} up 3%"
    assert link_tickers("$SQX.AX and $BRK-B") == f"{a('SQX.AX', 'SQX')} and {a('BRK-B', 'BRK-B')}"
    assert link_tickers("$BTC rallied") == f"{a('BTC-USD', 'BTC')} rallied" and yahoo_url("BTC").endswith("BTC-USD")


def test_exchange_suffix_links_with_the_suffix_hidden():
    out = link_tickers("SQX.AX and BRK.B and 9988.HK and 000660.KS")
    assert out == f"{a('SQX.AX', 'SQX')} and BRK.B and {a('9988.HK', '9988')} and {a('000660.KS', '000660')}"
    assert link_tickers("RCI-B.TO") == a("RCI-B.TO", "RCI-B")
    assert ticker_label("BRK.B") == "BRK.B" and ticker_label("SQX.AX") == "SQX"


def test_exchange_prefix_maps_to_the_yahoo_suffix():
    assert link_tickers("up on ASX:NXT.") == f"up on {a('NXT.AX', 'NXT')}."
    assert link_tickers("(ASX: NXT)") == f"({a('NXT.AX', 'NXT')})"
    assert link_tickers("NASDAQ: NVDA, LON:BP, NYSE:F") == f"{a('NVDA', 'NVDA')}, {a('BP.L', 'BP')}, {a('F', 'F')}"
    assert link_tickers("TSE:7203 TSE:SHOP HKG:700") == f"{a('7203.T', '7203')} {a('SHOP.TO', 'SHOP')} {a('0700.HK', '700')}"
    assert yahoo_symbol("NXT", "asx") == "NXT.AX" and yahoo_symbol("NVDA", "NASDAQ") == "NVDA"
    assert yahoo_symbol("X", "MOON") is None


def test_indices_and_crypto_pairs():
    assert link_tickers("^GSPC and BTC-USD") == f"{a('%5EGSPC', '^GSPC')} and {a('BTC-USD', 'BTC')}"


def test_name_and_ticker_are_bolded_together():
    known = {"MU": "MU", "TTD": "TTD", "F": "F"}
    out = link_tickers("• Micron (MU) +3.1%\n1. Trade Desk (TTD) -5%", known)
    assert f"• <b>Micron ({a('MU', 'MU')})</b> +3.1%" in out and f"1. <b>Trade Desk ({a('TTD', 'TTD')})</b> -5%" in out
    assert link_tickers("DUG Tech (DUG.AX) fell") == f"<b>DUG Tech ({a('DUG.AX', 'DUG')})</b> fell"
    assert link_tickers("NextDC (ASX:NXT) +5%") == f"<b>NextDC ({a('NXT.AX', 'NXT')})</b> +5%"
    assert link_tickers("Nvidia (NASDAQ: NVDA)") == f"<b>Nvidia ({a('NVDA', 'NVDA')})</b>"
    assert link_tickers("SK Hynix (000660.KS)") == f"<b>SK Hynix ({a('000660.KS', '000660')})</b>"
    assert link_tickers("Ford (F) fell", known) == f"<b>Ford ({a('F', 'F')})</b> fell"


def test_a_bare_code_in_brackets_links_only_when_known_since_it_names_no_exchange():
    text = "Goodman Group (GMG) and Digital Realty (DLR)"
    assert link_tickers(text) == text  # GMG is GMG.AX: a US link would be wrong
    assert link_tickers(text, {"DLR": "DLR"}) == f"Goodman Group (GMG) and <b>Digital Realty ({a('DLR', 'DLR')})</b>"
    for text in ("the Fed (RBA) held and vitamin C (CEO)", "Australian Financial Review (AFR) reports"):
        assert link_tickers(text, {"MU": "MU"}) == text


def test_bolding_leaves_sentence_openers_and_existing_bold_alone():
    known = {"MU": "MU", "NVDA": "NVDA"}
    assert link_tickers("Today Micron (MU) ripped", known).startswith("Today <b>Micron (")
    assert link_tickers("<b>Micron (MU)</b> up", known) == f"<b>Micron ({a('MU', 'MU')})</b> up"
    assert link_tickers("<code>Micron (MU)</code>", known) == "<code>Micron (MU)</code>"
    assert link_tickers("Micron (MU)\nNvidia (NVDA)", known).count("<b>") == 2  # a line break ends a name


def test_sharesight_code_and_market_links_the_code_and_keeps_the_exchange():
    assert link_tickers("• NVDA (NASDAQ): beat") == f"• {a('NVDA', 'NVDA')} (NASDAQ): beat"
    assert link_tickers("• NXT (ASX): halt") == f"• {a('NXT.AX', 'NXT')} (ASX): halt"
    assert link_tickers("Nvidia (NASDAQ) fell") == "Nvidia (NASDAQ) fell"


def test_known_tickers_link_bare_but_not_single_letters_or_everyday_words():
    known = {"NVDA": "NVDA", "AMD": "AMD", "GM": "GM", "IT": "IT", "A": "A", "ALL": "ALL", "700": "0700.HK"}
    out = link_tickers("NVDA up, AMD too; GM expanded, IT spending, A big move, ALL in, 1,700 staff", known)
    assert out == f"{a('NVDA', 'NVDA')} up, {a('AMD', 'AMD')} too; GM expanded, IT spending, A big move, ALL in, 1,700 staff"
    assert link_tickers("General Motors (GM) and $GM", known) == f"<b>General Motors ({a('GM', 'GM')})</b> and {a('GM', 'GM')}"
    before = dict(tickers.KNOWN)
    try:
        tickers.set_known({"NVDA": "NVDA"})
        assert link_tickers("NVDA and AMD") == f"{a('NVDA', 'NVDA')} and AMD"  # the daily list, with no `known` passed
    finally:
        tickers.set_known(before)


def test_an_exchange_suffix_links_even_a_code_that_is_also_a_word():
    assert link_tickers("Aristocrat (ALL.AX) rose") == f"<b>Aristocrat ({a('ALL.AX', 'ALL')})</b> rose"
    assert link_tickers("ASX.AX, $ALL.AX and AI.PA") == f"{a('ASX.AX', 'ASX')}, {a('ALL.AX', 'ALL')} and {a('AI.PA', 'AI')}"
    assert link_tickers("ALL in, $FY26.AX") == "ALL in, $FY26.AX"


def test_a_bracket_that_is_not_a_ticker_leaves_the_words_before_it_linkable():
    known = {"NVDA": "NVDA"}
    assert link_tickers("NVDA (Q3 FY26) revenue", known) == f"{a('NVDA', 'NVDA')} (Q3 FY26) revenue"
    assert link_tickers("NXT.AX (FY26) guidance") == f"{a('NXT.AX', 'NXT')} (FY26) guidance"


def test_share_classes_written_with_a_dot_link_to_yahoos_hyphen():
    known = {"BRK-B": "BRK-B", "BRK.B": "BRK-B"}
    assert link_tickers("Berkshire Hathaway (BRK.B) rose", known) == f"<b>Berkshire Hathaway ({a('BRK-B', 'BRK.B')})</b> rose"
    assert link_tickers("$BRK.B and BRK.B", known) == f"{a('BRK-B', 'BRK.B')} and {a('BRK-B', 'BRK.B')}"


def test_names_with_an_escaped_ampersand_are_bolded_whole():
    known = {"T": "T", "PG": "PG", "SPGI": "SPGI"}
    assert link_tickers("AT&amp;T (T) rose", known) == f"<b>AT&amp;T ({a('T', 'T')})</b> rose"
    assert link_tickers("Procter &amp; Gamble (PG)", known) == f"<b>Procter &amp; Gamble ({a('PG', 'PG')})</b>"
    assert link_tickers("S&amp;P Global (SPGI)", known) == f"<b>S&amp;P Global ({a('SPGI', 'SPGI')})</b>"


def test_asx_codes_from_the_known_set_link_with_their_suffix_but_word_codes_do_not():
    known = {"BHP": "BHP.AX", "GPT": "GPT.AX"}
    assert link_tickers("BHP fell; GPT models", known) == f"{a('BHP.AX', 'BHP')} fell; GPT models"
    assert link_tickers("GPT Group (GPT)", known) == f"<b>GPT Group ({a('GPT.AX', 'GPT')})</b>"


def test_protected_markup_is_left_alone():
    assert link_tickers('<a href="x">NVDA</a> and $AMD') == f'<a href="x">NVDA</a> and {a("AMD", "AMD")}'
    assert link_tickers("<pre>$NVDA 12.3</pre> $AMD").startswith("<pre>$NVDA 12.3</pre> <a ")
    assert link_tickers("<code>$NVDA</code>") == "<code>$NVDA</code>"
    assert link_tickers("<b>$AMD</b>") == f"<b>{a('AMD', 'AMD')}</b>"


def test_raw_links_go_behind_citation_numbers():
    out = link_tickers("see https://a.com/x/NVDA. And (https://b.com/y?q=1&amp;z=2) plus https://a.com/x/NVDA again")
    assert out.count('<a href="https://a.com/x/NVDA">[1]</a>') == 2  # same URL, same number
    assert '(<a href="https://b.com/y?q=1&amp;z=2">[2]</a>)' in out and "[1]</a>. And" in out
    assert "quote/NVDA" not in out  # the ticker inside the URL is not linked


def test_citations_on_their_own_lines_join_the_preceding_text():
    a, b = '<a href="https://a.com/1">[1]</a>', '<a href="https://b.com/2">[2]</a>'
    assert link_tickers("Thin volume.\n\nhttps://a.com/1\nhttps://b.com/2") == f"Thin volume.{a}{b}"
    assert link_tickers("One.\n\nhttps://a.com/1\n\nTwo.\nhttps://b.com/2") == f"One.{a}\n\nTwo.{b}"
    assert link_tickers("Said https://a.com/1 then more") == f"Said {a} then more"  # inline: untouched
    assert link_tickers("Thin.\nhttps://a.com/1 and more") == f"Thin.\n{a} and more"  # not a citation-only line


def test_link_labelled_with_its_own_url_is_numbered_but_named_links_and_code_are_not():
    assert link_tickers('<a href="https://y.com/p">https://y.com/p…</a> ok') == '<a href="https://y.com/p">[1]</a> ok'
    assert link_tickers('<a href="https://y.com/p">Reuters</a>') == '<a href="https://y.com/p">Reuters</a>'
    assert link_tickers("<code>https://x.com</code>") == "<code>https://x.com</code>"


def test_google_finance_links_name_the_exchange_where_the_symbol_does(env):
    from lib import config
    from lib.history import compact
    assert config.load({**env, "FINANCE_LINK_TARGET": "Google"}).link_target == "google"
    assert config.load(env).link_target == "yahoo"
    try:
        config.load({**env, "FINANCE_LINK_TARGET": "bing"})
        raise AssertionError("bing accepted")
    except config.ConfigError:
        pass
    g = tickers.google_url
    assert g("NXT.AX") == "https://www.google.com/finance/quote/NXT:ASX"
    assert g("0700.HK") == "https://www.google.com/finance/quote/700:HKG" and g("7203.T").endswith("/7203:TYO")
    assert g("BTC") == g("BTC-USD") == "https://www.google.com/finance/quote/BTC-USD"
    assert g("NVDA") == "https://www.google.com/finance?q=NVDA" and g("BRK-B").endswith("?q=BRK.B")
    tickers.set_target("google")
    try:
        out = link_tickers("ASX:NXT and $NVDA")
        assert out == ('<a href="https://www.google.com/finance/quote/NXT:ASX">NXT</a> and '
                       '<a href="https://www.google.com/finance?q=NVDA">NVDA</a>')
        assert compact(out) == "NXT and NVDA" and tickers.quote_labels(out) == {"NXT", "NVDA"}
    finally:
        tickers.set_target("yahoo")


def test_google_links_take_the_us_exchange_from_the_text_or_the_known_lists():
    g = tickers.google_url
    before = dict(tickers.US_EXCHANGES)
    try:
        tickers.set_us_exchanges({"JPM": "NYSE", "SPCX": "NASDAQ", "BRK-B": "NYSE"})
        assert g("JPM") == "https://www.google.com/finance/quote/JPM:NYSE"
        assert g("SPCX") == "https://www.google.com/finance/quote/SPCX:NASDAQ"
        assert g("BRK-B") == "https://www.google.com/finance/quote/BRK.B:NYSE"
        assert g("CBA.AX") == "https://www.google.com/finance/quote/CBA:ASX"
        assert g("ACME", "NYSE") == "https://www.google.com/finance/quote/ACME:NYSE"  # NYSE:ACME, ACME (NYSE)
        assert g("ACME") == "https://www.google.com/finance?q=ACME"  # no exchange known: a search
        tickers.set_target("google")
        assert 'quote/NVDA:NASDAQ"' in link_tickers("NASDAQ: NVDA and NVDA (NASDAQ) and Nvidia (NASDAQ:NVDA)")
        assert 'quote/JPM:NYSE">JPM</a>' in link_tickers("$JPM")
    finally:
        tickers.set_target("yahoo")
        tickers.set_us_exchanges(before)
