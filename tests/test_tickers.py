from lib.tickers import link_tickers, ticker_label, yahoo_url


def test_link_only_no_bold():
    out = link_tickers("NVDA up 3%")
    assert out == '<a href="https://finance.yahoo.com/quote/NVDA">NVDA</a> up 3%'
    assert "<b>" not in out


def test_label_hides_suffix_url_keeps_it():
    out = link_tickers("SQX.AX and BRK.B and 9988.HK")
    assert '<a href="https://finance.yahoo.com/quote/SQX.AX">SQX</a>' in out
    assert '<a href="https://finance.yahoo.com/quote/BRK.B">BRK.B</a>' in out
    assert '<a href="https://finance.yahoo.com/quote/9988.HK">9988</a>' in out
    assert ticker_label("BRK.B") == "BRK.B" and ticker_label("SQX.AX") == "SQX"


def test_crypto_gets_usd_in_url_only():
    assert yahoo_url("BTC").endswith("BTC-USD")
    assert '>BTC</a>' in link_tickers("BTC rallied")


def test_protected_markup_is_left_alone():
    assert link_tickers('<a href="x">NVDA</a> and AI') == '<a href="x">NVDA</a> and AI'
    assert link_tickers("<pre>NVDA 12.3</pre> AMD").startswith("<pre>NVDA 12.3</pre> <a ")
    assert link_tickers("<code>NVDA</code>") == "<code>NVDA</code>"
    # a ticker the model bolded still gets linked (inside the bold)
    assert link_tickers("<b>AMD</b>").startswith("<b><a ")


def test_stoplist_and_one_letter():
    assert link_tickers("AI and CEO, Q3 FY26") == "AI and CEO, Q3 FY26"
    assert 'quote/F"' in link_tickers("F 5.2 today")
    assert link_tickers("vitamin C helps") == "vitamin C helps"


def test_company_name_and_ticker_are_bolded_together():
    out = link_tickers("• Micron (MU) +3.1%\n1. Trade Desk (TTD) -5%")
    assert '• <b>Micron (<a href="https://finance.yahoo.com/quote/MU">MU</a>)</b> +3.1%' in out
    assert '1. <b>Trade Desk (<a href="https://finance.yahoo.com/quote/TTD">TTD</a>)</b> -5%' in out
    ext = link_tickers("DUG Tech (DUG.AX) fell")
    assert ext.startswith("<b>") and ">DUG</a>)</b> fell" in ext and 'quote/DUG.AX"' in ext


def test_bolding_leaves_sentence_openers_existing_bold_and_non_tickers_alone():
    assert link_tickers("Today Micron (MU) ripped").startswith("Today <b>Micron (")
    assert link_tickers("<b>Micron (MU)</b> up").count("<b>") == 1
    assert link_tickers("<b>Micron</b> (MU)").count("<b>") == 1
    assert "<b>" not in link_tickers("the Fed (RBA) held and vitamin C (CEO)")
    assert "<b>" not in link_tickers("<code>Micron (MU)</code>")
    assert link_tickers("Micron (MU)\nNvidia (NVDA)").count("<b>") == 2  # a line break ends a name


def test_raw_links_go_behind_citation_numbers():
    out = link_tickers("see https://a.com/x/NVDA. And (https://b.com/y?q=1&amp;z=2) plus https://a.com/x/NVDA again")
    assert out.count('<a href="https://a.com/x/NVDA">[1]</a>') == 2  # same URL, same number
    assert '(<a href="https://b.com/y?q=1&amp;z=2">[2]</a>)' in out and "[1]</a>. And" in out
    assert "quote/NVDA" not in out  # the ticker inside the URL is not linked


def test_link_labelled_with_its_own_url_is_numbered_but_named_links_and_code_are_not():
    assert link_tickers('<a href="https://y.com/p">https://y.com/p…</a> ok') == '<a href="https://y.com/p">[1]</a> ok'
    assert link_tickers('<a href="https://y.com/p">Reuters</a>') == '<a href="https://y.com/p">Reuters</a>'
    assert link_tickers("<code>https://x.com</code>") == "<code>https://x.com</code>"
