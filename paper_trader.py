"""Persistent paper portfolio and email notifications.

This module never sends an order to Zerodha. It only records simulated fills.
"""

from __future__ import annotations

import json
import math
import os
import smtplib
from datetime import datetime, time
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pandas as pd

from signals import entry_side

STATE_PATH = Path(os.getenv("PAPER_TRADING_FILE", "paper_trades.json"))
IST = "Asia/Kolkata"

# Entries are allowed only inside this window; exits run any time the market is
# open. Candle timestamps can lag wall clock by ~15 minutes on delayed feeds, so
# the window is enforced against the clock as well as the candle.
ENTRY_START = time(9, 30)
ENTRY_END = time(15, 0)
SQUARE_OFF = time(15, 15)


def _now() -> str:
    return pd.Timestamp.now(tz=IST).isoformat()


def new_state(initial_cash: float) -> dict[str, Any]:
    return {
        "version": 1,
        "initial_cash": round(float(initial_cash), 2),
        "cash": round(float(initial_cash), 2),
        "open_positions": {},
        "trades": [],
        "events": [],
        "seen_entries": [],
        "kite_requirement_alerted": False,
        "kite_fetch_alerted": False,
        "kite_fetch_reason": "",
        "kite_fetch_alerted_at": "",
        "kite_connected": None,
        "seen_intents": [],
        "last_briefing_date": "",
        "last_briefing_signature": "",
        "login_email_date": "",
        "day_end_email_date": "",
        "updated_at": _now(),
    }


def load_state(initial_cash: float = 100_000) -> dict[str, Any]:
    if not STATE_PATH.exists():
        state = new_state(initial_cash)
        save_state(state)
        return state
    try:
        state = json.loads(STATE_PATH.read_text())
        if not isinstance(state, dict):
            raise ValueError("paper state must be a JSON object")
        state.setdefault("open_positions", {})
        state.setdefault("trades", [])
        state.setdefault("events", [])
        state.setdefault("seen_entries", [])
        state.setdefault("kite_requirement_alerted", False)
        state.setdefault("kite_fetch_alerted", False)
        state.setdefault("kite_fetch_reason", "")
        state.setdefault("kite_fetch_alerted_at", "")
        state.setdefault("kite_connected", None)
        state.setdefault("seen_intents", [])
        state.setdefault("last_briefing_date", "")
        state.setdefault("last_briefing_signature", "")
        state.setdefault("login_email_date", "")
        state.setdefault("day_end_email_date", "")
        state.setdefault("initial_cash", float(initial_cash))
        state.setdefault("cash", float(initial_cash))
        return state
    except (OSError, ValueError, json.JSONDecodeError):
        backup = STATE_PATH.with_suffix(f".broken-{datetime.now():%Y%m%d-%H%M%S}.json")
        STATE_PATH.replace(backup)
        state = new_state(initial_cash)
        state["events"].append(
            {"time": _now(), "type": "STATE_RESET", "reason": f"Invalid state backed up to {backup.name}"}
        )
        save_state(state)
        return state


def save_state(state: dict[str, Any]) -> None:
    state["updated_at"] = _now()
    temp = STATE_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    temp.replace(STATE_PATH)


def reset_state(initial_cash: float) -> dict[str, Any]:
    state = new_state(initial_cash)
    save_state(state)
    return state


def _email_settings() -> dict[str, Any]:
    return {
        "host": os.getenv("SMTP_HOST", "smtp.gmail.com").strip(),
        "port": int(os.getenv("SMTP_PORT", "587")),
        "user": os.getenv("SMTP_USER", "").strip(),
        "password": os.getenv("SMTP_PASSWORD", "").strip(),
        "to": os.getenv("ALERT_EMAIL", "").strip(),
        "from": os.getenv("SMTP_FROM", os.getenv("SMTP_USER", "")).strip(),
    }


def email_ready() -> bool:
    cfg = _email_settings()
    return bool(cfg["host"] and cfg["user"] and cfg["password"] and cfg["to"] and cfg["from"])


