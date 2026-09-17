"""Zerodha Kite Connect session + OHLCV helpers."""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

import pandas as pd

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
TOKEN_PATH = Path(".kite_token")
INTERVAL_MAP = {"1m": "minute", "5m": "5minute", "15m": "15minute", "1d": "day"}
INDEX_KITE = "NSE:NIFTY 50"


def api_key() -> str:
    return os.getenv("KITE_API_KEY", "").strip()


def api_secret() -> str:
    return os.getenv("KITE_API_SECRET", "").strip()


def login_url(key: str | None = None) -> str:
    from kiteconnect import KiteConnect

    kite = KiteConnect(api_key=key or api_key())
    return kite.login_url()


def load_saved_token() -> str | None:
    if not TOKEN_PATH.exists():
        return None
    try:
        data = json.loads(TOKEN_PATH.read_text())
        return data.get("access_token")
    except (OSError, json.JSONDecodeError):
        return None


def save_token(access_token: str) -> None:
    TOKEN_PATH.write_text(json.dumps({"access_token": access_token}))


def clear_token() -> None:
    if TOKEN_PATH.exists():
        TOKEN_PATH.unlink()


def exchange_request_token(request_token: str, secret: str, key: str | None = None) -> dict:
    from kiteconnect import KiteConnect

    kite = KiteConnect(api_key=key or api_key())
    data = kite.generate_session(request_token.strip(), api_secret=secret.strip())
    save_token(data["access_token"])
    return data


def make_kite(access_token: str, key: str | None = None):
    from kiteconnect import KiteConnect

    kite = KiteConnect(api_key=key or api_key())
    kite.set_access_token(access_token)
    return kite


def _nse_tradingsymbol(symbol: str) -> str:
    symbol = symbol.strip().upper().replace(".NS", "").replace(".BO", "")
    if symbol in {"NIFTY", "NIFTY50", "NIFTY 50"}:
        return INDEX_KITE
    return f"NSE:{symbol}"


def instrument_token(kite, symbol: str) -> int:
    quote_key = _nse_tradingsymbol(symbol)
    q = kite.ltp(quote_key)
    if quote_key not in q:
        raise KeyError(f"Kite LTP missing for {quote_key}: {q}")
    return int(q[quote_key]["instrument_token"])


def fetch_ohlcv(kite, symbol: str, interval: str, lookback_days: int) -> pd.DataFrame:
    from kiteconnect import KiteException

    token = instrument_token(kite, symbol)
    kite_interval = INTERVAL_MAP.get(interval, "5minute")
    now = dt.datetime.now(IST)
    start = now - dt.timedelta(days=lookback_days)
    try:
        records = kite.historical_data(token, start, now, kite_interval)
    except KiteException:
        raise
    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)
    df = df.rename(columns=str.lower)
    df["date"] = pd.to_datetime(df["date"])
    if df["date"].dt.tz is None:
        df["date"] = df["date"].dt.tz_localize(IST)
    else:
        df["date"] = df["date"].dt.tz_convert(IST)
    df = df.set_index("date")
    return df[["open", "high", "low", "close", "volume"]]
