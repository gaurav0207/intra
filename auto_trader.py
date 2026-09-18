"""Headless automatic paper trading, 9:30–15:00 IST.

Streamlit only executes while a browser session is connected, so the dashboard
cannot trade unattended. Run this instead, on any machine that stays awake:

    python3 auto_trader.py --loop          # runs all day, sleeps overnight
    python3 auto_trader.py --once          # single pass, for cron

It writes to the same paper_trades.json and sends the same emails as the app.
No real broker order is ever placed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import time

import kite_client
import paper_trader
import scanner
import universe
from data import IST, kite_health, market_status


def log(message: str) -> None:
    stamp = dt.datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
    print(f"[{stamp}] {message}", flush=True)


def _kite():
    token = kite_client.load_saved_token()
    if not token:
        return None
    try:
        client = kite_client.make_kite(token)
        client.profile()
        return client
    except Exception as exc:  # noqa: BLE001
        log(f"Kite session unusable, falling back to Yahoo: {exc}")
        return None


def cycle(settings: dict) -> list[dict]:
    state = paper_trader.load_state(settings["initial_cash"])
    kite_alert = paper_trader.sync_kite_requirement(state, settings["require_kite"])
    if kite_alert is not None:
        sent, detail = kite_alert
        log(f"Zerodha requirement disabled; alert email {'sent' if sent else 'failed'}: {detail}")
    kite = _kite()
    kite_data_ok, kite_data_reason = kite_health(kite)
    symbols = sorted(set(settings["universe"]) | set(state["open_positions"]))
    signals, candles, failed = scanner.scan(
        symbols,
        settings["interval"],
        kite=kite,
        stop_mult=settings["stop_mult"],
        target_mult=settings["target_mult"],
        long_only=True,
    )
    if kite_data_ok and signals and all(sig.data_source != "kite" for sig in signals.values()):
        kite_data_ok = False
        kite_data_reason = "Kite login exists, but every candle came from Yahoo Finance instead of Zerodha."
    fetch_alert = paper_trader.sync_kite_fetch(state, kite_data_ok, kite_data_reason)
    if fetch_alert is not None:
        sent, detail = fetch_alert
        log(
            f"Zerodha fetch failed ({kite_data_reason}); "
            f"alert email {'sent' if sent else 'failed'}: {detail}"
        )
    elif not kite_data_ok:
        log(f"Zerodha data unavailable: {kite_data_reason}")
    if failed:
        log(f"No data for: {', '.join(failed)}")

    status = market_status()
    events = paper_trader.run_cycle(
        state,
        signals,
        candles,
        enabled=True,
        market_open=status == "open",
        budget_per_trade=settings["budget"],
        max_positions=settings["max_positions"],
        minimum_confidence=settings["min_confidence"],
        require_kite=settings["require_kite"],
    )

    for event in events:
        if event["type"] == "BUY":
            log(
                f"BUY  {event['symbol']} x{event['quantity']} @ ₹{event['entry_price']:,.2f} "
                f"= ₹{event['amount_invested']:,.2f} (stop {event['stop']}, target {event['target']})"
            )
        else:
            log(
                f"SELL {event['symbol']} @ ₹{event['exit_price']:,.2f} "
                f"= ₹{event['proceeds']:,.2f} | P&L ₹{event['pnl']:+,.2f} ({event['pnl_pct']:+.2f}%) "
                f"| {event['exit_reason']}"
            )
        email = event.get("email", {})
        if not email.get("sent"):
            log(f"  email not sent: {email.get('detail')}")

    if not events:
        best = sorted(signals.values(), key=lambda s: -s.confidence)[:3]
        summary = ", ".join(f"{s.symbol} {s.confidence}% {s.what_to_do}" for s in best)
        log(f"No action ({status}). Closest: {summary}")

    summary = paper_trader.portfolio_summary(
        state, {sym: sig.price for sym, sig in signals.items()}
    )
    log(
        f"Equity ₹{summary['equity']:,.2f} | cash ₹{summary['cash']:,.2f} | "
        f"realized ₹{summary['realized_pnl']:+,.2f} | open ₹{summary['unrealized_pnl']:+,.2f}"
    )
    return events


def seconds_until(target: dt.time) -> float:
    now = dt.datetime.now(IST)
    nxt = now.replace(hour=target.hour, minute=target.minute, second=0, microsecond=0)
    if nxt <= now:
        nxt += dt.timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += dt.timedelta(days=1)
    return (nxt - now).total_seconds()


def loop(settings: dict) -> None:
    log(
        f"Auto paper trading {paper_trader.ENTRY_START:%H:%M}–{paper_trader.ENTRY_END:%H:%M} IST "
        f"on {len(settings['universe'])} stocks, every {settings['every']}s. Ctrl+C to stop."
    )
    while True:
        now = dt.datetime.now(IST)
        status = market_status()
        if status != "open":
            wait = seconds_until(dt.time(9, 15))
            log(f"Market {status}. Sleeping {wait / 3600:.1f}h until the next session.")
            time.sleep(min(wait, 3600))
            continue

        # Exits must keep running until square-off, so the loop stays awake
        # past the entry cut-off.
        if now.time() >= paper_trader.SQUARE_OFF:
            state = paper_trader.load_state(settings["initial_cash"])
            if not state["open_positions"]:
                wait = seconds_until(dt.time(9, 15))
                log(f"Session done, all flat. Sleeping {wait / 3600:.1f}h.")
                time.sleep(min(wait, 3600))
                continue

        try:
            cycle(settings)
        except Exception as exc:  # noqa: BLE001
            log(f"Cycle failed, will retry: {exc}")
        time.sleep(settings["every"])


def build_settings(args) -> dict:
    return {
        "universe": universe.get(args.universe),
        "interval": args.interval,
        "budget": float(args.budget),
        "max_positions": int(args.max_positions),
        "min_confidence": int(args.min_confidence),
        "initial_cash": float(args.cash),
        "stop_mult": float(args.stop_mult),
        "target_mult": float(args.target_mult),
        "require_kite": args.require_kite,
        "every": int(args.every),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Headless paper trading for NSE intraday signals.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--loop", action="store_true", help="Run continuously through the session.")
    mode.add_argument("--once", action="store_true", help="Run a single scan, then exit.")
    parser.add_argument("--universe", default=universe.DEFAULT_UNIVERSE, choices=list(universe.UNIVERSES))
    parser.add_argument("--interval", default="5m", choices=["1m", "5m", "15m"])
    parser.add_argument("--budget", default=os.getenv("PAPER_BUDGET", "5000"))
    parser.add_argument("--max-positions", default=os.getenv("PAPER_MAX_POSITIONS", "5"))
    parser.add_argument("--min-confidence", default=os.getenv("PAPER_MIN_CONFIDENCE", "65"))
    parser.add_argument("--cash", default=os.getenv("PAPER_INITIAL_CASH", "100000"))
    parser.add_argument("--stop-mult", default="1.5")
    parser.add_argument("--target-mult", default="2.5")
    parser.add_argument("--every", default=os.getenv("PAPER_INTERVAL_SECONDS", "60"))
    parser.add_argument(
        "--require-kite",
        dest="require_kite",
        action="store_true",
        default=True,
        help="Skip entries unless Kite data is live (default).",
    )
    parser.add_argument(
        "--no-require-kite",
        dest="require_kite",
        action="store_false",
        help="Allow paper entries on delayed Yahoo prices and email ALERT_EMAIL once.",
    )
    args = parser.parse_args()

    settings = build_settings(args)
    if args.once:
        cycle(settings)
        return 0
    try:
        loop(settings)
    except KeyboardInterrupt:
        log("Stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