def send_email(subject: str, body: str) -> tuple[bool, str]:
    cfg = _email_settings()
    if not email_ready():
        return False, "SMTP not configured"

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg["from"]
    msg["To"] = cfg["to"]
    msg.set_content(body)
    try:
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=15) as smtp:
            smtp.starttls()
            smtp.login(cfg["user"], cfg["password"])
            smtp.send_message(msg)
        return True, f"sent to {cfg['to']}"
    except smtplib.SMTPAuthenticationError as exc:
        return False, (
            "Gmail rejected the login. Create an App Password at "
            "https://myaccount.google.com/apppasswords and use that 16-character "
            f"value as SMTP_PASSWORD, not your account password. ({exc.smtp_code})"
        )
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def sync_kite_requirement(state: dict[str, Any], require_kite: bool) -> tuple[bool, str] | None:
    """Email ALERT_EMAIL once when Zerodha-required entries are turned off.

    Returns (sent, detail) when an email is attempted, otherwise None.
    """
    if require_kite:
        if state.get("kite_requirement_alerted"):
            state["kite_requirement_alerted"] = False
            save_state(state)
        return None

    if state.get("kite_requirement_alerted"):
        return None

    body = (
        "Zerodha data is no longer required for new paper entries.\n\n"
        "The dashboard will now simulate buys against delayed Yahoo Finance "
        "prices. Fills will not match live NSE quotes. No real broker order "
        "is placed.\n\n"
        f"Time: {_now()}\n"
        "Turn the setting back on in the sidebar to require Kite data again."
    )
    ok, detail = send_email(
        "Paper trading: Zerodha data requirement DISABLED",
        body,
    )
    state["kite_requirement_alerted"] = True
    _record_event(
        state,
        {
            "time": _now(),
            "type": "KITE_REQUIREMENT_DISABLED",
            "email": {"sent": ok, "detail": detail},
        },
    )
    save_state(state)
    return ok, detail


def sync_kite_fetch(state: dict[str, Any], healthy: bool, reason: str = "") -> tuple[bool, str] | None:
    """Email ALERT_EMAIL when Zerodha candles cannot be fetched.

    Same reason is not re-sent until Kite recovers, or 4 hours pass.
    """
    if healthy:
        if state.get("kite_fetch_alerted"):
            state["kite_fetch_alerted"] = False
            state["kite_fetch_reason"] = ""
            state["kite_fetch_alerted_at"] = ""
            save_state(state)
        return None

    reason = (reason or "Unknown Zerodha fetch failure").strip()
    last_reason = str(state.get("kite_fetch_reason") or "")
    last_at = str(state.get("kite_fetch_alerted_at") or "")
    if state.get("kite_fetch_alerted") and last_reason == reason:
        if last_at:
            try:
                age_hours = (pd.Timestamp.now(tz=IST) - pd.Timestamp(last_at)).total_seconds() / 3600
                if age_hours < 4:
                    return None
            except (TypeError, ValueError):
                return None
        else:
            return None

    body = (
        "The app could not fetch market data from Zerodha Kite.\n\n"
        f"Reason: {reason}\n"
        f"Time: {_now()}\n\n"
        "Paper entries that require Kite data are blocked until this is fixed. "
        "Typical causes: expired daily login, missing market-data subscription, "
        "or insufficient API permissions.\n\n"
        "Reconnect Kite in the sidebar, or check "
        "https://developers.kite.trade/ for the historical/LTP permission."
    )
    ok, detail = send_email("Paper trading: Zerodha data fetch FAILED", body)
    state["kite_fetch_alerted"] = True
    state["kite_fetch_reason"] = reason
    state["kite_fetch_alerted_at"] = _now()
    _record_event(
        state,
        {
            "time": _now(),
            "type": "KITE_FETCH_FAILED",
            "reason": reason,
            "email": {"sent": ok, "detail": detail},
        },
    )
    save_state(state)
    return ok, detail


def sync_kite_connection(
    state: dict[str, Any],
    connected: bool,
    reason: str = "",
) -> tuple[bool, str] | None:
    """Email when the trader's Kite session appears or disappears."""
    previous = state.get("kite_connected")
    if previous is connected:
        return None

    state["kite_connected"] = connected
    if previous is None and not connected:
        save_state(state)
        return None

    if connected:
        subject = "[Trader] Kite CONNECTED"
        body = (
            "The paper trader is connected to Zerodha Kite.\n\n"
            f"Time: {_now()}\n"
            "It can use Kite candles for new entries if your app has market-data "
            "permission. No real broker order is placed."
        )
        event_type = "KITE_CONNECTED"
    else:
        subject = "[Trader] Kite DISCONNECTED"
        body = (
            "The paper trader lost its Zerodha Kite session.\n\n"
            f"Reason: {reason or 'session missing or expired'}\n"
            f"Time: {_now()}\n\n"
            "Open the dashboard, complete the daily Kite login, and paste the "
            "request_token. The trader picks it up on the next cycle.\n"
            "Until then, entries that require Kite data stay blocked."
        )
        event_type = "KITE_DISCONNECTED"

    ok, detail = send_email(subject, body)
    _record_event(
        state,
        {
            "time": _now(),
            "type": event_type,
            "reason": reason,
            "email": {"sent": ok, "detail": detail},
        },
    )
    save_state(state)
    return ok, detail


