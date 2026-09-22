"""One-off diagnostic: count why the adaptive policy rejected every candidate.

Run inside the trader container:
    python diagnose_entries.py
"""

from __future__ import annotations

import collections
import math

import adaptive_policy
import kite_client
import paper_trader
import scanner
import signals as signals_mod


def main() -> int:
    token = kite_client.load_saved_token()
    kite = None
    if token:
        try:
            kite = kite_client.make_kite(token)
            kite.profile()
        except Exception as exc:  # noqa: BLE001
            print(f"kite unusable: {exc}")
            kite = None
    print(f"kite session: {'yes' if kite else 'no'}")

    state = paper_trader.load_state(100_000.0)
    symbols = sorted(set(adaptive_policy.AI_UNIVERSE) | set(state["open_positions"]))
    signals, candles, failed = scanner.scan(
        symbols, "5m", kite=kite, stop_mult=1.5, target_mult=2.5, long_only=False
    )
    print(f"scanned {len(signals)} symbols, {len(failed)} failed")

    prices = {s: sig.price for s, sig in signals.items()}
    plan = adaptive_policy.decide(state, signals, candles, prices)
    print(f"plan: {plan.regime} conf>={plan.confidence_required} max={plan.max_positions} risk={plan.risk_per_trade}")

    equity = float(state["cash"])
    available_cash = float(state["cash"])
    reasons = collections.Counter()
    actions = collections.Counter()
    conf_ok_but_not_enter = []
    eligible_conf: list[dict] = []

    for symbol, sig in signals.items():
        actions[sig.what_to_do] += 1
        if symbol in state["open_positions"] or symbol not in candles:
            reasons["already open / no candles"] += 1
            continue
        side = signals_mod.entry_side(sig)
        if side is None:
            if sig.what_to_do in {"HOLD LONG", "HOLD SHORT"}:
                reasons["held setup already past half its move"] += 1
            else:
                reasons[f"no entry/hold bar ({sig.what_to_do})"] += 1
            if sig.confidence >= plan.confidence_required:
                conf_ok_but_not_enter.append(f"{symbol} {sig.confidence}% {sig.what_to_do}")
            continue
        price = float(sig.price)
        stop = float(sig.stop_loss or price)
        target = float(sig.target or price)
        stop_distance = price - stop if side == "LONG" else stop - price
        reward = target - price if side == "LONG" else price - target
        bar = candles[symbol].iloc[-1]
        traded_value = float(bar.get("vol_avg", 0)) * price
        atr = float(bar.get("atr", 0))
        rr = reward / stop_distance if stop_distance > 0 else 0.0
        qty_by_risk = math.floor(plan.risk_per_trade / stop_distance) if stop_distance > 0 else 0
        cap = equity * (0.12 if plan.regime == "RISK-ON" else 0.08)
        qty_by_cap = math.floor(min(cap, available_cash) / price)
        eligible_conf.append(
            {
                "symbol": symbol,
                "conf": sig.confidence,
                "action": sig.what_to_do,
                "atr_pct": atr / price if price else 0.0,
                "rr": rr,
                "cr_value": traded_value / 10_000_000,
                "qty": min(qty_by_risk, qty_by_cap),
                "stop_vs_atr": stop_distance / atr if atr else 0.0,
            }
        )
        if sig.confidence < plan.confidence_required:
            reasons[f"confidence < {plan.confidence_required}"] += 1
            continue
        if price <= 0 or stop_distance <= 0 or rr < 1.25:
            reasons["reward/risk < 1.25"] += 1
            continue
        if traded_value < 5_000_000:
            reasons["illiquid (<50L traded value)"] += 1
            continue
        if not adaptive_policy.MIN_ATR_PCT <= atr / price <= adaptive_policy.MAX_ATR_PCT:
            reasons["atr% outside band"] += 1
            continue
        if stop_distance < 0.25 * atr:
            reasons["stop already trailed onto price"] += 1
            continue
        if min(qty_by_risk, qty_by_cap) < 1:
            reasons[f"qty<1 (risk gives {qty_by_risk}, cap gives {qty_by_cap})"] += 1
            continue
        reasons["PASSED"] += 1

    print("\n-- what_to_do distribution --")
    for k, v in actions.most_common():
        print(f"{v:4d}  {k}")
    print("\n-- rejection reasons --")
    for k, v in reasons.most_common():
        print(f"{v:4d}  {k}")
    print(f"\n-- entry-eligible candidates ({len(eligible_conf)}) --")
    print(f"  {'symbol':12s} {'conf':>5s} {'atr%':>7s} {'R:R':>6s} {'cr/bar':>7s} {'qty':>5s} {'stop/atr':>9s}  action")
    for row in sorted(eligible_conf, key=lambda r: -r["conf"]):
        print(
            f"  {row['symbol']:12s} {row['conf']:4d}% {row['atr_pct']:6.3%} {row['rr']:6.2f} "
            f"{row['cr_value']:7.2f} {row['qty']:5d} {row['stop_vs_atr']:9.2f}  {row['action']}"
        )

    print(f"\n-- high confidence but not an ENTER bar ({len(conf_ok_but_not_enter)}) --")
    for row in conf_ok_but_not_enter[:25]:
        print(f"  {row}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
