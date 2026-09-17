"""Parallel signal scan across many symbols.

Fetching one symbol needs three network round trips (intraday, daily, 15m), so
a 50-name universe is far too slow sequentially. Daily candles are reused for
the whole session because they only change once a day.
"""

from __future__ import annotations

import datetime as dt
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pandas as pd

from data import IST, fetch_daily, fetch_index, fetch_intraday
from indicators import compute_all
from signals import attach_tomorrow_forecast, generate_signal

_daily_cache: dict[tuple[str, dt.date], pd.DataFrame] = {}


def _daily(symbol: str, kite) -> pd.DataFrame | None:
    key = (symbol, dt.datetime.now(IST).date())
    if key in _daily_cache:
        return _daily_cache[key]
    df = fetch_daily(symbol, kite=kite)
    if df.empty:
        return None
    enriched = compute_all(df)
    _daily_cache.clear()
    _daily_cache[key] = enriched
    return enriched


def _one(
    symbol: str,
    interval: str,
    kite,
    stop_mult: float,
    target_mult: float,
    long_only: bool,
    index_df: pd.DataFrame | None,
) -> tuple[str, Any, pd.DataFrame] | None:
    intraday = fetch_intraday(symbol, interval=interval, kite=kite)
    if intraday.empty:
        return None
    df = compute_all(intraday)

    daily = _daily(symbol, kite)
    htf = None
    if interval != "15m":
        raw_htf = fetch_intraday(symbol, interval="15m", period="5d", kite=kite)
        if not raw_htf.empty:
            htf = compute_all(raw_htf)

    sig = generate_signal(
        symbol, df, stop_mult, target_mult,
        long_only=long_only, daily=daily, htf=htf, index_df=index_df,
    )
    if sig is None:
        return None
    if daily is not None:
        sig = attach_tomorrow_forecast(sig, daily, stop_mult, target_mult, long_only=long_only)
    return symbol, sig, df


def scan(
    symbols: list[str],
    interval: str,
    kite=None,
    stop_mult: float = 1.5,
    target_mult: float = 2.5,
    long_only: bool = True,
    max_workers: int = 8,
) -> tuple[dict[str, Any], dict[str, pd.DataFrame], list[str]]:
    """Return (signals, candles, failed_symbols) for every symbol requested."""
    index_df = None
    try:
        raw_index = fetch_index(interval=interval, kite=kite)
        if not raw_index.empty:
            index_df = compute_all(raw_index)
    except Exception:  # noqa: BLE001
        index_df = None

    signals: dict[str, Any] = {}
    candles: dict[str, pd.DataFrame] = {}
    failed: list[str] = []

    def work(symbol: str):
        try:
            return _one(symbol, interval, kite, stop_mult, target_mult, long_only, index_df)
        except Exception:  # noqa: BLE001
            return None

    workers = max(1, min(max_workers, len(symbols)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for symbol, result in zip(symbols, pool.map(work, symbols)):
            if result is None:
                failed.append(symbol)
                continue
            _, sig, df = result
            signals[symbol] = sig
            candles[symbol] = df

    return signals, candles, failed