def send_morning_login_email(
    state: dict[str, Any],
    login_url: str,
    app_url: str,
) -> tuple[bool, str] | None:
    """Send one daily Kite login email at/after 09:30 IST."""
    now = pd.Timestamp.now(tz=IST)
    today = now.strftime("%Y-%m-%d")
    if (
        now.weekday() >= 5
        or not (ENTRY_START <= now.time() < ENTRY_END)
        or state.get("login_email_date") == today
    ):
        return None
    body = (
        "The NSE entry window is now open.\n\n"
        f"1. Login to Kite: {login_url}\n"
        f"2. Open the dashboard to confirm/add the token: {app_url or 'APP_URL is not configured'}\n\n"
        "If the Kite developer redirect URL points to the dashboard, the request "
        "token is captured automatically after login.\n\n"
        f"Entries: {ENTRY_START:%H:%M}–{ENTRY_END:%H:%M} IST\n"
        f"Square-off: {SQUARE_OFF:%H:%M} IST\n"
        f"Time: {_now()}\nNo real broker order is placed."
    )
    ok, detail = send_email("[Trader] 09:30 — Login to Kite and open dashboard", body)
    state["login_email_date"] = today
    _record_event(
        state,
        {"time": _now(), "type": "MORNING_LOGIN_EMAIL", "email": {"sent": ok, "detail": detail}},
    )
    save_state(state)
    return ok, detail


def send_day_end_email(
    state: dict[str, Any],
    prices: dict[str, float],
) -> tuple[bool, str] | None:
    """Send one end-of-day summary at/after 15:15 IST."""
    now = pd.Timestamp.now(tz=IST)
    today = now.strftime("%Y-%m-%d")
    if now.weekday() >= 5 or now.time() < SQUARE_OFF or state.get("day_end_email_date") == today:
        return None

    trades = [
        t
        for t in state.get("trades", [])
        if t.get("exit_time")
        and pd.Timestamp(t["exit_time"]).tz_convert(IST).strftime("%Y-%m-%d") == today
    ]
    realized = sum(float(t.get("pnl", 0)) for t in trades)
    wins = sum(float(t.get("pnl", 0)) > 0 for t in trades)
    losses = sum(float(t.get("pnl", 0)) < 0 for t in trades)
    details = "\n".join(
        f"- {t['symbol']} {t.get('side', 'LONG')}: "
        f"₹{float(t.get('pnl', 0)):+,.2f} ({float(t.get('pnl_pct', 0)):+.2f}%) · "
        f"{t.get('exit_reason', '')}"
        for t in trades
    ) or "- No completed trades today."
    summary = portfolio_summary(state, prices)
    body = (
        "The trading day has ended. No more new entries will be taken today.\n\n"
        f"Completed trades: {len(trades)} ({wins} wins, {losses} losses)\n"
        f"Realized P&L today: ₹{realized:+,.2f}\n"
        f"Account equity: ₹{summary['equity']:,.2f}\n"
        f"Total account P&L: ₹{summary['total_pnl']:+,.2f}\n"
        f"Open positions: {len(state.get('open_positions', {}))}\n\n"
        f"Today's trades:\n{details}\n\n"
        f"Time: {_now()}\nNext entry window: 09:30 IST on the next NSE weekday."
    )
    ok, detail = send_email(
        f"[Trader] Day ended — P&L ₹{realized:+,.2f}",
        body,
    )
    state["day_end_email_date"] = today
    _record_event(
        state,
        {"time": _now(), "type": "DAY_END_EMAIL", "email": {"sent": ok, "detail": detail}},
    )
    save_state(state)
    return ok, detail


