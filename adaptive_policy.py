"""Data-driven paper-trading policy.

This is an adaptive rules engine, not a profit predictor. It chooses exposure
from current breadth, signal quality, volatility, and available equity.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

import live_kite
import paper_trader
import universe
from options import OptionContract
from signals import entry_side

DAILY_PROFIT_TARGET = 5_000.0

# Ceiling on concurrent open paper positions, in every regime.
MAX_OPEN_POSITIONS = 5

# ATR band for one intraday candle, not a daily range: a 5-minute NSE large-cap
# bar typically moves 0.10-0.20% of price, so a daily-scale floor rejects the
# whole universe. The ceiling still keeps gapping, news-driven names out.
MIN_ATR_PCT = 0.0005
MAX_ATR_PCT = 0.06

# Scan these underlyings, then buy ATM NFO calls/puts. Watchlist is display-only.
AI_UNIVERSE = list(dict.fromkeys(universe.FNO_UNDERLYINGS))


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
    candidate_contracts: dict[str, OptionContract] = field(default_factory=dict)
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
    contracts: dict[str, OptionContract] | None = None,
) -> AdaptivePlan:
    """Create today's exposure plan from the latest complete scan."""
    contracts = contracts or {}
    equity = float(state.get("cash", 0))
    for symbol, position in state.get("open_positions", {}).items():
        mark = float(prices.get(symbol, position["entry_price"]))
        if position.get("contract"):
            entry = float(position["entry_price"])
            if mark > max(entry * 8, entry + 50):
                mark = entry
            equity += mark * int(position["quantity"])
        elif str(position.get("side", "LONG")).upper() == "SHORT":
            equity += float(position["amount_invested"]) + (
                float(position["entry_price"]) - mark
            ) * int(position["quantity"])
        else:
            equity += mark * int(position["quantity"])

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
    daily_loss_limit = max(DAILY_PROFIT_TARGET, equity * 0.025)
    available_cash = float(state.get("cash", 0))
    if live_kite.is_enabled(state):
        available_cash = min(available_cash, live_kite.remaining_capital(state))
    slots = max(max_positions - len(state.get("open_positions", {})), 0)

    # quality, symbol, side, contract
    ranked: list[tuple[float, str, str, OptionContract]] = []
    for symbol, sig in signals.items():
        if symbol in state.get("open_positions", {}) or symbol not in candles:
            continue
        side = entry_side(sig)
        if side is None or sig.confidence < confidence:
            continue
        if paper_trader._already_exited_this_move(state, symbol, sig):
            continue
        contract = contracts.get(symbol)
        if contract is None:
            continue
        price = float(contract.premium)
        stop = float(contract.stop)
        target = float(contract.target)
        stop_distance = price - stop
        reward = target - price
        if price <= 0 or stop_distance <= 0 or reward / stop_distance < 1.25:
            continue
        if contract.cost_per_lot() <= 0:
            continue

        frame = candles[symbol]
        bar = frame.iloc[-1]
        avg_volume = float(bar.get("vol_avg", 0))
        traded_value = avg_volume * float(sig.price)
        atr = float(bar.get("atr", 0))
        atr_pct = atr / float(sig.price) if sig.price else 0.0
        is_index = symbol in universe.INDEX_UNDERLYINGS
        if atr > 0 and not is_index and not MIN_ATR_PCT <= atr_pct <= MAX_ATR_PCT:
            continue
        if atr > 0 and is_index and atr_pct > MAX_ATR_PCT:
            continue

        quality = sig.confidence + min(reward / stop_distance, 3) * 5 + min(traded_value / 100_000_000, 5)
        if (regime == "RISK-ON" and side == "LONG") or (regime == "RISK-OFF" and side == "SHORT"):
            quality += 8
        ranked.append((quality, symbol, side, contract))

    ranked.sort(reverse=True)
    selected_rows = ranked[:slots]

    today = pd.Timestamp.now(tz="Asia/Kolkata").strftime("%Y-%m-%d")
    realized = _daily_realized(state, today)
    remaining_target = max(DAILY_PROFIT_TARGET - realized, 0.0)
    per_slot_cap = available_cash / slots if slots else 0.0

    required_budgets: dict[str, float] = {}
    if selected_rows:
        if remaining_target > 0:
            profit_share = remaining_target / len(selected_rows)
            for _, symbol, _, contract in selected_rows:
                reward = contract.target - contract.premium
                reward_pct = reward / contract.premium if contract.premium else 0.0
                if reward_pct <= 0:
                    continue
                required_budgets[symbol] = profit_share / reward_pct
        else:
            for _, symbol, _, contract in selected_rows:
                required_budgets[symbol] = per_slot_cap

    budgets: dict[str, float] = {}
    chosen: dict[str, OptionContract] = {}
    projected_profit = 0.0
    cash_left = available_cash
    for _, symbol, _, contract in selected_rows:
        needed = required_budgets.get(symbol, 0.0)
        alloc = min(needed, per_slot_cap, cash_left)
        lot_cost = contract.cost_per_lot()
        lots = math.floor(alloc / lot_cost) if lot_cost > 0 else 0
        if lots < 1:
            continue
        budget = round(lots * lot_cost, 2)
        budgets[symbol] = budget
        chosen[symbol] = contract
        cash_left -= budget
        projected_profit += lots * contract.lot_size * (contract.target - contract.premium)

    sides = {symbol: contract.option_type for symbol, contract in chosen.items()}
    explanation = [
        f"Paper book buys ATM NFO options (LONG→CE, SHORT→PE); no cash shares.",
        f"Breadth: {bull_breadth:.0%} bullish, {bear_breadth:.0%} bearish.",
        f"{regime} requires {confidence}% confidence and allows {max_positions} option positions.",
        f"Deploying up to ₹{sum(budgets.values()):,.0f} of option premium; "
        f"projected target profit ₹{projected_profit:,.0f}.",
        f"Daily loss stop: ₹{daily_loss_limit:,.0f}.",
        f"Today's realized P&L: ₹{realized:+,.2f}; profit objective ₹{DAILY_PROFIT_TARGET:,.0f} "
        f"(guides sizing only — no hard stop).",
    ]
    if live_kite.is_enabled(state):
        explanation.append(
            f"Live Zerodha cap: ₹{live_kite.capital_limit():,.0f} premium max · "
            f"₹{live_kite.remaining_capital(state):,.0f} free now."
        )
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
        candidate_contracts=chosen,
        selected=[
            f"{contract.tradingsymbol}"
            for symbol, contract in chosen.items()
        ],
        explanation=explanation,
    )
