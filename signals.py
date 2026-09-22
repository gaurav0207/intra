"""
Rule-based intraday signal engine.

This is a transparent, explainable scoring system -- NOT a black box,
and NOT a guarantee of profit. Every rule is visible and editable below.
It combines trend (VWAP, EMA crossover), momentum (RSI, MACD) and
volume confirmation into a single score, then walks today's candles
to decide when to enter and when to get out.
"""

from dataclasses import dataclass, field
from datetime import time as dtime
from typing import List, Optional

import pandas as pd

from data import next_session_label
from indicators import enrich_context

# Fraction of the entry-to-target move that may already be gone before opening
# a held setup counts as chasing rather than entering.
MAX_HOLD_ENTRY_PROGRESS = 0.5


@dataclass
class Signal:
    symbol: str
    action: str          # STRONG BUY / BUY / HOLD / SELL / STRONG SELL
    score: int
    price: float
    reasons: List[str]
    stop_loss: Optional[float]
    target: Optional[float]
    rsi: float
    vwap: float
    what_to_do: str = "WAIT"
    side: str = "FLAT"           # LONG / SHORT / FLAT
    entry: Optional[float] = None
    entered_at: Optional[str] = None
    exit_reason: Optional[str] = None
    in_trade: bool = False
    playbook: List[str] = field(default_factory=list)
    tomorrow_bias: str = "—"
    tomorrow_plan: str = ""
    tomorrow_reasons: List[str] = field(default_factory=list)
    tomorrow_entry: Optional[float] = None
    tomorrow_stop: Optional[float] = None
    tomorrow_target: Optional[float] = None
    tomorrow_invalid: Optional[float] = None
    tomorrow_session: str = ""
    confidence: int = 0
    data_source: str = "yahoo"


def entry_side(sig: "Signal") -> Optional[str]:
    """Return LONG/SHORT if this signal may be opened now, else None.

    ``ENTER ... NOW`` prints only on the single bar where the setup fires, so a
    scan landing one bar later would skip the whole move. A HOLD that still has
    most of its move left is therefore entry-eligible too, measured as progress
    from the entry price towards the target rather than by clock time.
    """
    action = sig.what_to_do
    if action in {"ENTER LONG NOW", "ENTER SHORT NOW"}:
        return "SHORT" if "SHORT" in action else "LONG"
    if action not in {"HOLD LONG", "HOLD SHORT"}:
        return None
    if sig.entry is None or sig.target is None:
        return None
    side = "SHORT" if "SHORT" in action else "LONG"
    entry, target, price = float(sig.entry), float(sig.target), float(sig.price)
    span = target - entry if side == "LONG" else entry - target
    captured = price - entry if side == "LONG" else entry - price
    if span <= 0 or captured / span > MAX_HOLD_ENTRY_PROGRESS:
        return None
    return side


def _has(row: pd.Series, col: str) -> bool:
    return col in row.index and pd.notna(row[col])


