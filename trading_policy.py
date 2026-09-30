"""Shared intraday rules — paper book and Zerodha mirroring use the same policy."""

from __future__ import annotations

import math
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

IST = timezone(timedelta(hours=5, minutes=30))

_INDEX_SYMBOLS = ("NIFTY 50", "NIFTY", "NIFTY50")


def _env_int(name: str, default: int, *fallback_env: str) -> int:
    for key in (name, *fallback_env):
        raw = os.getenv(key, "").strip()
        if not raw:
            continue
        try:
            return int(raw)
        except ValueError:
            continue
    return default


def _env_float(name: str, default: float, *fallback_env: str) -> float:
    for key in (name, *fallback_env):
        raw = os.getenv(key, "").strip()
        if not raw:
            continue
        try:
            return float(raw)
        except ValueError:
            continue
    return default


def min_confidence() -> int:
    return max(0, min(100, _env_int("TRADER_MIN_CONFIDENCE", 70, "LIVE_MIN_CONFIDENCE")))


def max_lots_per_trade() -> int:
    return max(1, _env_int("TRADER_MAX_LOTS", 1, "LIVE_MAX_LOTS"))


def max_deploy_fraction() -> float:
    return max(0.05, min(1.0, _env_float("TRADER_MAX_DEPLOY_FRACTION", 0.30, "LIVE_MAX_DEPLOY_FRACTION")))


def max_open_positions() -> int:
    return max(1, _env_int("TRADER_MAX_OPEN_POSITIONS", 5, "LIVE_MAX_OPEN_POSITIONS"))


def option_reversal_score() -> int:
    """Match signals.py short/long exit scores (±2) unless overridden."""
    return max(1, _env_int("OPTION_REVERSAL_SCORE", 2))


def option_stop_fraction() -> float:
    """Exit a long option once premium is this far below the fill (default −8%).

    The underlying ATR stop was closing trades after a few minutes on a small
    stock wiggle. The option's own premium is the loss that matters.
    """
    return max(0.03, min(0.40, _env_float("OPTION_STOP_FRACTION", 0.08)))


def option_take_profit_fraction() -> float:
    """Book the option once premium is this far above entry (default +12%)."""
    return max(0.03, min(1.0, _env_float("OPTION_TAKE_PROFIT_FRACTION", 0.12)))


def option_breakeven_arm_fraction() -> float:
    """After this gain, raise the premium stop to entry so a winner cannot become a full stop-loss."""
    arm = max(0.02, min(0.5, _env_float("OPTION_BREAKEVEN_ARM_FRACTION", 0.06)))
    return min(arm, option_take_profit_fraction())


def daily_loss_cap() -> float | None:
    raw = os.getenv("TRADER_DAILY_LOSS_LIMIT", os.getenv("LIVE_DAILY_LOSS_LIMIT", "5000")).strip()
    if not raw:
        return None
    try:
        return max(100.0, float(raw))
    except ValueError:
        return 5000.0


def kite_fetch_entry_cooldown_minutes() -> int:
    return max(0, _env_int("KITE_FETCH_ENTRY_COOLDOWN_MINUTES", 30))


def apply_confidence_floor(regime_confidence: int) -> int:
    return max(int(regime_confidence), min_confidence())


def apply_daily_loss_limit(computed: float) -> float:
    cap = daily_loss_cap()
    if cap is None:
        return float(computed)
    return min(float(computed), cap)


def slot_deploy_fraction(paper_default: float) -> float:
    return min(float(paper_default), max_deploy_fraction())


def cap_lots(lots: int) -> int:
    return min(int(lots), max_lots_per_trade())


def block_pe_above_nifty_vwap() -> bool:
    raw = os.getenv("TRADER_BLOCK_PE_ABOVE_NIFTY_VWAP", "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def index_above_vwap(candles: dict[str, pd.DataFrame]) -> bool | None:
    """True when the index last closed above session VWAP (risk-on tape)."""
    for sym in _INDEX_SYMBOLS:
        frame = candles.get(sym)
        if frame is None or len(frame) < 1:
            continue
        last = frame.iloc[-1]
        vwap = last.get("vwap")
        if vwap is None or pd.isna(vwap):
            continue
        return float(last["close"]) > float(vwap)
    return None


def block_new_pe_reason(candles: dict[str, pd.DataFrame]) -> str | None:
    if not block_pe_above_nifty_vwap():
        return None
    above = index_above_vwap(candles)
    if above is True:
        return "Nifty is above VWAP (risk-on tape) — no new PE entries"
    return None


def entry_block_reason(state: dict[str, Any] | None) -> str | None:
    if not state or not state.get("kite_fetch_alerted"):
        return None
    minutes = kite_fetch_entry_cooldown_minutes()
    if minutes <= 0:
        return None
    last_at = str(state.get("kite_fetch_alerted_at") or "")
    if not last_at:
        return f"Zerodha fetch failed — no new entries for {minutes}m"
    try:
        alerted = datetime.fromisoformat(last_at)
        if alerted.tzinfo is None:
            alerted = alerted.replace(tzinfo=IST)
        else:
            alerted = alerted.astimezone(IST)
        elapsed = (datetime.now(IST) - alerted).total_seconds() / 60
        if elapsed >= minutes:
            return None
        left = max(1, int(math.ceil(minutes - elapsed)))
        reason = str(state.get("kite_fetch_reason") or "fetch error")
        return f"Zerodha fetch failed ({reason}) — no new entries for ~{left}m"
    except (TypeError, ValueError):
        return f"Zerodha fetch failed — no new entries for {minutes}m"


def policy_summary() -> str:
    return (
        f"Policy: conf ≥{min_confidence()}%, "
        f"≤{max_lots_per_trade()} lot/trade, "
        f"≤{max_deploy_fraction():.0%} cash/slot, "
        f"≤{max_open_positions()} open, "
        f"loss stop ₹{daily_loss_cap() or 0:,.0f}, "
        f"take profit +{option_take_profit_fraction():.0%} premium, "
        f"premium stop −{option_stop_fraction():.0%}, "
        f"option reversal ±{option_reversal_score()}."
    )
