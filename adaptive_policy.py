"""Data-driven paper-trading policy.

This is an adaptive rules engine, not a profit predictor. It chooses exposure
from current breadth, signal quality, volatility, and available equity.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

import universe
from signals import entry_side

DAILY_PROFIT_TARGET = 5_000.0

# Ceiling on concurrent open paper positions, in every regime.
MAX_OPEN_POSITIONS = 5

# ATR band for one intraday candle, not a daily range: a 5-minute NSE large-cap
# bar typically moves 0.10-0.20% of price, so a daily-scale floor rejects the
# whole universe. The ceiling still keeps gapping, news-driven names out.
MIN_ATR_PCT = 0.0005
MAX_ATR_PCT = 0.06

# Broad but still liquid cash-market universe. The user's display watchlist is
# deliberately not involved in this decision.
AI_UNIVERSE = sorted(set(universe.NIFTY_50) | set(universe.HIGH_BETA))


@dataclass
class AdaptivePlan:
    regime: str
    confidence_required: int
    max_positions: int
    risk_per_trade: float
    daily_profit_target: float
    daily_loss_limit: float
    projected_profit: float = 0.0
    candidate_budgets: dict[str, float] = field(default_factory=dict)
    candidate_sides: dict[str, str] = field(default_factory=dict)
    selected: list[str] = field(default_factory=list)
    explanation: list[str] = field(default_factory=list)


def _daily_realized(state: dict[str, Any], today: str) -> float:
    total = 0.0
    for trade in state.get("trades", []):
        exit_time = trade.get("exit_time")
        if exit_time and pd.Timestamp(exit_time).tz_convert("Asia/Kolkata").strftime("%Y-%m-%d") == today:
            total += float(trade.get("pnl", 0))
    return total


def decide(
    state: dict[str, Any],
    signals: dict[str, Any],
    candles: dict[str, pd.DataFrame],
    prices: dict[str, float],
) -> AdaptivePlan:
    """Create today's exposure plan from the latest complete scan."""
    equity = float(state.get("cash", 0))
    for symbol, position in state.get("open_positions", {}).items():
        equity += float(prices.get(symbol, position["entry_price"])) * int(position["quantity"])

    usable = [s for s in signals.values() if s.symbol in candles]
    bullish = sum(s.score >= 3 for s in usable)
    bearish = sum(s.score <= -3 for s in usable)
    count = max(len(usable), 1)
    bull_breadth = bullish / count
    bear_breadth = bearish / count
    strong = sum(abs(s.score) >= 6 for s in usable) / count

    if bull_breadth >= 0.45 and bull_breadth - bear_breadth >= 0.15:
        regime = "RISK-ON"
        confidence = 55 if strong >= 0.30 else 60
        max_positions = MAX_OPEN_POSITIONS
        risk_fraction = 0.006
    elif bear_breadth >= 0.45:
        regime = "RISK-OFF"
        confidence = 65
        max_positions = MAX_OPEN_POSITIONS
        risk_fraction = 0.003
    else:
        regime = "MIXED"
        confidence = 60
        max_positions = MAX_OPEN_POSITIONS
        risk_fraction = 0.004

    risk_per_trade = max(100.0, equity * risk_fraction)
    daily_loss_limit = max(1_000.0, equity * 0.015)
    available_cash = float(state.get("cash", 0))
    slots = max(max_positions - len(state.get("open_positions", {})), 0)

    # quality, symbol, side, price, stop distance, reward per share
    ranked: list[tuple[float, str, str, float, float, float]] = []
    for symbol, sig in signals.items():
        if symbol in state.get("open_positions", {}) or symbol not in candles:
            continue
        side = entry_side(sig)
        if side is None or sig.confidence < confidence:
            continue
        price = float(sig.price)
        stop = float(sig.stop_loss or price)
        target = float(sig.target or price)
        stop_distance = price - stop if side == "LONG" else stop - price
        reward = target - price if side == "LONG" else price - target
        if price <= 0 or stop_distance <= 0 or reward / stop_distance < 1.25:
            continue

        frame = candles[symbol]
        bar = frame.iloc[-1]
        avg_volume = float(bar.get("vol_avg", 0))
        traded_value = avg_volume * price
        # Avoid thin names and extremely volatile entries.
        atr = float(bar.get("atr", 0))
        atr_pct = atr / price
        if traded_value < 5_000_000 or not MIN_ATR_PCT <= atr_pct <= MAX_ATR_PCT:
            continue
        # A stop that has trailed up to the current price is an instant
        # stop-out for anyone opening the trade now.
        if stop_distance < 0.25 * atr:
            continue

        quality = sig.confidence + min(reward / stop_distance, 3) * 5 + min(traded_value / 100_000_000, 5)
        # Prefer the side aligned with the breadth regime, but both are valid.
        if (regime == "RISK-ON" and side == "LONG") or (regime == "RISK-OFF" and side == "SHORT"):
            quality += 8
        ranked.append((quality, symbol, side, price, stop_distance, reward))

    ranked.sort(reverse=True)
    selected_rows = ranked[:slots]

    today = pd.Timestamp.now(tz="Asia/Kolkata").strftime("%Y-%m-%d")
    realized = _daily_realized(state, today)
    remaining_target = max(DAILY_PROFIT_TARGET - realized, 0.0)

    # Size from the requested output first. If the capital needed to earn the
    # remaining target at each setup's stated target exceeds available cash,
    # deploy all available paper cash across the best setups. This deliberately
    # removes the old 8-12% concentration cap; stop-losses and the daily loss
    # limit remain the downside controls.
    required_budgets: dict[str, float] = {}
    if selected_rows:
        profit_share = remaining_target / len(selected_rows)
        for _, symbol, _, price, _, reward in selected_rows:
            reward_pct = reward / price
            required_budgets[symbol] = profit_share / reward_pct

    # Reserve capital for the slots that are still free, so a setup firing now
    # cannot swallow the cash that later setups need. With five free slots any
    # single entry takes at most a fifth of the cash, which is what keeps the
    # position cap meaningful instead of ending up concentrated in one name.
    per_slot_cap = available_cash / slots if slots else 0.0

    budgets: dict[str, float] = {}
    projected_profit = 0.0
    cash_left = available_cash
    for _, symbol, _, price, _, reward in selected_rows:
        alloc = min(required_budgets[symbol], per_slot_cap, cash_left)
        qty = math.floor(alloc / price)
        if qty < 1:
            continue
        budget = round(qty * price, 2)
        budgets[symbol] = budget
        cash_left -= budget
        projected_profit += qty * reward

    sides = {symbol: side for _, symbol, side, _, _, _ in selected_rows if symbol in budgets}
    explanation = [
        f"Breadth: {bull_breadth:.0%} bullish, {bear_breadth:.0%} bearish.",
        f"{regime} requires {confidence}% confidence and allows {max_positions} positions.",
        f"Deploying up to ₹{sum(budgets.values()):,.0f} toward selected targets; "
        f"projected target profit ₹{projected_profit:,.0f}.",
        f"Daily loss stop: ₹{daily_loss_limit:,.0f}.",
        f"Today's realized P&L: ₹{realized:+,.2f}; profit objective: ₹{DAILY_PROFIT_TARGET:,.0f}.",
    ]
    return AdaptivePlan(
        regime=regime,
        confidence_required=confidence,
        max_positions=max_positions,
        risk_per_trade=round(risk_per_trade, 2),
        daily_profit_target=DAILY_PROFIT_TARGET,
        daily_loss_limit=round(daily_loss_limit, 2),
        projected_profit=round(projected_profit, 2),
        candidate_budgets=budgets,
        candidate_sides=sides,
        selected=[f"{symbol} {side}" for _, symbol, side, _, _, _ in selected_rows if symbol in budgets],
        explanation=explanation,
    )