def _score_row(prev: pd.Series, latest: pd.Series) -> tuple[int, List[str]]:
    score = 0
    reasons = []

    # 1. Trend vs VWAP
    if latest["close"] > latest["vwap"]:
        score += 1
        reasons.append("Price is above VWAP (intraday uptrend)")
    else:
        score -= 1
        reasons.append("Price is below VWAP (intraday downtrend)")

    # 2. EMA9/EMA21 crossover
    if latest["ema9"] > latest["ema21"] and prev["ema9"] <= prev["ema21"]:
        score += 2
        reasons.append("EMA9 just crossed above EMA21 (fresh bullish crossover)")
    elif latest["ema9"] > latest["ema21"]:
        score += 1
        reasons.append("EMA9 above EMA21 (short-term uptrend)")
    elif latest["ema9"] < latest["ema21"] and prev["ema9"] >= prev["ema21"]:
        score -= 2
        reasons.append("EMA9 just crossed below EMA21 (fresh bearish crossover)")
    else:
        score -= 1
        reasons.append("EMA9 below EMA21 (short-term downtrend)")

    # 3. RSI
    rsi = latest["rsi"]
    if rsi < 30:
        score += 1
        reasons.append(f"RSI {rsi:.0f} is oversold (possible bounce)")
    elif rsi > 70:
        score -= 1
        reasons.append(f"RSI {rsi:.0f} is overbought (possible pullback)")

    # 4. MACD
    if latest["macd"] > latest["macd_signal"]:
        score += 1
        reasons.append("MACD above signal line (bullish momentum)")
    else:
        score -= 1
        reasons.append("MACD below signal line (bearish momentum)")

    # 5. Volume
    if latest["vol_avg"] > 0 and latest["volume"] > 1.5 * latest["vol_avg"]:
        if score > 0:
            score += 1
            reasons.append("Volume spike confirms the up-move")
        elif score < 0:
            score -= 1
            reasons.append("Volume spike confirms the down-move")

    # 6. Bollinger — mean reversion at bands, trend if riding mid
    if _has(latest, "bb_lower") and latest["close"] <= latest["bb_lower"]:
        score += 1
        reasons.append("Close at/under lower Bollinger (stretch down)")
    elif _has(latest, "bb_upper") and latest["close"] >= latest["bb_upper"]:
        score -= 1
        reasons.append("Close at/over upper Bollinger (stretch up)")

    # 7. Supertrend
    if _has(latest, "st_dir"):
        if latest["st_dir"] > 0:
            score += 1
            reasons.append("Supertrend is bullish")
        else:
            score -= 1
            reasons.append("Supertrend is bearish")

    # 8. ADX — only add when the market is actually trending
    if _has(latest, "adx") and latest["adx"] >= 22:
        if _has(latest, "plus_di") and latest["plus_di"] > latest["minus_di"]:
            score += 1
            reasons.append(f"ADX {latest['adx']:.0f} trending up (+DI > −DI)")
        else:
            score -= 1
            reasons.append(f"ADX {latest['adx']:.0f} trending down (−DI > +DI)")
    elif _has(latest, "adx") and latest["adx"] < 16:
        reasons.append(f"ADX {latest['adx']:.0f} — choppy, treat signals as weaker")

    # 9. Stochastic turn
    if _has(latest, "stoch_k") and _has(prev, "stoch_k"):
        k, d = latest["stoch_k"], latest["stoch_d"]
        pk = prev["stoch_k"]
        if k < 20 and k > d and pk <= prev["stoch_d"]:
            score += 1
            reasons.append("Stochastic turning up from oversold")
        elif k > 80 and k < d and pk >= prev["stoch_d"]:
            score -= 1
            reasons.append("Stochastic turning down from overbought")

    # 10. Opening-range breakout (after first 15m)
    if _has(latest, "orb_high") and _has(latest, "orb_low"):
        t = None
        try:
            t = _bar_time(latest.name)
        except Exception:
            t = None
        if t is None or t >= dtime(9, 30):
            if latest["close"] > latest["orb_high"]:
                score += 1
                reasons.append("Holding above the 15m opening range high")
            elif latest["close"] < latest["orb_low"]:
                score -= 1
                reasons.append("Holding below the 15m opening range low")

    # 11. Prior day high / low
    if _has(latest, "pdh") and latest["close"] > latest["pdh"]:
        score += 1
        reasons.append("Above prior-day high (range expansion)")
    elif _has(latest, "pdl") and latest["close"] < latest["pdl"]:
        score -= 1
        reasons.append("Below prior-day low (range expansion down)")

    # 12. Gap vs prior close
    if _has(latest, "pdc") and _has(latest, "open"):
        gap = (latest["open"] - latest["pdc"]) / latest["pdc"]
        if gap > 0.006 and latest["close"] > latest["open"]:
            score += 1
            reasons.append("Gap-up holding (open above prior close)")
        elif gap < -0.006 and latest["close"] < latest["open"]:
            score -= 1
            reasons.append("Gap-down holding")

    # 13. Bullish/bearish engulfing
    body = latest["close"] - latest["open"]
    prev_body = prev["close"] - prev["open"]
    if body > 0 and prev_body < 0 and latest["close"] >= prev["open"] and latest["open"] <= prev["close"]:
        score += 1
        reasons.append("Bullish engulfing candle")
    elif body < 0 and prev_body > 0 and latest["close"] <= prev["open"] and latest["open"] >= prev["close"]:
        score -= 1
        reasons.append("Bearish engulfing candle")

    # 14. Higher-timeframe EMA bias
    if _has(latest, "htf_bull"):
        if latest["htf_bull"] > 0:
            score += 1
            reasons.append("15m trend agrees (EMA9 > EMA21)")
        elif latest["htf_bull"] < 0:
            score -= 1
            reasons.append("15m trend disagrees (EMA9 < EMA21)")

    # 15. Nifty / market filter
    if _has(latest, "mkt_bull"):
        if latest["mkt_bull"] > 0:
            score += 1
            reasons.append("Nifty is above VWAP (risk-on tape)")
        else:
            score -= 1
            reasons.append("Nifty is below VWAP (risk-off tape)")

    # 16. Relative strength vs Nifty
    if _has(latest, "rs"):
        if latest["rs"] > 0.002:
            score += 1
            reasons.append("Stock is outperforming Nifty on this move")
        elif latest["rs"] < -0.002:
            score -= 1
            reasons.append("Stock is underperforming Nifty on this move")

    return score, reasons


