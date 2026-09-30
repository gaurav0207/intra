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

import live_kite
import trading_policy
import options
from signals import entry_side

STATE_PATH = Path(os.getenv("PAPER_TRADING_FILE", "paper_trades.json"))
IST = "Asia/Kolkata"
DEFAULT_INITIAL_CASH = float(os.getenv("PAPER_INITIAL_CASH", "50000"))


def default_initial_cash() -> float:
    return DEFAULT_INITIAL_CASH

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
        "auto_trading_enabled": True,
        "live_trading_enabled": False,
        "loss_halt_date": "",
        "loss_halt_reason": "",
        "updated_at": _now(),
    }


def load_state(initial_cash: float | None = None) -> dict[str, Any]:
    if initial_cash is None:
        initial_cash = default_initial_cash()
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
        state.setdefault("auto_trading_enabled", True)
        state.setdefault("live_trading_enabled", False)
        state.setdefault("loss_halt_date", "")
        state.setdefault("loss_halt_reason", "")
        state.setdefault("initial_cash", float(initial_cash))
        state.setdefault("cash", float(initial_cash))
        state["_loaded_at"] = _now()
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
    if STATE_PATH.exists():
        try:
            disk = json.loads(STATE_PATH.read_text())
            disk_at = str(disk.get("updated_at") or "")
            loaded_at = str(state.get("_loaded_at") or "")
            if disk_at and loaded_at and disk_at > loaded_at:
                for key in (
                    "cash",
                    "initial_cash",
                    "open_positions",
                    "trades",
                    "events",
                    "seen_entries",
                    "seen_intents",
                    "auto_trading_enabled",
                    "live_trading_enabled",
                    "loss_halt_date",
                    "loss_halt_reason",
                ):
                    if key in disk:
                        state[key] = disk[key]
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    state.pop("_loaded_at", None)
    state["updated_at"] = _now()
    temp = STATE_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    temp.replace(STATE_PATH)


def reset_state(initial_cash: float) -> dict[str, Any]:
    """Fresh paper book; keeps dashboard trading toggles; clears trade/event logs."""
    preserved: dict[str, Any] = {}
    if STATE_PATH.exists():
        try:
            old = json.loads(STATE_PATH.read_text())
            if isinstance(old, dict):
                for key in ("auto_trading_enabled", "live_trading_enabled"):
                    if key in old:
                        preserved[key] = old[key]
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    state = new_state(initial_cash)
    state.update(preserved)
    state["trades"] = []
    state["events"] = []
    state["seen_entries"] = []
    state["seen_intents"] = []
    state["open_positions"] = {}
    save_state(state)
    return state


def rebase_capital(target: float) -> dict[str, Any]:
    """Set cash and initial_cash to *target* when flat; otherwise only fix initial baseline."""
    target = round(float(target), 2)
    state = load_state()
    if state.get("open_positions"):
        state["initial_cash"] = target
        save_state(state)
        return state
    state["cash"] = target
    state["initial_cash"] = target
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