def notify_watch_and_intents(
    state: dict[str, Any],
    signals: dict[str, Any],
    chart_data: dict[str, Any],
    budget_per_trade: float,
    minimum_confidence: int,
    require_kite: bool,
    kite_ok: bool,
    market_open: bool,
    max_positions: int,
    plan_notes: list[str] | None = None,
    daily_profit_target: float | None = None,
    plan_signature: str | None = None,
) -> list[tuple[bool, str]]:
    """Email what the trader is about to buy, without repeating the same setup."""
    sent_results: list[tuple[bool, str]] = []
    now = pd.Timestamp.now(tz=IST)
    in_window = bool(market_open and ENTRY_START <= now.time() < ENTRY_END)
    today = now.strftime("%Y-%m-%d")
    seen = list(state.get("seen_intents") or [])

    def estimated_qty(price: float) -> int:
        available = min(float(budget_per_trade), float(state["cash"]))
        return math.floor(available / price) if price > 0 else 0

    lines = []
    new_keys = []
    for symbol, sig in sorted(signals.items(), key=lambda kv: -kv[1].confidence):
        if symbol in state["open_positions"] or symbol not in chart_data:
            continue
        side = entry_side(sig)
        if side is None or sig.confidence < minimum_confidence:
            continue
        timestamp = pd.Timestamp(chart_data[symbol].index[-1]).isoformat()
        key = f"{symbol}|{timestamp}|{side}|INTENT"
        if key in seen:
            continue
        qty = estimated_qty(float(sig.price))
        amount = round(qty * float(sig.price), 2) if qty else 0.0
        blockers = []
        if not in_window:
            blockers.append(f"waiting for the {ENTRY_START:%H:%M}–{ENTRY_END:%H:%M} IST window")
        if require_kite and not kite_ok:
            blockers.append("waiting for live Kite data")
        if qty < 1:
            blockers.append("price is above the per-trade budget")
        if len(state["open_positions"]) >= int(max_positions):
            blockers.append(f"already at max {max_positions} open positions")
        status = "WILL INVEST ON THIS CYCLE" if not blockers else "WATCHING — blocked: " + "; ".join(blockers)
        lines.append(
            f"- {symbol} {side} @ ₹{sig.price:,.2f} · ~{qty} shares · ~₹{amount:,.2f} · "
            f"conf {sig.confidence}% · score {sig.score}\n"
            f"  Stop ₹{sig.stop_loss} · Target ₹{sig.target}\n"
            f"  {status}"
        )
        new_keys.append(key)

    # Live breadth, P&L and projected amounts change every scan. They belong in
    # the email body, but not in its identity; otherwise each small market move
    # sends another "session briefing". A new briefing is sent only once at
    # session start or when the actual policy/regime changes.
    briefing_signature = "|".join(
        [
            today,
            plan_signature or "",
            str(minimum_confidence),
            str(max_positions),
            str(round(float(daily_profit_target or 0), 2)),
        ]
    )
    if (
        in_window
        and (
            state.get("last_briefing_date") != today
            or state.get("last_briefing_signature") != briefing_signature
        )
    ):
        ranked = sorted(
            (
                (sig.confidence, sig.score, sym, sig.what_to_do, sig.price)
                for sym, sig in signals.items()
                if sym not in state["open_positions"]
            ),
            reverse=True,
        )[:5]
        watch_lines = "\n".join(
            f"- {sym}: {instruction} · {conf}% · score {score} · ₹{price:,.2f}"
            for conf, score, sym, instruction, price in ranked
        ) or "- none yet"
        notes = "\n".join(plan_notes or [])
        body = (
            "SESSION BRIEFING — when and what the trader will buy\n\n"
            f"Entry window: {ENTRY_START:%H:%M}–{ENTRY_END:%H:%M} IST (weekdays)\n"
            f"Square-off: {SQUARE_OFF:%H:%M} IST\n"
            f"Max positions: {max_positions}\n"
            f"Budget per trade: ₹{float(budget_per_trade):,.0f}\n"
            f"Minimum confidence: {minimum_confidence}%\n"
            f"Daily profit objective: ₹{float(daily_profit_target or 0):,.0f} "
            "(objective, not guaranteed)\n"
            f"Kite required: {'yes' if require_kite else 'no'} "
            f"(currently {'connected' if kite_ok else 'not connected'})\n\n"
            + (notes + "\n\n" if notes else "")
            +
            "It enters only on a FRESH ENTER LONG/SHORT signal that clears those rules.\n"
            "You also get an email at the exact moment it buys or exits.\n\n"
            f"Closest names right now:\n{watch_lines}\n\n"
            f"Time: {_now()}\nNo real broker order is placed."
        )
        ok, detail = send_email("[Trader] Today's paper-invest plan", body)
        sent_results.append((ok, detail))
        state["last_briefing_date"] = today
        state["last_briefing_signature"] = briefing_signature
        _record_event(
            state,
            {"time": _now(), "type": "SESSION_BRIEFING", "email": {"sent": ok, "detail": detail}},
        )

    if lines:
        body = (
            "The trader has a live LONG/SHORT entry setup.\n\n"
            + "\n".join(lines)
            + f"\n\nWindow: {ENTRY_START:%H:%M}–{ENTRY_END:%H:%M} IST · "
            f"Kite {'connected' if kite_ok else 'disconnected'}\n"
            f"Time: {_now()}\n"
            "A second email is sent the instant a paper BUY or EXIT fills.\n"
            "No real broker order is placed."
        )
        ok, detail = send_email("[Trader] Will invest / watching", body)
        sent_results.append((ok, detail))
        seen.extend(new_keys)
        state["seen_intents"] = seen[-500:]
        _record_event(
            state,
            {"time": _now(), "type": "INTENT", "symbols": new_keys, "email": {"sent": ok, "detail": detail}},
        )

    if sent_results:
        save_state(state)
    return sent_results