def _action_from_score(score: int) -> str:
    if score >= 6:
        return "STRONG BUY"
    if score >= 3:
        return "BUY"
    if score <= -6:
        return "STRONG SELL"
    if score <= -3:
        return "SELL"
    return "HOLD"


def _bar_time(ts) -> dtime:
    ts = pd.Timestamp(ts)
    if ts.tzinfo is not None:
        ts = ts.tz_convert("Asia/Kolkata")
    return ts.time()


def _levels(price: float, atr: float, side: str, stop_mult: float, target_mult: float) -> tuple[float, float]:
    if side == "LONG":
        return round(price - stop_mult * atr, 2), round(price + target_mult * atr, 2)
    return round(price + stop_mult * atr, 2), round(price - target_mult * atr, 2)


def generate_signal(
    symbol: str,
    df: pd.DataFrame,
    atr_stop_mult: float = 1.5,
    atr_target_mult: float = 2.5,
    long_only: bool = True,
    daily: Optional[pd.DataFrame] = None,
    htf: Optional[pd.DataFrame] = None,
    index_df: Optional[pd.DataFrame] = None,
) -> Optional[Signal]:
    """Walk today's candles and return enter / hold / exit instructions.

    df must already have indicators computed (see indicators.compute_all).
    """
    if df is None or len(df) < 25:
        return None

    source = df.attrs.get("source", "yahoo")
    df = enrich_context(df, daily=daily, htf=htf, index_df=index_df)
    df.attrs["source"] = source

    no_entry_after = dtime(15, 0)
    no_entry_before = dtime(9, 30)
    square_off = dtime(15, 15)
    enter_long_at = 4
    enter_short_at = -4
    exit_long_score = -2
    exit_short_score = 2

    position = None  # dict: side, entry, stop, target, entered_at, entry_idx
    last_event = None  # "entered" | "exited" on the latest bar
    last_exit_reason = None
    prev_score = 0

    start = 21
    for i in range(start, len(df)):
        prev = df.iloc[i - 1]
        latest = df.iloc[i]
        score, _ = _score_row(prev, latest)
        bar_t = _bar_time(df.index[i])
        close = float(latest["close"])
        high = float(latest["high"])
        low = float(latest["low"])
        atr = float(latest["atr"]) if pd.notna(latest["atr"]) else None
        is_last = i == len(df) - 1
        event = None
        exit_reason = None

        if position is not None and atr:
            side = position["side"]
            hit_stop = (side == "LONG" and low <= position["stop"]) or (
                side == "SHORT" and high >= position["stop"]
            )
            hit_target = (side == "LONG" and high >= position["target"]) or (
                side == "SHORT" and low <= position["target"]
            )
            reversed_ = (side == "LONG" and score <= exit_long_score) or (
                side == "SHORT" and score >= exit_short_score
            )
            time_exit = bar_t >= square_off

            if hit_stop:
                event = "exited"
                exit_reason = f"Stop-loss hit at {position['stop']}"
            elif hit_target:
                event = "exited"
                exit_reason = f"Target hit at {position['target']}"
            elif reversed_:
                event = "exited"
                exit_reason = f"Signal reversed (score {score}) — close the trade"
            elif time_exit:
                event = "exited"
                exit_reason = "Square off before close (after 15:15 IST)"
            else:
                # Trail stop in the direction of the trade; never loosen it.
                trail_stop, _ = _levels(close, atr, side, atr_stop_mult, atr_target_mult)
                if side == "LONG":
                    position["stop"] = max(position["stop"], trail_stop)
                else:
                    position["stop"] = min(position["stop"], trail_stop)

            if event == "exited":
                position = None

        if position is None and event != "exited" and atr and atr > 0 and no_entry_before <= bar_t < no_entry_after:
            choppy = _has(latest, "adx") and latest["adx"] < 16
            need = enter_long_at + (2 if choppy else 0)
            fresh_long = score >= need and prev_score < need
            late_long = (
                score >= 6
                and abs(close - float(latest["ema9"])) <= 0.35 * atr
                and not choppy
            )
            mkt_ok = not _has(latest, "mkt_bull") or latest["mkt_bull"] >= 0 or score >= 7
            fresh_short = (not long_only) and score <= -need and prev_score > -need

            side = None
            if (fresh_long or late_long) and mkt_ok:
                side = "LONG"
            elif fresh_short:
                side = "SHORT"

            if side:
                stop, target = _levels(close, atr, side, atr_stop_mult, atr_target_mult)
                position = {
                    "side": side,
                    "entry": round(close, 2),
                    "stop": stop,
                    "target": target,
                    "entered_at": pd.Timestamp(df.index[i]).strftime("%H:%M"),
                    "entry_idx": i,
                }
                event = "entered"

        if is_last:
            last_event = event
            last_exit_reason = exit_reason

        prev_score = score

    latest = df.iloc[-1]
    prev = df.iloc[-2]
    score, reasons = _score_row(prev, latest)
    action = _action_from_score(score)
    price = float(latest["close"])
    atr = float(latest["atr"]) if pd.notna(latest["atr"]) else None

    stop_loss = target = None
    if atr:
        if "BUY" in action:
            stop_loss, target = _levels(price, atr, "LONG", atr_stop_mult, atr_target_mult)
        elif "SELL" in action and not long_only:
            stop_loss, target = _levels(price, atr, "SHORT", atr_stop_mult, atr_target_mult)

    playbook: List[str] = []
    exit_reason = last_exit_reason
    in_trade = position is not None
    side = position["side"] if position else "FLAT"
    entry = position["entry"] if position else None
    entered_at = position["entered_at"] if position else None

    if position:
        stop_loss = position["stop"]
        target = position["target"]

    bar_t = _bar_time(df.index[-1])

    if last_event == "entered" and position:
        what_to_do = f"ENTER {position['side']} NOW"
        playbook = [
            f"Enter {position['side'].lower()} now around {price:.2f}.",
            f"Place stop-loss at {stop_loss}.",
            f"Take profit at {target}.",
            "If the next refresh flips to EXIT, close immediately — do not wait for the target.",
        ]
    elif last_event == "exited":
        what_to_do = "EXIT NOW"
        playbook = [
            last_exit_reason or "Exit rule triggered on the latest candle.",
            "Go flat. Do not re-enter until a fresh setup appears.",
        ]
        if "BUY" in action and bar_t < no_entry_after:
            playbook.append("Bias is still buy-ish, but wait for a new entry signal — do not chase this exit.")
    elif position:
        what_to_do = f"HOLD {position['side']}"
        playbook = [
            f"Stay in the {position['side'].lower()} from {entry} ({entered_at} IST).",
            f"Get out if price hits stop {stop_loss} (exit).",
            f"Get out if price hits target {target} (take profit).",
            "Get out if the signal reverses, or at 15:15 IST (intraday square-off).",
            "Stop trails with ATR as price moves in your favour.",
        ]
    elif bar_t >= no_entry_after:
        what_to_do = "NO NEW ENTRIES"
        playbook = [
            "Past 15:00 IST — do not open a new intraday trade.",
            "If you are still in a position from earlier, square off by 15:15 IST.",
        ]
    elif "BUY" in action and long_only:
        what_to_do = "WAIT — no entry yet"
        playbook = [
            "Bias is bullish, but there is no fresh entry this candle.",
            "Enter only on a new score push to +2 or a strong buy that is still hugging EMA9.",
            "Do not chase a move that already ran without an entry mark.",
        ]
    elif "SELL" in action:
        what_to_do = "STAY OUT" if long_only else "WAIT — no short yet"
        playbook = [
            "Bias is bearish." + (" Cash stocks: do not buy." if long_only else ""),
            "Wait for a fresh short setup." if not long_only else "Wait for the next long setup.",
        ]
    else:
        what_to_do = "WAIT"
        playbook = ["No setup. Stay flat until a fresh BUY (or short, if enabled) prints."]

    conf = min(100, int(round(100 * abs(score) / 12)))

    return Signal(
        symbol=symbol,
        action=action,
        score=score,
        price=round(price, 2),
        reasons=reasons,
        stop_loss=stop_loss,
        target=target,
        rsi=round(float(latest["rsi"]), 1),
        vwap=round(float(latest["vwap"]), 2),
        what_to_do=what_to_do,
        side=side,
        entry=entry,
        entered_at=entered_at,
        exit_reason=exit_reason,
        in_trade=in_trade,
        playbook=playbook,
        confidence=conf,
        data_source=source,
    )


