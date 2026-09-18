"""The trading universe the app scans on its own.

This is deliberately separate from the sidebar watchlist. The watchlist is for
looking at charts; these lists are what the paper trader is allowed to buy.
Names are liquid NSE cash-segment stocks, chosen so intraday spreads stay tight.
"""

from __future__ import annotations

LIQUID_20 = [
    "RELIANCE", "HDFCBANK", "ICICIBANK", "SBIN", "AXISBANK",
    "INFY", "TCS", "HCLTECH", "BHARTIARTL", "ITC",
    "LT", "MARUTI", "TMPV", "TATASTEEL", "HINDALCO",
    "JSWSTEEL", "ADANIENT", "ADANIPORTS", "COALINDIA", "ONGC",
]

NIFTY_50 = [
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK",
    "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV", "BEL", "BHARTIARTL",
    "BPCL", "BRITANNIA", "CIPLA", "COALINDIA", "DRREDDY",
    "EICHERMOT", "GRASIM", "HCLTECH", "HDFCBANK", "HDFCLIFE",
    "HEROMOTOCO", "HINDALCO", "HINDUNILVR", "ICICIBANK", "INDUSINDBK",
    "INFY", "ITC", "JSWSTEEL", "KOTAKBANK", "LT",
    "M&M", "MARUTI", "NESTLEIND", "NTPC", "ONGC",
    "POWERGRID", "RELIANCE", "SBILIFE", "SBIN", "SHRIRAMFIN",
    "SUNPHARMA", "TATACONSUM", "TMPV", "TATASTEEL", "TCS",
    "TECHM", "TITAN", "TRENT", "ULTRACEMCO", "WIPRO",
]

HIGH_BETA = [
    "TMPV", "TATASTEEL", "HINDALCO", "JSWSTEEL", "ADANIENT",
    "ADANIPORTS", "VEDL", "SAIL", "IDFCFIRSTB", "PNB",
    "BANKBARODA", "CANBK", "IEX", "RBLBANK", "ETERNAL",
]

UNIVERSES: dict[str, list[str]] = {
    "Liquid 20 (default)": LIQUID_20,
    "Nifty 50": NIFTY_50,
    "High beta movers": HIGH_BETA,
}

DEFAULT_UNIVERSE = "Liquid 20 (default)"


def get(name: str) -> list[str]:
    return list(UNIVERSES.get(name, LIQUID_20))
