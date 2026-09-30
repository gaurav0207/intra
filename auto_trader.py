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

import adaptive_policy
import kite_client
import live_kite
import trading_policy
import kite_ws
import open_auction
import options
import paper_trader
import scanner
from data import IST, fetch_index, kite_health, market_status
from indicators import compute_all
from signals import entry_side


def log(message: str) -> None:
    stamp = dt.datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
    print(f"[{stamp}] {message}", flush=True)


def _kite():
    token = kite_client.load_saved_token()
    if not token:
        return None, "No Zerodha Kite session (not logged in, or the token expired)."
    try:
        client = kite_client.make_kite(token)
        client.profile()
        return client, ""
    except Exception as exc:  # noqa: BLE001
        log(f"Kite session unusable, falling back to Yahoo: {exc}")
        return None, str(exc)


def cycle(settings: dict) -> list[dict]:
    state = paper_trader.load_state()
    kite_alert = paper_trader.sync_kite_requirement(state, settings["require_kite"])
    if kite_alert is not None:
        sent, detail = kite_alert
        log(f"Zerodha requirement disabled; alert email {'sent' if sent else 'failed'}: {detail}")
    kite, kite_reason = _kite()
    kite_session = kite is not None
    conn_alert = paper_trader.sync_kite_connection(state, kite_session, kite_reason)
    if conn_alert is not None:
        sent, detail = conn_alert
        log(
            f"Kite {'CONNECTED' if kite_session else 'DISCONNECTED'}; "
            f"email {'sent' if sent else 'failed'}: {detail}"
        )
    kite_data_ok, kite_data_reason = kite_health(kite)
    if kite is not None:
        kite_ws.start()
    symbols = sorted(set(adaptive_policy.AI_UNIVERSE) | set(state["open_positions"]))
    signals, candles, failed = scanner.scan(
        symbols,
        settings["interval"],
        kite=kite,
        stop_mult=settings["stop_mult"],
        target_mult=settings["target_mult"],
        long_only=False,
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

    if kite is not None:
        try:
            raw_index = fetch_index(interval=settings["interval"], kite=kite)
            if not raw_index.empty:
                candles["NIFTY 50"] = compute_all(raw_index)
        except Exception:  # noqa: BLE001
            pass

    wanted: dict[str, tuple[str, float]] = {}
    for symbol, sig in signals.items():
        side = entry_side(sig)
        if side is None:
            continue
        wanted[symbol] = (side, float(sig.price))
    top = sorted(wanted, key=lambda s: -signals[s].confidence)[:12]
    contracts = options.resolve_many(kite, {s: wanted[s] for s in top}) if kite else {}
    options.apply_live_premiums(contracts)
    if wanted and not contracts:
        log(
            "No NFO ATM contracts resolved (Kite session, DTE window, or illiquid premium)."
        )
    elif contracts:
        log(
            "Options mapped: "
            + ", ".join(f"{c.tradingsymbol} @ ₹{c.premium}" for c in list(contracts.values())[:8])
        )

    option_marks: dict[str, float] = {}
    open_contracts = [
        pos["contract"]
        for pos in state["open_positions"].values()
        if pos.get("contract")
    ]
    if kite:
        ws_tokens = [
            int(pos["instrument_token"])
            for pos in state["open_positions"].values()
            if pos.get("instrument_token")
        ]
        for contract in contracts.values():
            ws_tokens.append(int(contract.instrument_token))
        if ws_tokens:
            kite_ws.subscribe_tokens(ws_tokens)
    if kite and open_contracts:
        if live_kite.is_enabled(state):
            exit_marks = options.fetch_option_exit_marks(kite, open_contracts)
            for symbol, pos in state["open_positions"].items():
                mark = exit_marks.get(str(pos.get("contract") or ""))
                if mark:
                    option_marks[symbol] = mark
        else:
            token_map = {
                pos["contract"]: int(pos["instrument_token"])
                for pos in state["open_positions"].values()
                if pos.get("contract") and pos.get("instrument_token")
            }
            ltps = options.fetch_option_ltps(kite, open_contracts, instrument_tokens=token_map)
            for symbol, pos in state["open_positions"].items():
                last = ltps.get(pos.get("contract", ""))
                if last:
                    option_marks[symbol] = last

    status = market_status()
    prices = {sym: sig.price for sym, sig in signals.items()}
    prices.update(option_marks)
    options.apply_live_premiums(contracts)
    plan = adaptive_policy.decide(state, signals, candles, prices, contracts=contracts)
    options.apply_live_premiums(plan.candidate_contracts)
    log(
        f"AI plan: {plan.regime} | confidence {plan.confidence_required}% | "
        f"max {plan.max_positions} | deploy ₹{sum(plan.candidate_budgets.values()):,.0f} | "
        f"projected target P&L ₹{plan.projected_profit:,.0f} | "
        f"selected {', '.join(plan.selected) or 'none'}"
    )
    intents = paper_trader.notify_watch_and_intents(
        state,
        {
            symbol: sig
            for symbol, sig in signals.items()
            if symbol in plan.candidate_budgets
        },
        candles,
        budget_per_trade=max(plan.candidate_budgets.values(), default=0),
        minimum_confidence=plan.confidence_required,
        require_kite=settings["require_kite"],
        kite_ok=kite_data_ok,
        market_open=status == "open",
        max_positions=plan.max_positions,
        plan_notes=plan.explanation,
        daily_profit_target=plan.daily_profit_target,
        plan_signature=plan.regime,
        candidate_contracts=plan.candidate_contracts,
        candidate_budgets=plan.candidate_budgets,
        daily_loss_limit=plan.daily_loss_limit,
    )
    for ok, detail in intents:
        log(f"Intent/plan email {'sent' if ok else 'failed'}: {detail}")

    halt = paper_trader.session_entry_block_reason(
        state, prices, plan.daily_profit_target, plan.daily_loss_limit, candles
    )
    if halt:
        log(f"Session halt active — no new entries: {halt}")
    pe_block = trading_policy.block_new_pe_reason(candles)
    if pe_block:
        log(pe_block)
    entry_block = trading_policy.entry_block_reason(state)
    if entry_block:
        log(f"Entry cooldown: {entry_block}")

    auto_on = bool(state.get("auto_trading_enabled", True))
    if not auto_on:
        log("Automatic trading is OFF (dashboard switch) — new entries skipped; exits still run.")

    events = paper_trader.run_cycle(
        state,
        signals,
        candles,
        enabled=auto_on,
        market_open=status == "open",
        budget_per_trade=0,
        max_positions=plan.max_positions,
        minimum_confidence=plan.confidence_required,
        require_kite=settings["require_kite"],
        candidate_budgets=plan.candidate_budgets,
        candidate_contracts=plan.candidate_contracts,
        option_marks=option_marks,
        daily_profit_target=plan.daily_profit_target,
        daily_loss_limit=plan.daily_loss_limit,
    )

    for event in events:
        if event.get("type") == "EXIT_WORKING":
            log(
                f"EXIT order Open {event.get('contract') or event.get('symbol')} "
                f"(Zerodha has not filled it yet)"
            )
            continue
        if event.get("type") == "ORDER_CLOSED":
            log(f"ENTRY Closed {event.get('contract') or event.get('symbol')}: {event.get('reason')}")
            continue
        if event.get("type") == "EXIT_FAILED":
            log(f"EXIT failed {event.get('contract') or event.get('symbol')}: {event.get('error')}")
            continue
        if event["type"] in {"BUY", "SHORT"}:
            label = event.get("contract") or event["symbol"]
            log(
                f"{event['type']:5s} {label} x{event.get('lots', event['quantity'])} "
                f"@ ₹{event['entry_price']:,.2f} "
                f"= ₹{event['amount_invested']:,.2f} (stop {event['stop']}, target {event['target']})"
            )
        else:
            log(
                f"{event['type']:5s} {event['symbol']} @ ₹{event['exit_price']:,.2f} "
                f"= ₹{event['proceeds']:,.2f} | P&L ₹{event['pnl']:+,.2f} ({event['pnl_pct']:+.2f}%) "
                f"| {event['exit_reason']}"
            )
        email = event.get("email", {})
        if not email.get("sent"):
            log(f"  email not sent: {email.get('detail')}")

    if not events:
        best = sorted(signals.values(), key=lambda s: -s.confidence)[:3]
        summary = ", ".join(f"{s.symbol} {s.confidence}% {s.what_to_do}" for s in best)
        if halt:
            log(f"No action ({status}). {halt}. Closest: {summary}")
        else:
            log(f"No action ({status}). Closest: {summary}")

    summary = paper_trader.portfolio_summary(
        state, prices
    )
    log(
        f"Equity ₹{summary['equity']:,.2f} | cash ₹{summary['cash']:,.2f} | "
        f"realized ₹{summary['realized_pnl']:+,.2f} | open ₹{summary['unrealized_pnl']:+,.2f}"
    )
    day_end = paper_trader.send_day_end_email(state, prices)
    if day_end is not None:
        sent, detail = day_end
        log(f"Day-end email {'sent' if sent else 'failed'}: {detail}")
    return events


def _option_marks_from_state(state: dict) -> dict[str, float]:
    marks: dict[str, float] = {}
    token_map = {
        pos["contract"]: int(pos["instrument_token"])
        for pos in state["open_positions"].values()
        if pos.get("contract") and pos.get("instrument_token")
    }
    symbols = [pos["contract"] for pos in state["open_positions"].values() if pos.get("contract")]
    if not symbols:
        return marks
    ltps = options.fetch_option_ltps(None, symbols, instrument_tokens=token_map)
    for symbol, pos in state["open_positions"].items():
        last = ltps.get(pos.get("contract", ""))
        if last:
            marks[symbol] = last
    return marks


def run_open_auction(settings: dict) -> list[dict]:
    """Fast 09:15–09:30 path: tape entries + premium exits, no 5m universe scan."""
    state = paper_trader.load_state()
    kite, kite_reason = _kite()
    if kite is not None:
        kite_ws.start()
    plan = open_auction.ensure_plan(state, kite)
    log(
        f"Open auction armed: {len(plan)} ATM name(s) · {open_auction.summary_line()}"
        + (f" · kite {kite_reason}" if kite is None else "")
    )
    if plan:
        log(
            "Watching "
            + ", ".join(
                f"{r['tradingsymbol']} ({r['bias']}, prev ₹{r.get('prev_premium') or 0:.2f})"
                for r in plan[:8]
            )
        )
    events = open_auction.try_entries(state, kite)
    state = paper_trader.load_state()
    marks = _option_marks_from_state(state)
    exits = paper_trader.run_cycle(
        state,
        {},
        {},
        enabled=False,
        market_open=True,
        budget_per_trade=0,
        max_positions=0,
        minimum_confidence=100,
        require_kite=settings["require_kite"],
        option_marks=marks,
    )
    events.extend(exits)
    for event in events:
        label = event.get("contract") or event.get("symbol")
        if event.get("type") in {"BUY", "SHORT"}:
            log(
                f"OPEN {event.get('side')} {label} @ ₹{event.get('entry_price'):,.2f} "
                f"· {event.get('open_auction_reason') or 'tape'}"
            )
        elif event.get("type") in {"COVER", "EXIT", "SELL"}:
            log(
                f"EXIT {label} @ ₹{event.get('exit_price', 0):,.2f} "
                f"| {event.get('exit_reason')}"
            )
    if not events:
        log("Open auction: no tape fill this pass.")
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
        f"Auto paper trading OPTIONS {paper_trader.ENTRY_START:%H:%M}–{paper_trader.ENTRY_END:%H:%M} IST "
        f"on {len(adaptive_policy.AI_UNIVERSE)} underlyings (ATM CE/PE), every {settings['every']}s. "
        f"{open_auction.summary_line()}. Ctrl+C to stop."
    )
    while True:
        now = dt.datetime.now(IST)
        state = paper_trader.load_state()
        try:
            login_url = kite_client.login_url()
        except Exception as exc:  # noqa: BLE001
            login_url = f"Unable to create Kite login URL: {exc}"
        morning = paper_trader.send_morning_login_email(
            state,
            login_url,
            os.getenv("APP_URL", "").strip(),
        )
        if morning is not None:
            sent, detail = morning
            log(f"09:30 login email {'sent' if sent else 'failed'}: {detail}")

        # Send the summary even if the process restarted after market close.
        if now.time() >= dt.time(15, 30):
            end = paper_trader.send_day_end_email(state, {})
            if end is not None:
                sent, detail = end
                log(f"Day-end email {'sent' if sent else 'failed'}: {detail}")

        status = market_status()
        if open_auction.enabled() and now.weekday() < 5 and open_auction.in_prep_window():
            kite, reason = _kite()
            if kite is not None:
                kite_ws.start()
            plan = open_auction.ensure_plan(paper_trader.load_state(), kite)
            log(
                f"Pre-open: subscribed {len(plan)} auction contract(s)"
                + ("" if kite else f" (no kite: {reason})")
            )
            time.sleep(5)
            continue
        if open_auction.enabled() and now.weekday() < 5 and open_auction.in_entry_window():
            try:
                run_open_auction(settings)
            except Exception as exc:  # noqa: BLE001
                log(f"Open auction failed, will retry: {exc}")
            time.sleep(2)
            continue
        if status != "open":
            wake = dt.time(9, 0) if open_auction.enabled() else dt.time(9, 15)
            wait = seconds_until(wake)
            log(f"Market {status}. Sleeping {wait / 3600:.1f}h until {wake:%H:%M}.")
            time.sleep(min(wait, 3600))
            continue

        # Exits must keep running until square-off, so the loop stays awake
        # past the entry cut-off.
        if now.time() >= paper_trader.SQUARE_OFF:
            state = paper_trader.load_state()
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
        "interval": args.interval,
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
    parser.add_argument("--interval", default="5m", choices=["1m", "5m", "15m"])
    parser.add_argument(
        "--cash",
        default=os.getenv("PAPER_INITIAL_CASH", str(int(paper_trader.default_initial_cash()))),
    )
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
        now = dt.datetime.now(IST)
        if open_auction.enabled() and now.weekday() < 5 and (
            open_auction.in_prep_window() or open_auction.in_entry_window()
        ):
            run_open_auction(settings)
        else:
            cycle(settings)
        return 0
    try:
        loop(settings)
    except KeyboardInterrupt:
        log("Stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
