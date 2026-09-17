"""
Technical indicators used for intraday signal generation.
All functions take a pandas DataFrame with columns:
    open, high, low, close, volume
and return the DataFrame with new indicator columns added.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def add_ema(df: pd.DataFrame, period: int, col: str = "close") -> pd.DataFrame:
    df[f"ema{period}"] = df[col].ewm(span=period, adjust=False).mean()
    return df


def add_rsi(df: pd.DataFrame, period: int = 14, col: str = "close") -> pd.DataFrame:
    delta = df[col].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))
    df["rsi"] = df["rsi"].fillna(50)
    return df


def add_macd(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9, col: str = "close") -> pd.DataFrame:
    ema_fast = df[col].ewm(span=fast, adjust=False).mean()
    ema_slow = df[col].ewm(span=slow, adjust=False).mean()
    df["macd"] = ema_fast - ema_slow
    df["macd_signal"] = df["macd"].ewm(span=signal, adjust=False).mean()
    df["macd_hist"] = df["macd"] - df["macd_signal"]
    return df


def add_vwap(df: pd.DataFrame) -> pd.DataFrame:
    """Session VWAP. Assumes df covers a single trading session (today)."""
    typical_price = (df["high"] + df["low"] + df["close"]) / 3
    cum_vol = df["volume"].cumsum().replace(0, np.nan)
    df["vwap"] = (typical_price * df["volume"]).cumsum() / cum_vol
    df["vwap"] = df["vwap"].fillna(df["close"])
    return df


def add_bollinger(df: pd.DataFrame, period: int = 20, num_std: float = 2.0, col: str = "close") -> pd.DataFrame:
    mid = df[col].rolling(period).mean()
    std = df[col].rolling(period).std()
    df["bb_mid"] = mid
    df["bb_upper"] = mid + num_std * std
    df["bb_lower"] = mid - num_std * std
    return df


def add_atr(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift()).abs()
    low_close = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df["atr"] = tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    df["atr"] = df["atr"].fillna(tr.rolling(period, min_periods=1).mean())
    return df


def add_volume_avg(df: pd.DataFrame, period: int = 20) -> pd.DataFrame:
    df["vol_avg"] = df["volume"].rolling(period, min_periods=1).mean()
    return df


def add_stochastic(df: pd.DataFrame, period: int = 14, smooth: int = 3) -> pd.DataFrame:
    lowest = df["low"].rolling(period, min_periods=period).min()
    highest = df["high"].rolling(period, min_periods=period).max()
    span = (highest - lowest).replace(0, np.nan)
    df["stoch_k"] = (100 * (df["close"] - lowest) / span).fillna(50)
    df["stoch_d"] = df["stoch_k"].rolling(smooth, min_periods=1).mean()
    return df


def add_adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    high = df["high"]
    low = df["low"]
    up = high.diff()
    down = -low.diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    tr = df["atr"] if "atr" in df.columns else (high - low)
    alpha = 1 / period
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=alpha, adjust=False).mean() / tr.replace(0, np.nan)
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=alpha, adjust=False).mean() / tr.replace(0, np.nan)
    dx = (100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)).fillna(0)
    df["plus_di"] = plus_di.fillna(0)
    df["minus_di"] = minus_di.fillna(0)
    df["adx"] = dx.ewm(alpha=alpha, adjust=False).mean().fillna(0)
    return df


def add_supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0) -> pd.DataFrame:
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift()).abs()
    low_close = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean().fillna(tr)
    hl2 = (df["high"] + df["low"]) / 2
    upper = hl2 + multiplier * atr
    lower = hl2 - multiplier * atr
    st = np.zeros(len(df))
    direction = np.ones(len(df))
    for i in range(len(df)):
        if i == 0:
            st[i] = lower.iloc[i]
            continue
        prev_dir = direction[i - 1]
        if prev_dir >= 0:
            st[i] = max(lower.iloc[i], st[i - 1])
            direction[i] = 1 if df["close"].iloc[i] >= st[i] else -1
            if direction[i] < 0:
                st[i] = upper.iloc[i]
        else:
            st[i] = min(upper.iloc[i], st[i - 1])
            direction[i] = -1 if df["close"].iloc[i] <= st[i] else 1
            if direction[i] > 0:
                st[i] = lower.iloc[i]
    df["supertrend"] = st
    df["st_dir"] = direction
    return df


def add_vwap_bands(df: pd.DataFrame) -> pd.DataFrame:
    typical = (df["high"] + df["low"] + df["close"]) / 3
    dev = (typical - df["vwap"]).expanding().std().fillna(0)
    df["vwap_upper"] = df["vwap"] + dev
    df["vwap_lower"] = df["vwap"] - dev
    return df


def add_opening_range(df: pd.DataFrame, minutes: int = 15) -> pd.DataFrame:
    df = df.copy()
    df["orb_high"] = np.nan
    df["orb_low"] = np.nan
    if df.empty:
        return df
    deltas = df.index.to_series().diff().median()
    bar_minutes = 5
    if pd.notna(deltas):
        bar_minutes = max(int(deltas.total_seconds() // 60) or 5, 1)
    n = max(int(round(minutes / bar_minutes)), 1)
    # session date in IST
    dates = df.index.tz_convert("Asia/Kolkata").date if df.index.tz is not None else df.index.date
    orb_h = {}
    orb_l = {}
    for day in pd.unique(dates):
        mask = dates == day
        part = df.loc[mask]
        window = part.iloc[:n]
        orb_h[day] = float(window["high"].max())
        orb_l[day] = float(window["low"].min())
    df["orb_high"] = [orb_h[d] for d in dates]
    df["orb_low"] = [orb_l[d] for d in dates]
    return df


def add_prev_day_levels(df: pd.DataFrame, daily: pd.DataFrame | None) -> pd.DataFrame:
    df = df.copy()
    df["pdh"] = np.nan
    df["pdl"] = np.nan
    df["pdc"] = np.nan
    if daily is None or daily.empty or df.empty:
        return df
    daily = daily.copy()
    d_idx = daily.index.tz_convert("Asia/Kolkata").date if daily.index.tz is not None else pd.Index(daily.index).date
    dates = df.index.tz_convert("Asia/Kolkata").date if df.index.tz is not None else df.index.date
    lookup = {}
    for i, day in enumerate(d_idx):
        if i == 0:
            continue
        prev = daily.iloc[i - 1]
        lookup[day] = (float(prev["high"]), float(prev["low"]), float(prev["close"]))
    df["pdh"] = [lookup[d][0] if d in lookup else np.nan for d in dates]
    df["pdl"] = [lookup[d][1] if d in lookup else np.nan for d in dates]
    df["pdc"] = [lookup[d][2] if d in lookup else np.nan for d in dates]
    return df


def add_htf_bias(df: pd.DataFrame, htf: pd.DataFrame | None) -> pd.DataFrame:
    df = df.copy()
    df["htf_bull"] = 0
    if htf is None or htf.empty or "ema9" not in htf.columns:
        return df
    htf = htf[["ema9", "ema21"]].copy()
    htf["htf_bull"] = np.where(htf["ema9"] > htf["ema21"], 1, -1)
    aligned = htf["htf_bull"].reindex(df.index, method="ffill")
    df["htf_bull"] = aligned.fillna(0)
    return df


def add_index_context(df: pd.DataFrame, index_df: pd.DataFrame | None) -> pd.DataFrame:
    df = df.copy()
    df["mkt_bull"] = 0
    df["rs"] = 0.0
    if index_df is None or index_df.empty:
        return df
    idx = index_df[["close"]].rename(columns={"close": "idx_close"})
    if "vwap" in index_df.columns:
        idx["idx_vwap"] = index_df["vwap"]
    joined = idx.reindex(df.index, method="ffill")
    if "idx_vwap" in joined.columns:
        df["mkt_bull"] = np.where(joined["idx_close"] > joined["idx_vwap"], 1, -1)
    else:
        df["mkt_bull"] = np.where(joined["idx_close"] > joined["idx_close"].ewm(span=21, adjust=False).mean(), 1, -1)
    stock_ret = df["close"].pct_change(10)
    idx_ret = joined["idx_close"].pct_change(10)
    df["rs"] = (stock_ret - idx_ret).fillna(0)
    return df


def compute_all(df: pd.DataFrame) -> pd.DataFrame:
    """Run the full indicator stack on a fresh copy of df."""
    df = df.copy()
    df = add_ema(df, 9)
    df = add_ema(df, 21)
    df = add_ema(df, 50)
    df = add_rsi(df, 14)
    df = add_macd(df)
    df = add_vwap(df)
    df = add_vwap_bands(df)
    df = add_bollinger(df)
    df = add_atr(df, 14)
    df = add_adx(df, 14)
    df = add_stochastic(df, 14)
    df = add_supertrend(df)
    df = add_volume_avg(df, 20)
    df = add_opening_range(df, 15)
    return df


def enrich_context(
    df: pd.DataFrame,
    daily: pd.DataFrame | None = None,
    htf: pd.DataFrame | None = None,
    index_df: pd.DataFrame | None = None,
) -> pd.DataFrame:
    df = add_prev_day_levels(df, daily)
    df = add_htf_bias(df, htf)
    df = add_index_context(df, index_df)
    return df