def _score_daily(prev: pd.Series, latest: pd.Series) -> tuple[int, List[str]]:
    """Daily trend score for the next session. Uses EMA21 as the trend line (not session VWAP)."""
    score = 0
    reasons: List[str] = []

    if latest["close"] > latest["ema21"]:
        score += 1
        reasons.append("Daily close is above EMA21 (uptrend into tomorrow)")
    else:
        score -= 1
        reasons.append("Daily close is below EMA21 (downtrend into tomorrow)")

    if latest["ema9"] > latest["ema21"] and prev["ema9"] <= prev["ema21"]:
        score += 2
        reasons.append("Daily EMA9 just crossed above EMA21")
    elif latest["ema9"] > latest["ema21"]:
        score += 1
        reasons.append("Daily EMA9 is above EMA21")
    elif latest["ema9"] < latest["ema21"] and prev["ema9"] >= prev["ema21"]:
        score -= 2
        reasons.append("Daily EMA9 just crossed below EMA21")
    else:
        score -= 1
        reasons.append("Daily EMA9 is below EMA21")

    if latest["close"] > latest["ema50"]:
        score += 1
        reasons.append("Price is above EMA50 (broader daily trend is up)")
    else:
        score -= 1
        reasons.append("Price is below EMA50 (broader daily trend is down)")

    rsi = latest["rsi"]
    if rsi < 30:
        score += 1
        reasons.append(f"Daily RSI {rsi:.0f} is oversold — bounce possible tomorrow")
    elif rsi > 70:
        score -= 1
        reasons.append(f"Daily RSI {rsi:.0f} is overbought — pullback possible tomorrow")

    if latest["macd"] > latest["macd_signal"]:
        score += 1
        reasons.append("Daily MACD is bullish")
    else:
        score -= 1
        reasons.append("Daily MACD is bearish")

    if latest["vol_avg"] > 0 and latest["volume"] > 1.5 * latest["vol_avg"]:
        if score > 0:
            score += 1
            reasons.append("High volume confirms the daily up-move")
        elif score < 0:
            score -= 1
            reasons.append("High volume confirms the daily down-move")

    if latest["close"] > prev["high"]:
        score += 1
        reasons.append("Closed above the prior day's high (breakout)")
    elif latest["close"] < prev["low"]:
        score -= 1
        reasons.append("Closed below the prior day's low (breakdown)")

    if _has(latest, "st_dir"):
        if latest["st_dir"] > 0:
            score += 1
            reasons.append("Daily Supertrend is bullish")
        else:
            score -= 1
            reasons.append("Daily Supertrend is bearish")

    if _has(latest, "adx") and latest["adx"] >= 20:
        if latest["plus_di"] > latest["minus_di"]:
            score += 1
            reasons.append(f"Daily ADX {latest['adx']:.0f} confirms an uptrend")
        else:
            score -= 1
            reasons.append(f"Daily ADX {latest['adx']:.0f} confirms a downtrend")

    if _has(latest, "stoch_k") and latest["stoch_k"] < 25:
        score += 1
        reasons.append("Daily stochastic is washed out — bounce candidate")
    elif _has(latest, "stoch_k") and latest["stoch_k"] > 80:
        score -= 1
        reasons.append("Daily stochastic is extended — pullback risk")

    return score, reasons


