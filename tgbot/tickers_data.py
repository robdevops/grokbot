"""Data for ticker linking: words that look like tickers but aren't, exchange suffixes, crypto."""

# Words shaped like tickers that aren't tickers.
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
    "U.S", "U.K", "P.A", "E.G", "I.E",
}

# Yahoo needs the exchange suffix in the URL, but it's noise in the chat: link SQX.AX, show SQX.
# Share classes like BRK.B are not suffixes, so they stay as written.
EXCHANGE_SUFFIXES = {
    "AX", "L", "TO", "V", "NZ", "HK", "SS", "SZ", "T", "KS", "DE",
    "PA", "AS", "MI", "MC", "ST", "OL", "SI", "BO", "NS", "SA", "MX",
}

# Yahoo quotes crypto as BTC-USD, not BTC.
CRYPTO = {
    "BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "BNB", "LTC", "DOT", "AVAX",
    "LINK", "TRX", "SHIB", "PEPE", "WLFI", "USDT", "USDC",
}
