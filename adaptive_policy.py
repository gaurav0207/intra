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

DAILY_PROFIT_TARGET = 5_000.0

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
    candidate_budgets: dict[str, float] = field(default_factory=dict)
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
        confidence = 60 if strong >= 0.30 else 65
        max_positions = 5
        risk_fraction = 0.006
    elif bear_breadth >= 0.45:
        regime = "RISK-OFF"
        confidence = 80
        max_positions = 1
        risk_fraction = 0.0025
    else:
        regime = "MIXED"
        confidence = 70
        max_positions = 3
        risk_fraction = 0.004

    risk_per_trade = max(100.0, equity * risk_fraction)
    daily_loss_limit = max(1_000.0, equity * 0.015)
    available_cash = float(state.get("cash", 0))
    slots = max(max_positions - len(state.get("open_positions", {})), 0)

    ranked: list[tuple[float, str, float]] = []
    for symbol, sig in signals.items():
        if symbol in state.get("open_positions", {}) or symbol not in candles:
            continue
        if sig.what_to_do != "ENTER LONG NOW" or sig.confidence < confidence:
            continue
        price = float(sig.price)
        stop = float(sig.stop_loss or price)
        target = float(sig.target or price)
        stop_distance = price - stop
        reward = target - price
        if price <= 0 or stop_distance <= 0 or reward / stop_distance < 1.25:
            continue

        frame = candles[symbol]
        bar = frame.iloc[-1]
        avg_volume = float(bar.get("vol_avg", 0))
        traded_value = avg_volume * price
        # Avoid thin names and extremely volatile entries.
        atr_pct = float(bar.get("atr", 0)) / price
        if traded_value < 5_000_000 or not 0.002 <= atr_pct <= 0.06:
            continue

        qty_by_risk = math.floor(risk_per_trade / stop_distance)
        concentration_cap = equity * (0.12 if regime == "RISK-ON" else 0.08)
        qty_by_cap = math.floor(min(concentration_cap, available_cash) / price)
        qty = min(qty_by_risk, qty_by_cap)
        if qty < 1:
            continue
        budget = round(qty * price, 2)
        quality = sig.confidence + min(reward / stop_distance, 3) * 5 + min(traded_value / 100_000_000, 5)
        ranked.append((quality, symbol, budget))

    ranked.sort(reverse=True)
    selected_rows = ranked[:slots]
    budgets = {symbol: budget for _, symbol, budget in selected_rows}

    today = pd.Timestamp.now(tz="Asia/Kolkata").strftime("%Y-%m-%d")
    realized = _daily_realized(state, today)
    explanation = [
        f"Breadth: {bull_breadth:.0%} bullish, {bear_breadth:.0%} bearish.",
        f"{regime} requires {confidence}% confidence and allows {max_positions} positions.",
        f"Risk budget per trade: ₹{risk_per_trade:,.0f}; daily loss stop: ₹{daily_loss_limit:,.0f}.",
        f"Today's realized P&L: ₹{realized:+,.2f}; profit objective: ₹{DAILY_PROFIT_TARGET:,.0f}.",
    ]
    return AdaptivePlan(
        regime=regime,
        confidence_required=confidence,
        max_positions=max_positions,
        risk_per_trade=round(risk_per_trade, 2),
        daily_profit_target=DAILY_PROFIT_TARGET,
        daily_loss_limit=round(daily_loss_limit, 2),
        candidate_budgets=budgets,
        selected=[symbol for _, symbol, _ in selected_rows],
        explanation=explanation,
    )