def _record_event(state: dict[str, Any], event: dict[str, Any]) -> None:
    state["events"].append(event)
    state["events"] = state["events"][-1000:]


def open_position(
    state: dict[str, Any],
    symbol: str,
    side: str,
    price: float,
    budget: float,
    stop: float,
    target: float,
    confidence: int,
    score: int,
    signal_key: str,
) -> dict[str, Any] | None:
    side = side.upper()
    if side not in {"LONG", "SHORT"}:
        raise ValueError(f"Unsupported paper side: {side}")
    price = round(float(price), 2)
    available = min(float(budget), float(state["cash"]))
    quantity = math.floor(available / price) if price > 0 else 0
    if quantity < 1 or symbol in state["open_positions"]:
        return None

    amount = round(quantity * price, 2)
    position = {
        "symbol": symbol,
        "side": side,
        "entry_time": _now(),
        "entry_price": price,
        "quantity": quantity,
        "amount_invested": amount,
        "stop": round(float(stop), 2),
        "target": round(float(target), 2),
        "confidence": int(confidence),
        "entry_score": int(score),
        "signal_key": signal_key,
    }
    state["cash"] = round(float(state["cash"]) - amount, 2)
    state["open_positions"][symbol] = position
    state["seen_entries"].append(signal_key)
    state["seen_entries"] = state["seen_entries"][-500:]
    event_type = "BUY" if side == "LONG" else "SHORT"
    event = {"time": _now(), "type": event_type, **position, "cash_after": state["cash"]}
    _record_event(state, event)

    body = (
        f"INSTANT PAPER {'BUY' if side == 'LONG' else 'SHORT SELL'} — "
        "the trader just filled this setup.\n\n"
        f"Side: {side}\n"
        f"Share: {symbol}\nPrice: ₹{price:,.2f}\nQuantity: {quantity}\n"
        f"Amount invested: ₹{amount:,.2f}\nStop-loss: ₹{position['stop']:,.2f}\n"
        f"Target: ₹{position['target']:,.2f}\nConfidence: {confidence}%\n"
        f"Cash remaining: ₹{state['cash']:,.2f}\n\nNo real broker order was placed."
    )
    verb = "BUY NOW" if side == "LONG" else "SHORT NOW"
    ok, detail = send_email(f"[Trader] {verb}: {symbol} × {quantity} @ ₹{price:,.2f}", body)
    event["email"] = {"sent": ok, "detail": detail}
    save_state(state)
    return event


