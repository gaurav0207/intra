"""
Data access layer.

Prefers Zerodha Kite Connect when an access token is provided, then
falls back to yfinance.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import yfinance as yf

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
INDEX_YF = "^NSEI"


def to_nse_symbol(symbol: str) -> str:
    symbol = symbol.strip().upper()
    if symbol in {"NIFTY", "NIFTY50", "NIFTY 50"}:
        return INDEX_YF
    if not symbol.endswith(".NS") and not symbol.endswith(".BO"):
        symbol += ".NS"
    return symbol


def next_session_label() -> str:
    """Next NSE cash session date (weekends skipped; holidays not known)."""
    now = dt.datetime.now(IST)
    d = now.date() + dt.timedelta(days=1)
    while d.weekday() >= 5:
        d += dt.timedelta(days=1)
    return d.strftime("%a %d %b")


def _tag(df: pd.DataFrame, source: str) -> pd.DataFrame:
    df = df.copy()
    df.attrs["source"] = source
    return df


def _normalize(df: pd.DataFrame, naive_as_ist: bool = False) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.rename(columns=str.lower)
    df = df[["open", "high", "low", "close", "volume"]]
    if df.index.tz is not None:
        df.index = df.index.tz_convert(IST)
    elif naive_as_ist:
        df.index = df.index.tz_localize(IST)
    else:
        df.index = df.index.tz_localize("UTC").tz_convert(IST)
    return df


def _from_kite(symbol: str, interval: str, lookback_days: int, kite) -> pd.DataFrame:
    from kite_client import fetch_ohlcv

    df = fetch_ohlcv(kite, symbol, interval, lookback_days)
    if df.empty:
        return df
    return _tag(df, "kite")


def _from_yahoo_daily(symbol: str, period: str) -> pd.DataFrame:
    ticker = to_nse_symbol(symbol)
    df = yf.download(
        ticker,
        interval="1d",
        period=period,
        progress=False,
        auto_adjust=False,
        multi_level_index=False,
    )
    if df.empty:
        return df
    df = _normalize(df, naive_as_ist=True)
    return _tag(df, "yahoo")


def _from_yahoo_intraday(symbol: str, interval: str, period: str) -> pd.DataFrame:
    ticker = to_nse_symbol(symbol)
    df = yf.download(
        ticker,
        interval=interval,
        period=period,
        progress=False,
        auto_adjust=False,
        multi_level_index=False,
    )
    if df.empty:
        return df
    df = _normalize(df)
    return _tag(df, "yahoo")


def fetch_daily(symbol: str, period: str = "6mo", kite=None) -> pd.DataFrame:
    """Daily OHLCV used for the next-session forecast and prior-day levels."""
    if kite is not None:
        try:
            df = _from_kite(symbol, "1d", 200, kite)
            if not df.empty:
                return df
        except Exception:
            pass
    return _from_yahoo_daily(symbol, period)


def fetch_intraday(symbol: str, interval: str = "5m", period: str = "1d", kite=None) -> pd.DataFrame:
    """Intraday OHLCV. Kite when connected, otherwise Yahoo."""
    lookback = 5 if interval == "1m" else 7
    if kite is not None:
        try:
            df = _from_kite(symbol, interval, lookback, kite)
            if not df.empty:
                # Keep today's session plus a little history for indicators.
                if period == "1d" and len(df):
                    last_day = df.index.tz_convert(IST)[-1].date()
                    today = dt.datetime.now(IST).date()
                    session = today if last_day >= today else last_day
                    day_mask = df.index.tz_convert(IST).date == session
                    session_df = df.loc[day_mask]
                    # Need enough bars for ADX/MACD; prepend previous days if thin.
                    if len(session_df) >= 40:
                        return _tag(session_df, df.attrs.get("source", "kite"))
                return df
        except Exception:
            pass
    return _from_yahoo_intraday(symbol, interval, period)


def fetch_index(interval: str = "5m", kite=None) -> pd.DataFrame:
    return fetch_intraday("NIFTY 50", interval=interval, period="5d", kite=kite)


def market_status() -> str:
    now = dt.datetime.now(IST)
    if now.weekday() >= 5:
        return "closed_weekend"
    open_t = now.replace(hour=9, minute=15, second=0, microsecond=0)
    close_t = now.replace(hour=15, minute=30, second=0, microsecond=0)
    if now < open_t:
        return "pre_open"
    if now > close_t:
        return "closed"
    return "open"
