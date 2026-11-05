"""Where ticker links go (FINANCE_LINK_TARGET): Yahoo Finance, Google Finance or TradingView, built from a Yahoo symbol."""

from __future__ import annotations

import re
from collections.abc import Mapping
from urllib.parse import quote

from .tickers_data import CRYPTO

YAHOO_QUOTE = "https://finance.yahoo.com/quote/"
GOOGLE_QUOTE = "https://www.google.com/finance/quote/"
GOOGLE_SEARCH = "https://www.google.com/finance?q="
TRADINGVIEW = "https://www.tradingview.com/symbols/"
QUOTE_PREFIXES = (YAHOO_QUOTE, GOOGLE_QUOTE, GOOGLE_SEARCH, TRADINGVIEW)  # history.compact, news_codes read these back
TARGETS = ("yahoo", "google", "tradingview")
TARGET = ["yahoo"]  # set at startup (app.build_ctx)

# Yahoo's suffix -> the exchange as each site names it ("NXT.AX" -> Google "NXT:ASX", TradingView "ASX-NXT")
GOOGLE_EXCHANGES = {
    "AX": "ASX", "NZ": "NZE", "L": "LON", "TO": "TSE", "V": "CVE", "HK": "HKG", "T": "TYO", "KS": "KRX",
    "KQ": "KOSDAQ", "DE": "ETR", "F": "FRA", "PA": "EPA", "AS": "AMS", "BR": "EBR", "SW": "SWX", "MI": "BIT",
    "MC": "BME", "ST": "STO", "OL": "OSL", "CO": "CPH", "HE": "HEL", "SI": "SGX", "SS": "SHA", "SZ": "SHE",
    "TW": "TPE", "BO": "BOM", "NS": "NSE", "SA": "BVMF", "MX": "BMV", "JO": "JSE", "TA": "TLV",
}
TRADINGVIEW_EXCHANGES = {
    "AX": "ASX", "NZ": "NZX", "L": "LSE", "TO": "TSX", "V": "TSXV", "HK": "HKEX", "T": "TSE", "KS": "KRX",
    "KQ": "KRX", "DE": "XETR", "F": "FWB", "PA": "EURONEXT", "AS": "EURONEXT", "BR": "EURONEXT", "SW": "SIX",
    "MI": "MIL", "MC": "BME", "ST": "OMXSTO", "OL": "OSL", "CO": "OMXCOP", "HE": "OMXHEX", "SI": "SGX", "SS": "SSE",
    "SZ": "SZSE", "TW": "TWSE", "BO": "BSE", "NS": "NSE", "SA": "BMFBOVESPA", "MX": "BMV", "JO": "JSE", "TA": "TASE",
}
# A US listing's exchange as Google Finance names it ("JPM": "NYSE"), from features/known_tickers.py; Yahoo needs none.
US_EXCHANGES: dict[str, str] = {}
GOOGLE_US = {"NASDAQ": "NASDAQ", "NYSE": "NYSE", "NYSEARCA": "NYSEARCA", "NYSEAMERICAN": "NYSEAMERICAN",
             "AMEX": "NYSEAMERICAN", "CBOE": "BATS", "BATS": "BATS", "OTC": "OTCMKTS", "OTCMKTS": "OTCMKTS"}
TRADINGVIEW_US = {"NASDAQ": "NASDAQ", "NYSE": "NYSE", "NYSEARCA": "AMEX", "NYSEAMERICAN": "AMEX", "BATS": "CBOE",
                  "OTCMKTS": "OTC"}  # from Google's names above
TRADINGVIEW_INDICES = {"^GSPC": "SP-SPX", "^IXIC": "NASDAQ-IXIC", "^NDX": "NASDAQ-NDX", "^DJI": "DJ-DJI",
                       "^AXJO": "ASX-XJO", "^VIX": "CBOE-VIX"}


def set_target(name: str) -> None:
    TARGET[0] = name


def set_us_exchanges(exchanges: Mapping[str, str]) -> None:
    US_EXCHANGES.clear()
    US_EXCHANGES.update(exchanges)


def _listing(symbol: str, market: str, table: Mapping[str, str]) -> tuple[str, str | None]:
    """A Yahoo symbol as (code, exchange name in `table`), the exchange None when unknown: NXT.AX -> (NXT, ASX),
    0700.HK -> (700, HKG), BRK-B -> (BRK.B, NYSE) from `market` (as the text named it) or US_EXCHANGES."""
    base, _, suffix = symbol.rpartition(".")
    if base and suffix in table:
        return (base.lstrip("0") or "0") if suffix == "HK" else base, table[suffix]
    return symbol.replace("-", "."), GOOGLE_US.get(market.upper()) or US_EXCHANGES.get(symbol)


def yahoo_url(symbol: str) -> str:
    sym = f"{symbol}-USD" if symbol in CRYPTO else symbol
    return YAHOO_QUOTE + quote(sym, safe="")


def google_url(symbol: str, market: str = "") -> str:
    """Google Finance: CODE:EXCHANGE (NXT:ASX, BRK.B:NYSE), BTC-USD as is, else a search (an exchange we can't place)."""
    if symbol in CRYPTO or symbol.endswith("-USD"):
        return GOOGLE_QUOTE + quote(symbol if symbol.endswith("-USD") else f"{symbol}-USD", safe="")
    code, exchange = _listing(symbol, market, GOOGLE_EXCHANGES)
    if exchange and not symbol.startswith("^"):
        return GOOGLE_QUOTE + quote(f"{code}:{exchange}", safe=":.")
    return GOOGLE_SEARCH + quote(code, safe="")


def tradingview_url(symbol: str, market: str = "") -> str:
    """TradingView: EXCHANGE-CODE (ASX-NXT, NYSE-BRK.A); a code alone where we can't place it (TradingView picks its
    main listing); BTCUSD for crypto."""
    if symbol in TRADINGVIEW_INDICES:
        return f"{TRADINGVIEW}{TRADINGVIEW_INDICES[symbol]}/"
    if symbol in CRYPTO or symbol.endswith("-USD"):
        return f"{TRADINGVIEW}{quote(symbol.removesuffix('-USD') + 'USD', safe='')}/"
    code, exchange = _listing(symbol, market, TRADINGVIEW_EXCHANGES)
    if exchange and exchange not in TRADINGVIEW_EXCHANGES.values():
        exchange = TRADINGVIEW_US.get(exchange)  # a US exchange, as Google names it
    name = f"{exchange}-{code}" if exchange else code.lstrip("^")
    return f"{TRADINGVIEW}{quote(name, safe='.-')}/"


def quote_url(symbol: str, market: str = "") -> str:
    """The quote page for a Yahoo symbol at the FINANCE_LINK_TARGET (`market`: the exchange the text named)."""
    if TARGET[0] == "google":
        return google_url(symbol, market)
    if TARGET[0] == "tradingview":
        return tradingview_url(symbol, market)
    return yahoo_url(symbol)


def quote_labels(html: str) -> set[str]:
    """The labels of the quote links in a reply (the codes as shown: NXT, 700), whichever site they point to."""
    starts = "|".join(re.escape(p) for p in QUOTE_PREFIXES)
    return set(re.findall(rf'<a href="(?:{starts})[^"]*">([^<]+)</a>', html))