def close_position(
    state: dict[str, Any],
    symbol: str,
    price: float,
    reason: str,
) -> dict[str, Any] | None:
    position = state["open_positions"].pop(symbol, None)
    if not position:
        return None

    price = round(float(price), 2)
    quantity = int(position["quantity"])
    side = str(position.get("side", "LONG")).upper()
    invested = float(position["amount_invested"])
    if side == "SHORT":
        pnl = round((float(position["entry_price"]) - price) * quantity, 2)
    else:
        pnl = round((price - float(position["entry_price"])) * quantity, 2)
    proceeds = round(invested + pnl, 2)
    pnl_pct = round((pnl / invested) * 100, 2) if invested else 0.0
    state["cash"] = round(float(state["cash"]) + proceeds, 2)

    trade = {
        **position,
        "exit_time": _now(),
        "exit_price": price,
        "proceeds": proceeds,
        "pnl": pnl,
        "pnl_pct": pnl_pct,
        "exit_reason": reason,
        "status": "CLOSED",
    }
    state["trades"].append(trade)
    event_type = "SELL" if side == "LONG" else "COVER"
    event = {"time": _now(), "type": event_type, **trade, "cash_after": state["cash"]}
    _record_event(state, event)

    result = "profit" if pnl >= 0 else "loss"
    body = (
        f"INSTANT PAPER EXIT — the trader just closed this {side} position.\n\n"
        f"Side: {side}\n"
        f"Share: {symbol}\nExit price: ₹{price:,.2f}\nQuantity: {quantity}\n"
        f"Total amount received: ₹{proceeds:,.2f}\nOriginally invested: ₹{invested:,.2f}\n"
        f"Result: {result.upper()} ₹{abs(pnl):,.2f} ({pnl_pct:+.2f}%)\n"
        f"Reason: {reason}\nPaper cash after exit: ₹{state['cash']:,.2f}\n\n"
        f"No real broker order was placed."
    )
    ok, detail = send_email(
        f"[Trader] EXIT NOW: {symbol} | {result} ₹{abs(pnl):,.2f}",
        body,
    )
    event["email"] = {"sent": ok, "detail": detail}
    save_state(state)
    return event


def portfolio_summary(state: dict[str, Any], prices: dict[str, float]) -> dict[str, float]:
    market_value = 0.0
    unrealized = 0.0
    for symbol, position in state["open_positions"].items():
        price = float(prices.get(symbol, position["entry_price"]))
        quantity = int(position["quantity"])
        if str(position.get("side", "LONG")).upper() == "SHORT":
            value = float(position["amount_invested"]) + (
                float(position["entry_price"]) - price
            ) * quantity
        else:
            value = price * quantity
        market_value += value
        unrealized += value - float(position["amount_invested"])
    realized = sum(float(t.get("pnl", 0)) for t in state["trades"])
    equity = float(state["cash"]) + market_value
    return {
        "cash": round(float(state["cash"]), 2),
        "market_value": round(market_value, 2),
        "equity": round(equity, 2),
        "realized_pnl": round(realized, 2),
        "unrealized_pnl": round(unrealized, 2),
        "total_pnl": round(equity - float(state["initial_cash"]), 2),
    }


def today_pnl(state: dict[str, Any], prices: dict[str, float]) -> float:
    today = pd.Timestamp.now(tz=IST).strftime("%Y-%m-%d")
    realized = sum(
        float(trade.get("pnl", 0))
        for trade in state.get("trades", [])
        if trade.get("exit_time")
        and pd.Timestamp(trade["exit_time"]).tz_convert(IST).strftime("%Y-%m-%d") == today
    )
    unrealized = 0.0
    for symbol, position in state.get("open_positions", {}).items():
        price = float(prices.get(symbol, position["entry_price"]))
        quantity = int(position["quantity"])
        if str(position.get("side", "LONG")).upper() == "SHORT":
            unrealized += (float(position["entry_price"]) - price) * quantity
        else:
            unrealized += price * quantity - float(position["amount_invested"])
    return round(realized + unrealized, 2)


def _already_exited_this_move(state: dict[str, Any], symbol: str, sig: Any) -> bool:
    """True when we already closed this symbol today and the signal is still
    holding the same move, so re-entering would just repeat the trade we left."""
    entered_at = getattr(sig, "entered_at", None)
    if not entered_at:
        return False
    today = pd.Timestamp.now(tz=IST).strftime("%Y-%m-%d")
    for trade in reversed(state.get("trades", [])):
        if trade.get("symbol") != symbol or not trade.get("exit_time"):
            continue
        stamp = pd.Timestamp(trade["exit_time"]).tz_convert(IST)
        if stamp.strftime("%Y-%m-%d") != today:
            continue
        return entered_at <= stamp.strftime("%H:%M")
    return False


