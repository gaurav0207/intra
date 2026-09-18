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


def _record_event(state: dict[str, Any], event: dict[str, Any]) -> None:
    state["events"].append(event)
    state["events"] = state["events"][-1000:]


def open_long(
    state: dict[str, Any],
    symbol: str,
    price: float,
    budget: float,
    stop: float,
    target: float,
    confidence: int,
    score: int,
    signal_key: str,
) -> dict[str, Any] | None:
    price = round(float(price), 2)
    available = min(float(budget), float(state["cash"]))
    quantity = math.floor(available / price) if price > 0 else 0
    if quantity < 1 or symbol in state["open_positions"]:
        return None

    amount = round(quantity * price, 2)
    position = {
        "symbol": symbol,
        "side": "LONG",
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
    event = {"time": _now(), "type": "BUY", **position, "cash_after": state["cash"]}
    _record_event(state, event)

    body = (
        f"PAPER BUY\n\n"
        f"Share: {symbol}\nPrice: ₹{price:,.2f}\nQuantity: {quantity}\n"
        f"Amount invested: ₹{amount:,.2f}\nStop-loss: ₹{position['stop']:,.2f}\n"
        f"Target: ₹{position['target']:,.2f}\nConfidence: {confidence}%\n"
        f"Cash remaining: ₹{state['cash']:,.2f}\n\nNo real broker order was placed."
    )
    ok, detail = send_email(f"Paper BUY: {symbol} × {quantity} @ ₹{price:,.2f}", body)
    event["email"] = {"sent": ok, "detail": detail}
    save_state(state)
    return event


def close_long(
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
    proceeds = round(quantity * price, 2)
    invested = float(position["amount_invested"])
    pnl = round(proceeds - invested, 2)
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
    event = {"time": _now(), "type": "SELL", **trade, "cash_after": state["cash"]}
    _record_event(state, event)

    result = "profit" if pnl >= 0 else "loss"
    body = (
        f"PAPER SELL / EXIT\n\n"
        f"Share: {symbol}\nExit price: ₹{price:,.2f}\nQuantity: {quantity}\n"
        f"Total amount received: ₹{proceeds:,.2f}\nOriginally invested: ₹{invested:,.2f}\n"
        f"Result: {result.upper()} ₹{abs(pnl):,.2f} ({pnl_pct:+.2f}%)\n"
        f"Reason: {reason}\nPaper cash after exit: ₹{state['cash']:,.2f}\n\n"
        f"No real broker order was placed."
    )
    ok, detail = send_email(
        f"Paper EXIT: {symbol} | {result} ₹{abs(pnl):,.2f}",
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
        value = price * int(position["quantity"])
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
        if float(bar["low"]) <= float(position["stop"]):
            fill = float(position["stop"])
            reason = "Stop-loss hit"
        elif float(bar["high"]) >= float(position["target"]):
            fill = float(position["target"])
            reason = "Target hit"
        elif sig.what_to_do == "EXIT NOW" or sig.score <= -2:
            reason = f"Signal reversal (score {sig.score})"
        elif now >= SQUARE_OFF:
            reason = "Intraday square-off at/after 15:15 IST"
        else:
            atr = float(bar["atr"]) if "atr" in bar and pd.notna(bar["atr"]) else 0.0
            if atr > 0:
                position["stop"] = round(max(float(position["stop"]), fill - 1.5 * atr), 2)

        if reason:
            event = close_long(state, symbol, fill, reason)
            if event:
                events.append(event)

    if not enabled or not market_open or not (entry_start <= now < entry_end):
        save_state(state)
        return events

    slots = max(0, int(max_positions) - len(state["open_positions"]))
    if slots == 0:
        save_state(state)
        return events

    candidates = []
    for symbol, sig in signals.items():
        if symbol in state["open_positions"] or symbol not in chart_data:
            continue
        timestamp = pd.Timestamp(chart_data[symbol].index[-1]).isoformat()
        signal_key = f"{symbol}|{timestamp}|LONG"
        if signal_key in state["seen_entries"]:
            continue
        if (
            sig.what_to_do == "ENTER LONG NOW"
            and sig.confidence >= minimum_confidence
            and (not require_kite or sig.data_source == "kite")
            and sig.stop_loss is not None
            and sig.target is not None
        ):
            candidates.append((sig.confidence, sig.score, symbol, signal_key))

    candidates.sort(reverse=True)
    for _, _, symbol, signal_key in candidates[:slots]:
        sig = signals[symbol]
        event = open_long(
            state,
            symbol,
            sig.price,
            budget_per_trade,
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