def attach_tomorrow_forecast(
    sig: Signal,
    daily: pd.DataFrame,
    atr_stop_mult: float = 1.5,
    atr_target_mult: float = 2.5,
    long_only: bool = True,
) -> Signal:
    """Fill next-session (tomorrow) bias and a concrete enter / invalidation plan."""
    session = next_session_label()
    sig.tomorrow_session = session
    if daily is None or len(daily) < 55:
        sig.tomorrow_bias = "NO DATA"
        sig.tomorrow_plan = "Not enough daily history to forecast the next session."
        return sig

    latest = daily.iloc[-1]
    prev = daily.iloc[-2]
    score, reasons = _score_daily(prev, latest)
    close = float(latest["close"])
    high = float(latest["high"])
    low = float(latest["low"])
    atr = float(latest["atr"]) if pd.notna(latest["atr"]) else close * 0.015
    pivot = (high + low + close) / 3
    r1 = round(2 * pivot - low, 2)
    s1 = round(2 * pivot - high, 2)
    pivot = round(pivot, 2)

    if score >= 2:
        bias = "BULLISH"
        entry = round(max(s1, close - 0.4 * atr), 2)
        stop = round(min(low, entry) - atr_stop_mult * 0.35 * atr, 2)
        target = round(max(r1, close + atr_target_mult * 0.5 * atr), 2)
        invalid = round(min(s1, low) - 0.15 * atr, 2)
        plan = (
            f"{session}: look to BUY a dip toward {entry} (pivot/S1 zone). "
            f"Stop {stop}. Target {target}. Thesis dies below {invalid}."
        )
    elif score >= 1:
        bias = "MILD BULLISH"
        entry = pivot
        stop = round(s1 - 0.2 * atr, 2)
        target = r1
        invalid = s1
        plan = (
            f"{session}: only buy if price holds {pivot} and does not lose {s1}. "
            f"Stop {stop}. First target {target}."
        )
    elif score <= -2:
        bias = "BEARISH"
        entry = round(min(r1, close + 0.4 * atr), 2)
        stop = round(max(high, entry) + atr_stop_mult * 0.35 * atr, 2)
        target = round(min(s1, close - atr_target_mult * 0.5 * atr), 2)
        invalid = round(max(r1, high) + 0.15 * atr, 2)
        if long_only:
            plan = (
                f"{session}: STAY OUT of longs. Weakness continues unless price reclaims {invalid}. "
                f"Do not buy dips."
            )
            entry = None
            stop = None
            target = None
        else:
            plan = (
                f"{session}: sell rallies toward {entry}. Stop {stop}. Target {target}. "
                f"Cover if price reclaims {invalid}."
            )
    elif score <= -1:
        bias = "MILD BEARISH"
        entry = None
        stop = None
        target = r1
        invalid = r1
        plan = (
            f"{session}: avoid fresh longs. Wait — only consider a buy if price reclaims {r1} "
            f"and holds it."
        )
    else:
        bias = "NEUTRAL"
        entry = pivot
        stop = s1
        target = r1
        invalid = s1
        plan = (
            f"{session}: no edge. Trade only a confirmed break: buy strength above {r1}, "
            f"or stay out below {s1}."
        )

    sig.tomorrow_bias = bias
    sig.tomorrow_plan = plan
    sig.tomorrow_reasons = reasons
    sig.tomorrow_entry = entry
    sig.tomorrow_stop = stop
    sig.tomorrow_target = target
    sig.tomorrow_invalid = invalid
    return sig