def run_cycle(
    state: dict[str, Any],
    signals: dict[str, Any],
    chart_data: dict[str, pd.DataFrame],
    enabled: bool,
    market_open: bool,
    budget_per_trade: float,
    max_positions: int,
    minimum_confidence: int,
    require_kite: bool,
    entry_start: time = ENTRY_START,
    entry_end: time = ENTRY_END,
    candidate_budgets: dict[str, float] | None = None,
    daily_profit_target: float | None = None,
    daily_loss_limit: float | None = None,
) -> list[dict[str, Any]]:
    """Exit existing positions, then enter the strongest eligible fresh signal."""
    events: list[dict[str, Any]] = []
    now = pd.Timestamp.now(tz=IST).time()

    # Exits run even if auto-entry has been switched off.
    for symbol in list(state["open_positions"]):
        if symbol not in signals or symbol not in chart_data:
            continue
        position = state["open_positions"][symbol]
        sig = signals[symbol]
        bar = chart_data[symbol].iloc[-1]
        fill = float(bar["close"])
        reason = None
        side = str(position.get("side", "LONG")).upper()
        stop_hit = (
            float(bar["low"]) <= float(position["stop"])
            if side == "LONG"
            else float(bar["high"]) >= float(position["stop"])
        )
        target_hit = (
            float(bar["high"]) >= float(position["target"])
            if side == "LONG"
            else float(bar["low"]) <= float(position["target"])
        )
        reversal = (
            sig.score <= -2
            if side == "LONG"
            else sig.score >= 2
        )
        if stop_hit:
            fill = float(position["stop"])
            reason = "Stop-loss hit"
        elif target_hit:
            fill = float(position["target"])
            reason = "Target hit"
        elif sig.what_to_do == "EXIT NOW" or reversal:
            reason = f"Signal reversal (score {sig.score})"
        elif now >= SQUARE_OFF:
            reason = "Intraday square-off at/after 15:15 IST"
        else:
            atr = float(bar["atr"]) if "atr" in bar and pd.notna(bar["atr"]) else 0.0
            if atr > 0:
                if side == "LONG":
                    position["stop"] = round(max(float(position["stop"]), fill - 1.5 * atr), 2)
                else:
                    position["stop"] = round(min(float(position["stop"]), fill + 1.5 * atr), 2)

        if reason:
            event = close_position(state, symbol, fill, reason)
            if event:
                events.append(event)

    prices = {symbol: float(sig.price) for symbol, sig in signals.items()}
    session_pnl = today_pnl(state, prices)
    target_hit = daily_profit_target is not None and session_pnl >= float(daily_profit_target)
    loss_hit = daily_loss_limit is not None and session_pnl <= -float(daily_loss_limit)
    if target_hit or loss_hit:
        reason = (
            f"Daily profit objective reached (₹{session_pnl:+,.2f})"
            if target_hit
            else f"Daily loss limit reached (₹{session_pnl:+,.2f})"
        )
        for symbol in list(state["open_positions"]):
            if symbol not in prices:
                continue
            event = close_position(state, symbol, prices[symbol], reason)
            if event:
                events.append(event)
        save_state(state)
        return events

    if not enabled or not market_open or not (entry_start <= now < entry_end):
        save_state(state)
        return events

    slots = max(0, int(max_positions) - len(state["open_positions"]))
    if slots == 0:
        save_state(state)
        return events

    candidates = []
    for symbol, sig in signals.items():
        if candidate_budgets is not None and symbol not in candidate_budgets:
            continue
        if symbol in state["open_positions"] or symbol not in chart_data:
            continue
        side = entry_side(sig)
        if side is None:
            continue
        timestamp = pd.Timestamp(chart_data[symbol].index[-1]).isoformat()
        signal_key = f"{symbol}|{timestamp}|{side}"
        if signal_key in state["seen_entries"]:
            continue
        if _already_exited_this_move(state, symbol, sig):
            continue
        if (
            sig.confidence >= minimum_confidence
            and (not require_kite or sig.data_source == "kite")
            and sig.stop_loss is not None
            and sig.target is not None
        ):
            candidates.append((sig.confidence, abs(sig.score), symbol, side, signal_key))

    candidates.sort(reverse=True)
    for _, _, symbol, side, signal_key in candidates[:slots]:
        sig = signals[symbol]
        event = open_position(
            state,
            symbol,
            side,
            sig.price,
            (
                float(candidate_budgets[symbol])
                if candidate_budgets is not None
                else budget_per_trade
            ),
            sig.stop_loss,
            sig.target,
            sig.confidence,
            sig.score,
            signal_key,
        )
        if event:
            events.append(event)

    save_state(state)
    return events