def option_plan_rows(
    state: dict[str, Any],
    signals: dict[str, Any],
    contracts: dict[str, Any],
    budgets: dict[str, float],
    chart_data: dict[str, Any] | None = None,
    minimum_confidence: int = 0,
    max_positions: int = 5,
    in_window: bool = True,
    kite_ok: bool = True,
    daily_profit_target: float | None = None,
    daily_loss_limit: float | None = None,
    prices: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Rows the dashboard and the will-invest email both use."""
    rows: list[dict[str, Any]] = []
    prices = prices or {}
    session_halt = session_entry_block_reason(
        state, prices, daily_profit_target, daily_loss_limit, chart_data
    )
    for symbol, contract in contracts.items():
        sig = signals.get(symbol)
        if sig is None or symbol in state["open_positions"]:
            continue
        if sig.confidence < minimum_confidence:
            continue
        if entry_side(sig) is None or _already_exited_this_move(state, symbol, sig):
            continue
        budget = float(budgets.get(symbol, 0))
        lot_cost = contract.cost_per_lot()
        lots = math.floor(min(budget, float(state["cash"])) / lot_cost) if lot_cost > 0 else 0
        amount = round(lots * lot_cost, 2) if lots else 0.0
        blockers = []
        if session_halt:
            blockers.append(session_halt)
        if chart_data and symbol in chart_data:
            timestamp = pd.Timestamp(chart_data[symbol].index[-1]).isoformat()
            signal_key = f"{symbol}|{timestamp}|{contract.option_type}"
            if signal_key in state.get("seen_entries", []):
                blockers.append("already paper-bought on this 5m bar")
        if not in_window:
            blockers.append(f"waiting for the {ENTRY_START:%H:%M}–{ENTRY_END:%H:%M} IST window")
        if not kite_ok:
            blockers.append("waiting for live Kite data")
        if not contract.mis_eligible():
            blockers.append(
                f"Zerodha would reject MIS (OI {contract.oi_lots:.0f} lots "
                f"< {options.MIN_MIS_OI_LOTS}) — no paper/live entry"
            )
        if lots < 1:
            blockers.append("premium × lot is above the reserved slot")
        if len(state["open_positions"]) >= int(max_positions):
            blockers.append(f"already at max {max_positions} open positions")
        status = "WILL BUY THIS OPTION" if not blockers else "WATCHING — " + "; ".join(blockers)
        row = {
            "Contract": contract.tradingsymbol,
            "Underlying": symbol,
            "Type": contract.option_type,
            "Premium": contract.premium,
            "Lots": lots,
            "Lot size": contract.lot_size,
            "Premium to pay": amount,
            "Stop": contract.stop,
            "Target": contract.target,
            "Conf %": sig.confidence,
            "Score": sig.score,
            "Status": status,
        }
        if chart_data and symbol in chart_data:
            row["_key"] = (
                f"{symbol}|{pd.Timestamp(chart_data[symbol].index[-1]).isoformat()}|"
                f"{contract.option_type}|INTENT"
            )
        rows.append(row)
    rows.sort(key=lambda r: -int(r["Conf %"]))
    return rows


def option_watch_rows(
    signals: dict[str, Any],
    contracts: dict[str, Any],
    minimum_confidence: int,
    regime: str,
) -> list[dict[str, Any]]:
    """When nothing is sized yet, show eligible underlyings and why they are not filling."""
    rows: list[dict[str, Any]] = []
    for symbol, sig in sorted(signals.items(), key=lambda item: -item[1].confidence):
        side = entry_side(sig)
        if side is None:
            continue
        contract = contracts.get(symbol)
        blockers: list[str] = []
        if sig.confidence < minimum_confidence:
            blockers.append(f"confidence {sig.confidence}% < {minimum_confidence}% ({regime})")
        if contract is None:
            blockers.append(
                f"no ATM {('PE' if side == 'SHORT' else 'CE')} "
                f"(DTE window or OI < {options.MIN_MIS_OI_LOTS} lots for MIS)"
            )
        elif contract.mis_eligible():
            blockers.append("waiting for adaptive sizing / next trader cycle")
        rows.append(
            {
                "Underlying": symbol,
                "Signal": sig.what_to_do,
                "Side": side,
                "Conf %": sig.confidence,
                "Contract": contract.tradingsymbol if contract else "—",
                "OI (lots)": contract.oi_lots if contract else "—",
                "Why not filling": "; ".join(blockers) if blockers else "eligible — check cash/slots",
            }
        )
        if len(rows) >= 10:
            break
    return rows


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
    candidate_contracts: dict[str, Any] | None = None,
    candidate_budgets: dict[str, float] | None = None,
    daily_loss_limit: float | None = None,
) -> list[tuple[bool, str]]:
    """Email what the trader is about to buy, without repeating the same setup."""
    sent_results: list[tuple[bool, str]] = []
    now = pd.Timestamp.now(tz=IST)
    in_window = bool(market_open and ENTRY_START <= now.time() < ENTRY_END)
    today = now.strftime("%Y-%m-%d")
    seen = list(state.get("seen_intents") or [])
    prices = {symbol: float(sig.price) for symbol, sig in signals.items()}
    rows = option_plan_rows(
        state,
        signals,
        candidate_contracts or {},
        candidate_budgets or {},
        chart_data=chart_data,
        minimum_confidence=minimum_confidence,
        max_positions=max_positions,
        in_window=in_window,
        kite_ok=bool(kite_ok),
        daily_profit_target=daily_profit_target,
        daily_loss_limit=daily_loss_limit,
        prices=prices,
    )
    state["last_option_plan"] = [
        {k: v for k, v in row.items() if not str(k).startswith("_")}
        for row in rows
    ]
    state["last_option_plan_at"] = _now()

    lines = []
    new_keys = []
    for row in rows:
        key = row.get("_key")
        if not key or key in seen:
            continue
        lines.append(
            f"- {row['Contract']}  ({row['Underlying']} {row['Type']})\n"
            f"  Premium ₹{row['Premium']:,.2f} · {row['Lots']} lot(s) × {row['Lot size']} "
            f"· pay ₹{row['Premium to pay']:,.2f} · conf {row['Conf %']}% · score {row['Score']}\n"
            f"  Stop ₹{row['Stop']} · Target ₹{row['Target']}\n"
            f"  {row['Status']}"
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
            "(sizes entries toward this; trading does not stop when reached)\n"
            f"Kite required: {'yes' if require_kite else 'no'} "
            f"(currently {'connected' if kite_ok else 'not connected'})\n\n"
            + (notes + "\n\n" if notes else "")
            +
            "It buys an ATM NFO call on ENTER LONG, or an ATM put on ENTER SHORT.\n"
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
            "The trader will buy these ATM NFO options (not cash shares).\n\n"
            + "\n".join(lines)
            + f"\n\nWindow: {ENTRY_START:%H:%M}–{ENTRY_END:%H:%M} IST · "
            f"Kite {'connected' if kite_ok else 'disconnected'}\n"
            f"Time: {_now()}\n"
            "A second email is sent the instant a paper BUY or EXIT fills.\n"
            "The same rows appear on the dashboard under Will buy next.\n"
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

    save_state(state)
    return sent_results


def _record_event(state: dict[str, Any], event: dict[str, Any]) -> None:
    state["events"].append(event)
    state["events"] = state["events"][-1000:]


def is_option_position(position: dict[str, Any]) -> bool:
    return bool(position.get("contract"))


def position_mark(symbol: str, position: dict[str, Any], prices: dict[str, float]) -> float:
    """Option positions must never be marked with the underlying cash price."""
    if not is_option_position(position):
        return float(prices.get(symbol, position["entry_price"]))
    entry = float(position["entry_price"])
    mark = prices.get(symbol)
    if mark is None:
        return entry
    mark = float(mark)
    # Underlying spots are orders of magnitude above a premium; reject those.
    if mark > max(entry * 8, entry + 50):
        return entry
    return mark


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
    contract: Any | None = None,
    spot_at_entry: float | None = None,
    underlying_stop: float | None = None,
    underlying_target: float | None = None,
) -> dict[str, Any] | None:
    if contract is None:
        return None
    side = str(contract.option_type).upper()
    price = round(float(contract.premium), 2)
    stop = float(contract.stop)
    target = float(contract.target)
    available = min(float(budget), float(state["cash"]))
    lot_cost = contract.cost_per_lot()
    lots = math.floor(available / lot_cost) if lot_cost > 0 else 0
    lots = trading_policy.cap_lots(lots)
    quantity = lots * int(contract.lot_size)
    if lots < 1 or symbol in state["open_positions"]:
        return None
    amount = round(quantity * price, 2)
    position = {
        "symbol": symbol,
        "side": side,
        "entry_time": _now(),
        "entry_price": price,
        "quantity": quantity,
        "lots": lots,
        "lot_size": int(contract.lot_size),
        "amount_invested": amount,
        "stop": round(float(stop), 2),
        "target": round(float(target), 2),
        "confidence": int(confidence),
        "entry_score": int(score),
        "signal_key": signal_key,
        "contract": contract.tradingsymbol,
        "strike": float(contract.strike),
        "expiry": contract.expiry,
        "instrument_token": int(contract.instrument_token),
        "underlying_side": "LONG" if side == "CE" else "SHORT",
    }
    if spot_at_entry is not None:
        position["underlying_spot_entry"] = round(float(spot_at_entry), 2)
    if underlying_stop is not None:
        position["underlying_stop"] = round(float(underlying_stop), 2)
    if underlying_target is not None:
        position["underlying_target"] = round(float(underlying_target), 2)

    broker_ok, broker_note = live_kite.validate_entry(state, position)
    if not broker_ok:
        return None

    live_note = ""
    live_result: dict[str, Any] | None = None
    if live_kite.is_enabled(state):
        live_result = live_kite.mirror_entry(state, position)
        if not live_result.get("ok"):
            send_email(
                "[Trader] LIVE order FAILED (paper entry cancelled)",
                f"Contract: {contract.tradingsymbol}\n"
                f"Error: {live_result.get('error')}\n\n"
                "Paper book was NOT updated because live mirroring is strict.",
            )
            return None
        position["live_kite_entry"] = live_result
        position["order_status"] = "Open"
        cap_note = live_result.get("cap_note") or broker_note or ""
        live_note = (
            f"\n\nLIVE ZERODHA ORDER PLACED\n"
            f"Order ID: {live_result.get('order_id')}\n"
            f"Limit: ₹{live_result.get('limit_price'):,.2f} · qty {live_result.get('quantity')}\n"
            + (f"{cap_note}\n" if cap_note else "")
        )
    else:
        position["order_status"] = "Executed"

    lots = int(position["lots"])
    quantity = int(position["quantity"])
    amount = round(float(position["amount_invested"]), 2)

    broker_line = (
        "Mirrored to your Zerodha account (see LIVE section below)."
        if live_kite.is_enabled(state)
        else "Paper only — entry passed Zerodha MIS/OI and premium-cap checks."
    )
    body = (
        f"INSTANT PAPER OPTION BUY — debit only, no cash shares.\n\n"
        f"Underlying: {symbol}\n"
        f"Contract: {contract.tradingsymbol}\n"
        f"Type: {side}  Strike: {contract.strike:g}  Expiry: {contract.expiry}\n"
        f"Premium: ₹{price:,.2f}\nLots: {lots} × {contract.lot_size} = {quantity} qty\n"
        f"Premium paid: ₹{amount:,.2f}\nStop: ₹{position['stop']:,.2f}\n"
        f"Target: ₹{position['target']:,.2f}\nConfidence: {confidence}%\n"
        f"Cash remaining: ₹{round(float(state['cash']) - amount, 2):,.2f}\n"
        f"{broker_line}{live_note}"
    )

    state["cash"] = round(float(state["cash"]) - amount, 2)
    state["open_positions"][symbol] = position
    state["seen_entries"].append(signal_key)
    state["seen_entries"] = state["seen_entries"][-500:]
    event = {"time": _now(), "type": "BUY", **position, "cash_after": state["cash"]}
    if live_result:
        event["live_kite"] = live_result
    _record_event(state, event)
    subject = f"[Trader] BUY {side}: {contract.tradingsymbol} × {lots} @ ₹{price:,.2f}"
    if live_result:
        subject = f"[Trader] LIVE+BUY {side}: {contract.tradingsymbol} × {lots}"
    ok, detail = send_email(subject, body)
    event["email"] = {"sent": ok, "detail": detail}
    save_state(state)
    return event


def manual_exit_positions(
    symbols: list[str] | None,
    prices: dict[str, float],
    initial_cash: float,
    reason: str = "Manual exit from dashboard",
) -> list[dict[str, Any]]:
    """Close one or all open paper positions at current marks (no broker order)."""
    state = load_state(initial_cash)
    targets = list(symbols) if symbols else list(state["open_positions"])
    events: list[dict[str, Any]] = []
    for symbol in targets:
        if symbol not in state["open_positions"]:
            continue
        mark = position_mark(symbol, state["open_positions"][symbol], prices)
        event = close_position(state, symbol, mark, reason)
        if event:
            events.append(event)
    return events


def _apply_entry_fill(state: dict[str, Any], position: dict[str, Any], order: dict[str, Any]) -> None:
    """Replace the assumed limit with Zerodha's average fill, once."""
    if position.get("fill_applied"):
        return
    avg = float(order.get("average_price") or 0)
    filled = int(order.get("filled_quantity") or 0)
    if avg <= 0 or filled <= 0:
        return
    old_amount = float(position.get("amount_invested") or 0)
    new_amount = round(avg * filled, 2)
    state["cash"] = round(float(state["cash"]) + old_amount - new_amount, 2)
    position["entry_price"] = round(avg, 2)
    position["quantity"] = filled
    lot = int(position.get("lot_size") or 0)
    if lot > 0:
        position["lots"] = max(1, filled // lot)
    position["amount_invested"] = new_amount
    position["fill_applied"] = True


def sync_with_broker(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Make the paper book follow Zerodha order status when live mirroring is on.

    Unfilled entry orders stay Open and are not treated as a position.
    A cancelled or rejected entry is removed and its reserved cash is returned.
    An exit stays on the book until Kite reports the sell as Executed, then the
    paper trade is Closed at the average fill.
    """
    events: list[dict[str, Any]] = []
    if not live_kite.is_enabled(state):
        for position in state.get("open_positions", {}).values():
            position.setdefault("order_status", "Open")
        return events

    book = live_kite.fetch_broker_book()
    if book is None:
        return events
    orders, net_qty = book

    def _drop_unfilled(symbol: str, position: dict[str, Any], reason: str) -> None:
        state["cash"] = round(float(state["cash"]) + float(position.get("amount_invested") or 0), 2)
        state["open_positions"].pop(symbol, None)
        events.append(
            {
                "time": _now(),
                "type": "ORDER_CLOSED",
                "symbol": symbol,
                "contract": position.get("contract"),
                "order_status": "Closed",
                "reason": reason,
            }
        )

    for symbol in list(state.get("open_positions", {})):
        position = state["open_positions"][symbol]
        contract = str(position.get("contract") or "")
        entry = position.get("live_kite_entry") or {}
        order_id = str(entry.get("order_id") or "")
        order = orders.get(order_id) if order_id else None
        phase = live_kite.order_phase(order) if order else None
        broker_qty = int(net_qty.get(contract, 0)) if contract else 0
        if phase == "Closed":
            _drop_unfilled(
                symbol,
                position,
                f"Zerodha {order.get('status') if order else 'cancelled'} — entry not filled",
            )
            continue
        if phase == "Open":
            position["order_status"] = "Open"
            continue
        if phase == "Executed" and broker_qty > 0:
            _apply_entry_fill(state, position, order or {})
            position["order_status"] = "Executed"
        elif broker_qty > 0 and phase is None:
            position["order_status"] = "Executed"
        elif not position.get("fill_applied"):
            # Not an open order and not a long position on Zerodha.
            # Yesterday's cancelled order is absent from today's order book.
            _drop_unfilled(
                symbol,
                position,
                "Not open on Zerodha (cancelled or never filled)",
            )
            continue
        else:
            exit_meta = position.get("live_kite_exit") or {}
            exit_order = orders.get(str(exit_meta.get("order_id") or ""))
            avg = 0.0
            if exit_order and live_kite.order_phase(exit_order) == "Executed":
                avg = float(exit_order.get("average_price") or 0)
            if avg <= 0 and contract:
                avg = live_kite.completed_sell_average(orders, contract)
            if avg <= 0 and exit_order and live_kite.order_phase(exit_order) == "Open":
                position["exit_pending"] = True
                position["order_status"] = "Open"
                continue
            position["order_status"] = "Closed"
            event = close_position(
                state,
                symbol,
                avg if avg > 0 else float(position.get("entry_price") or 0),
                str(position.get("pending_exit_reason") or "Zerodha exit filled"),
                skip_broker=True,
                save=False,
            )
            if event:
                events.append(event)
            continue

        exit_meta = position.get("live_kite_exit") or {}
        exit_id = str(exit_meta.get("order_id") or "")
        if not exit_id:
            continue
        exit_order = orders.get(exit_id)
        exit_phase = live_kite.order_phase(exit_order)
        if exit_phase == "Executed" and exit_order:
            avg = float(exit_order.get("average_price") or exit_meta.get("limit_price") or 0)
            reason = str(position.get("pending_exit_reason") or "Zerodha exit executed")
            position["exit_pending"] = False
            event = close_position(
                state,
                symbol,
                avg or float(position["entry_price"]),
                reason,
                skip_broker=True,
                save=False,
            )
            if event:
                events.append(event)
        elif exit_phase == "Closed":
            position["exit_pending"] = False
            position.pop("live_kite_exit", None)
            position.pop("pending_exit_reason", None)
            if position.get("fill_applied"):
                position["order_status"] = "Executed"
        else:
            position["exit_pending"] = True
            position["order_status"] = "Open"

    if events or state.get("open_positions"):
        save_state(state)
    return events


def close_position(
    state: dict[str, Any],
    symbol: str,
    price: float,
    reason: str,
    *,
    skip_broker: bool = False,
    save: bool = True,
) -> dict[str, Any] | None:
    position = state["open_positions"].get(symbol)
    if not position:
        return None

    price = round(float(price), 2)
    live_result: dict[str, Any] | None = None
    if live_kite.is_enabled(state) and position.get("contract") and not skip_broker:
        if position.get("order_status") == "Open" and not position.get("exit_pending"):
            return None
        live_result = live_kite.mirror_exit(position, price)
        if live_result.get("ok"):
            position["live_kite_exit"] = live_result
            orders = live_kite.fetch_orders_by_id()
            exit_order = orders.get(str(live_result.get("order_id")))
            if live_kite.order_phase(exit_order) != "Executed":
                position["exit_pending"] = True
                position["pending_exit_reason"] = reason
                position["order_status"] = "Open"
                save_state(state)
                return {
                    "time": _now(),
                    "type": "EXIT_WORKING",
                    "symbol": symbol,
                    "contract": position.get("contract"),
                    "order_status": "Open",
                    "order_id": live_result.get("order_id"),
                    "limit_price": live_result.get("limit_price"),
                }
            if exit_order and float(exit_order.get("average_price") or 0) > 0:
                price = round(float(exit_order["average_price"]), 2)
        if live_result.get("ok") and live_result.get("limit_price") and price <= 0:
            price = round(float(live_result["limit_price"]), 2)
        if not live_result.get("ok"):
            send_email(
                "[Trader] LIVE EXIT FAILED — paper position kept open",
                f"Contract: {position.get('contract') or symbol}\n"
                f"Error: {live_result.get('error')}\n\n"
                "Paper was NOT closed so the books stay aligned with Zerodha.",
            )
            return {
                "time": _now(),
                "type": "EXIT_FAILED",
                "symbol": symbol,
                "contract": position.get("contract"),
                "error": live_result.get("error"),
            }

    position = state["open_positions"].pop(symbol, None)
    if not position:
        return None
    quantity = int(position["quantity"])
    side = str(position.get("side", "LONG")).upper()
    invested = float(position["amount_invested"])
    if is_option_position(position) or side in {"CE", "PE"}:
        pnl = round((price - float(position["entry_price"])) * quantity, 2)
    elif side == "SHORT":
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
        "order_status": "Closed",
    }
    state["trades"].append(trade)
    event_type = "SELL" if side == "LONG" else "COVER"
    event = {"time": _now(), "type": event_type, **trade, "cash_after": state["cash"]}
    _record_event(state, event)

    live_note = ""
    if live_result and live_result.get("ok"):
        trade["live_kite_exit"] = live_result
        live_note = (
            f"\n\nLIVE ZERODHA EXIT ORDER (LIMIT — not market)\n"
            f"Order ID: {live_result.get('order_id')}\n"
            f"Limit: ₹{live_result.get('limit_price'):,.2f}\n"
            f"Qty: {live_result.get('quantity')} · {live_result.get('product', 'MIS')}\n"
            f"{live_result.get('pricing_note', '')}\n"
        )

    result = "profit" if pnl >= 0 else "loss"
    broker_line = (
        "Exit mirrored to Zerodha when live mirroring is on."
        if live_kite.is_enabled(state)
        else "No real broker order was placed."
    )
    body = (
        f"INSTANT PAPER EXIT — the trader just closed this {side} position.\n\n"
        f"Side: {side}\n"
        f"Share: {symbol}\nExit price: ₹{price:,.2f}\nQuantity: {quantity}\n"
        f"Total amount received: ₹{proceeds:,.2f}\nOriginally invested: ₹{invested:,.2f}\n"
        f"Result: {result.upper()} ₹{abs(pnl):,.2f} ({pnl_pct:+.2f}%)\n"
        f"Reason: {reason}\nPaper cash after exit: ₹{state['cash']:,.2f}\n\n"
        f"{broker_line}{live_note}"
    )
    ok, detail = send_email(
        f"[Trader] EXIT NOW: {symbol} | {result} ₹{abs(pnl):,.2f}",
        body,
    )
    event["email"] = {"sent": ok, "detail": detail}
    if live_result:
        event["live_kite"] = live_result
    if save:
        save_state(state)
    return event


def portfolio_summary(state: dict[str, Any], prices: dict[str, float]) -> dict[str, float]:
    market_value = 0.0
    unrealized = 0.0
    for symbol, position in state["open_positions"].items():
        price = position_mark(symbol, position, prices)
        quantity = int(position["quantity"])
        if is_option_position(position) or str(position.get("side", "")).upper() in {"CE", "PE"}:
            value = price * quantity
        elif str(position.get("side", "LONG")).upper() == "SHORT":
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


def _today_key() -> str:
    return pd.Timestamp.now(tz=IST).strftime("%Y-%m-%d")


def arm_loss_halt(state: dict[str, Any], reason: str) -> None:
    """Latch: no new entries for the rest of this IST calendar day."""
    state["loss_halt_date"] = _today_key()
    state["loss_halt_reason"] = reason


def loss_halt_active(state: dict[str, Any]) -> str | None:
    if state.get("loss_halt_date") == _today_key():
        return str(state.get("loss_halt_reason") or "daily loss stop already hit today")
    return None


def trading_halt_reason(
    state: dict[str, Any],
    prices: dict[str, float],
    daily_profit_target: float | None,
    daily_loss_limit: float | None,
) -> str | None:
    """Why new option entries are blocked for the rest of this session.

    The daily profit figure is a sizing objective only — hitting it does not halt trading.
    """
    latched = loss_halt_active(state)
    if latched:
        return latched
    session_pnl = today_pnl(state, prices)
    if daily_loss_limit is not None and session_pnl <= -float(daily_loss_limit):
        return (
            f"daily loss limit reached (today ₹{session_pnl:+,.0f}; "
            f"limit ₹{-float(daily_loss_limit):,.0f})"
        )
    return None


def session_entry_block_reason(
    state: dict[str, Any],
    prices: dict[str, float],
    daily_profit_target: float | None,
    daily_loss_limit: float | None,
    chart_data: dict[str, pd.DataFrame] | None = None,
) -> str | None:
    halt = trading_halt_reason(state, prices, daily_profit_target, daily_loss_limit)
    if halt and "daily loss limit reached" in halt:
        arm_loss_halt(state, halt)
    if halt:
        return halt
    if chart_data:
        pe_block = trading_policy.block_new_pe_reason(chart_data)
        if pe_block:
            return pe_block
    return None


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
        price = position_mark(symbol, position, prices)
        quantity = int(position["quantity"])
        if is_option_position(position) or str(position.get("side", "")).upper() in {"CE", "PE"}:
            unrealized += (price - float(position["entry_price"])) * quantity
        elif str(position.get("side", "LONG")).upper() == "SHORT":
            unrealized += (float(position["entry_price"]) - price) * quantity
        else:
            unrealized += price * quantity - float(position["amount_invested"])
    return round(realized + unrealized, 2)


def _underlying_side_for_option(position: dict[str, Any]) -> str:
    stored = position.get("underlying_side")
    if stored:
        return str(stored).upper()
    side = str(position.get("side", "")).upper()
    return "LONG" if side == "CE" else "SHORT"


def _score_reverses_option(underlying_side: str, score: int) -> bool:
    threshold = trading_policy.option_reversal_score()
    if underlying_side == "LONG":
        return score <= -threshold
    return score >= threshold


def _bar_hits_underlying_levels(
    bar: pd.Series, underlying_side: str, stop: float, target: float
) -> tuple[bool, bool]:
    high = float(bar["high"])
    low = float(bar["low"])
    if underlying_side == "LONG":
        return low <= stop, high >= target
    return high >= stop, low <= target


def _trail_underlying_levels_from_signal(position: dict[str, Any], sig: Any) -> None:
    """Keep option exits aligned with the simulator's trailed spot stop/target."""
    u_side = _underlying_side_for_option(position)
    if not sig.in_trade or str(sig.side).upper() != u_side:
        return
    if sig.stop_loss is not None:
        position["underlying_stop"] = round(float(sig.stop_loss), 2)
    if sig.target is not None:
        position["underlying_target"] = round(float(sig.target), 2)


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
        # Cash leftovers flattened when the book moved to NFO must not
        # block a later option entry on the same underlying.
        if not trade.get("contract"):
            continue
        stamp = pd.Timestamp(trade["exit_time"]).tz_convert(IST)
        if stamp.strftime("%Y-%m-%d") != today:
            continue
        return entered_at <= stamp.strftime("%H:%M")
    return False


def _loss_reentry_blocked(state: dict[str, Any], symbol: str) -> bool:
    """Skip re-entry after today's losing option exit on this underlying."""
    today = pd.Timestamp.now(tz=IST).strftime("%Y-%m-%d")
    for trade in reversed(state.get("trades", [])):
        if trade.get("symbol") != symbol or not trade.get("exit_time"):
            continue
        if not trade.get("contract"):
            continue
        stamp = pd.Timestamp(trade["exit_time"]).tz_convert(IST)
        if stamp.strftime("%Y-%m-%d") != today:
            continue
        return float(trade.get("pnl", 0)) < 0
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
    candidate_contracts: dict[str, Any] | None = None,
    option_marks: dict[str, float] | None = None,
    daily_profit_target: float | None = None,
    daily_loss_limit: float | None = None,
) -> list[dict[str, Any]]:
    """Exit existing positions, then enter the strongest eligible fresh signal."""
    events: list[dict[str, Any]] = []
    events.extend(sync_with_broker(state))
    now = pd.Timestamp.now(tz=IST).time()
    candidate_contracts = candidate_contracts or {}
    option_marks = option_marks or {}

    for symbol in list(state["open_positions"]):
        position = state["open_positions"][symbol]
        if is_option_position(position):
            continue
        event = close_position(
            state,
            symbol,
            float(position["entry_price"]),
            "Switched paper book from cash shares to NFO options (flat at entry)",
        )
        if event:
            events.append(event)

    # Exits run even if auto-entry has been switched off.
    for symbol in list(state["open_positions"]):
        if symbol not in signals:
            continue
        position = state["open_positions"][symbol]
        if position.get("exit_pending") or position.get("order_status") == "Open":
            continue
        sig = signals[symbol]
        option = is_option_position(position)
        bar = chart_data[symbol].iloc[-1] if symbol in chart_data else None
        reason = None
        if option:
            mark = float(option_marks.get(symbol, position["entry_price"]))
            fill = mark
            entry_px = float(position["entry_price"])
            take_profit_px = round(entry_px * (1 + trading_policy.option_take_profit_fraction()), 2)
            # The contract target can be +50%. Exit at the smaller gain instead of waiting for a stop.
            if take_profit_px < float(position.get("target") or take_profit_px):
                position["target"] = take_profit_px
            arm_px = round(entry_px * (1 + trading_policy.option_breakeven_arm_fraction()), 2)
            loss_stop = round(entry_px * (1 - trading_policy.option_stop_fraction()), 2)
            # Tighten a 35% contract stop down to the premium stop. Never loosen it.
            if entry_px > 0 and float(position["stop"]) < loss_stop:
                position["stop"] = loss_stop
            if entry_px > 0 and mark >= arm_px:
                locked = round(entry_px, 2)
                if float(position["stop"]) < locked:
                    position["stop"] = locked
            prem_stop = mark <= float(position["stop"])
            prem_target = mark >= float(position["target"])
            if prem_target and mark >= entry_px:
                pct = (mark / entry_px - 1) * 100 if entry_px else 0
                reason = f"Small profit taken (+{pct:.1f}% premium)"
            elif prem_stop and abs(mark - entry_px) <= max(0.05, entry_px * 0.01):
                reason = "Breakeven stop (gave back a small profit)"
            elif prem_stop:
                pct = (mark / entry_px - 1) * 100 if entry_px else 0
                reason = f"Premium stop hit ({pct:.1f}% premium)"
            if reason is None and _score_reverses_option(
                _underlying_side_for_option(position), int(sig.score)
            ):
                reason = f"Signal reversal (score {sig.score})"
            elif reason is None and sig.what_to_do == "EXIT NOW":
                closed = getattr(sig, "closed_side", None)
                if str(closed or "").upper() == _underlying_side_for_option(position):
                    reason = sig.exit_reason or "Underlying setup exited"
            elif reason is None and now >= SQUARE_OFF:
                reason = "Intraday square-off at/after 15:15 IST"
        else:
            if bar is None:
                continue
            fill = float(bar["close"])
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
            elif sig.what_to_do == "EXIT NOW":
                reason = sig.exit_reason or "Underlying setup exited"
            elif reversal:
                reason = f"Signal reversal (score {sig.score})"
            elif now >= SQUARE_OFF:
                reason = "Intraday square-off at/after 15:15 IST"

        if reason:
            event = close_position(state, symbol, fill, reason)
            if event:
                events.append(event)

    prices = {symbol: float(sig.price) for symbol, sig in signals.items()}
    prices.update(option_marks)
    session_pnl = today_pnl(state, prices)
    halt = trading_halt_reason(state, prices, daily_profit_target, daily_loss_limit)
    if halt:
        arm_loss_halt(state, halt)
        reason = halt[0].upper() + halt[1:]
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

    if trading_policy.entry_block_reason(state):
        save_state(state)
        return events

    entry_halt = session_entry_block_reason(
        state,
        prices,
        daily_profit_target,
        daily_loss_limit,
        chart_data,
    )
    if entry_halt:
        save_state(state)
        return events

    pe_block = trading_policy.block_new_pe_reason(chart_data)
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
        contract = candidate_contracts.get(symbol)
        if contract is None:
            continue
        if pe_block and str(contract.option_type).upper() == "PE":
            continue
        timestamp = pd.Timestamp(chart_data[symbol].index[-1]).isoformat()
        signal_key = f"{symbol}|{timestamp}|{contract.option_type}"
        if signal_key in state["seen_entries"]:
            continue
        if _already_exited_this_move(state, symbol, sig):
            continue
        if _loss_reentry_blocked(state, symbol):
            continue
        if sig.confidence >= minimum_confidence:
            candidates.append((sig.confidence, abs(sig.score), symbol, side, signal_key))

    candidates.sort(reverse=True)
    for _, _, symbol, side, signal_key in candidates[:slots]:
        sig = signals[symbol]
        contract = candidate_contracts.get(symbol)
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
            contract.stop if contract else sig.stop_loss,
            contract.target if contract else sig.target,
            sig.confidence,
            sig.score,
            signal_key,
            contract=contract,
            spot_at_entry=sig.price,
            underlying_stop=sig.stop_loss,
            underlying_target=sig.target,
        )
        if event:
            events.append(event)

    save_state(state)
    return events
