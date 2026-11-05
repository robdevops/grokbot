"""Data for ticker linking: words that look like tickers but aren't, exchange suffixes, crypto."""

# Words shaped like tickers that aren't tickers. Replies never link a bare word unless it is a known ticker
# (tickers.KNOWN), so this list only keeps the tool gate, the /segments fast path and "Name (WORD)" honest.
NOT_TICKERS = {
    "AI", "AM", "PM", "AND", "THE", "NOT", "BUT", "FOR", "ALL", "NEW", "OLD", "OK",
    "US", "USA", "UK", "EU", "AU", "CN", "UTC", "AEST", "AEDT", "GMT",
    "CEO", "CFO", "COO", "CTO", "IPO", "ETF", "ETN", "REIT", "SPAC", "LLC", "INC",
    "GDP", "CPI", "PPI", "FED", "FOMC", "ECB", "RBA", "BOJ", "SEC", "ASIC", "ATO",
    "ASX", "NYSE", "LSE", "TSX", "OTC", "CBOE", "CME",
    "EPS", "PE", "PEG", "DCF", "FCF", "EBIT", "ROE", "ROI", "ROIC", "TAM", "YOY",
    "YTD", "LTM", "TTM", "FY", "HY", "QOQ", "MOM", "EOD", "ATH", "ATL", "MA", "RSI",
    "USD", "AUD", "EUR", "GBP", "JPY", "CNY",
    "NOTE", "EDIT", "TLDR", "FYI", "IMO", "IMHO", "AKA", "ETA", "VS", "PS",
    "VR", "AR", "XR", "API", "GPU", "CPU", "TPU", "HBM", "DRAM", "NAND", "EUV", "OS",
    "U.S", "U.K", "P.A", "E.G", "I.E", "SMSF", "SUPER", "PNL",
    "GAAP", "EBITDA", "CAGR", "ARPU", "AFR", "NAV", "AUM", "DPS", "VWAP", "SMA", "EMA", "MACD", "LLM", "AGI",
    "WSJ", "NZD", "CAD", "CHF",
}

# Index tickers that are everyday words in capitals ("IT spending", "ALL in"): linked only as $IT or Name (IT).
WORD_TICKERS = {"IT", "ALL", "ON", "NOW", "KEY", "LOW", "ARE", "DAY", "HAS", "WELL", "BALL", "FAST", "CAT", "AMP"}

# An exchange as the model, the press or Sharesight writes it ("ASX:NXT", "NXT (ASX)") -> Yahoo's suffix ("" = US,
# none). TSE is Toronto for letters and Tokyo for numbers (tickers.yahoo_symbol); HK numbers are padded to 4 digits.
EXCHANGE_PREFIXES = {
    "ASX": "AX", "NZX": "NZ", "LSE": "L", "LON": "L", "TSX": "TO", "TSE": "TO", "TSXV": "V", "CVE": "V",
    "HKEX": "HK", "HKG": "HK", "SEHK": "HK", "TYO": "T", "JPX": "T", "KRX": "KS", "KOSDAQ": "KQ",
    "XETRA": "DE", "ETR": "DE", "FRA": "F", "EPA": "PA", "AMS": "AS", "EBR": "BR", "SWX": "SW", "SIX": "SW",
    "BIT": "MI", "BME": "MC", "STO": "ST", "OSL": "OL", "CPH": "CO", "HEL": "HE", "SGX": "SI", "SHA": "SS",
    "SHE": "SZ", "TPE": "TW", "BOM": "BO", "NSE": "NS", "BVMF": "SA", "BMV": "MX", "JSE": "JO", "TLV": "TA",
    "NYSE": "", "NASDAQ": "", "NYSEARCA": "", "NYSEAMERICAN": "", "AMEX": "", "OTC": "", "OTCMKTS": "",
    "CBOE": "", "BATS": "",
}

# Yahoo needs the exchange suffix in the URL, but it's noise in the chat: link SQX.AX, show SQX.
# Share classes like BRK.B are not suffixes, so they stay as written.
EXCHANGE_SUFFIXES = {
    "AX", "L", "TO", "V", "NZ", "HK", "SS", "SZ", "T", "KS", "DE",
    "PA", "AS", "MI", "MC", "ST", "OL", "SI", "BO", "NS", "SA", "MX",
} | {suffix for suffix in EXCHANGE_PREFIXES.values() if suffix}

# Yahoo quotes crypto as BTC-USD, not BTC.
CRYPTO = {
    "BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "BNB", "LTC", "DOT", "AVAX",
    "LINK", "TRX", "SHIB", "PEPE", "WLFI", "USDT", "USDC",
}
