from tgbot.tickers import link_tickers, ticker_label, yahoo_url


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
