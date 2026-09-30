"""Real-time open P&L from Kite WebSocket LTPs + cached position averages."""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import paper_trader

IST = timezone(timedelta(hours=5, minutes=30))
PNL_REFRESH_SECONDS = max(1, int(os.getenv("PNL_WS_REFRESH_SECONDS", "2")))
BROKER_LEGS_REFRESH_SECONDS = max(15, int(os.getenv("BROKER_LEGS_REFRESH_SECONDS", "45")))


def _now_ist() -> str:
    return datetime.now(tz=IST).strftime("%H:%M:%S IST")


def subscribe_open_instruments(state: dict[str, Any], extra_tokens: list[int] | None = None) -> None:
    import kite_ws

    tokens: list[int] = []
    for pos in (state.get("open_positions") or {}).values():
        tok = int(pos.get("instrument_token") or 0)
        if tok > 0:
            tokens.append(tok)
    if extra_tokens:
        tokens.extend(int(t) for t in extra_tokens if int(t) > 0)
    if not tokens:
        return
    kite_ws.start()
    kite_ws.subscribe_tokens(tokens)


def ws_prices_for_state(
    state: dict[str, Any],
    base_prices: dict[str, float] | None = None,
) -> tuple[dict[str, float], str]:
    """Merge WebSocket LTPs for open legs into a price map used by portfolio_summary."""
    import kite_ws

    prices = dict(base_prices or {})
    subscribe_open_instruments(state)
    sources: list[str] = []
    for symbol, pos in (state.get("open_positions") or {}).items():
        tok = int(pos.get("instrument_token") or 0)
        if tok <= 0:
            continue
        ltp = kite_ws.ltp_for_token(tok)
        if ltp:
            prices[symbol] = ltp
            sources.append("ws")
        elif symbol not in prices:
            prices[symbol] = float(pos.get("entry_price") or 0)
            sources.append("entry")
    age = kite_ws.last_tick_age_seconds()
    if age is not None and "ws" in sources:
        label = f"WebSocket LTP · last tick {age:.1f}s ago · {_now_ist()}"
    elif "ws" in sources:
        label = f"WebSocket LTP · {_now_ist()}"
    else:
        label = f"No ticks yet — using last refresh / entry · {_now_ist()}"
    return prices, label


def paper_open_leg_rows(state: dict[str, Any], prices: dict[str, float]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for symbol, pos in (state.get("open_positions") or {}).items():
        mark = paper_trader.position_mark(symbol, pos, prices)
        qty = int(pos.get("quantity") or 0)
        entry = float(pos.get("entry_price") or 0)
        invested = float(pos.get("amount_invested") or 0)
        pnl = round((mark - entry) * qty, 2)
        status = str(pos.get("order_status") or "Open")
        pnl_pct = round((pnl / invested) * 100, 2) if invested and status == "Executed" else None
        rows.append(
            {
                "Status": status,
                "Underlying": symbol,
                "Contract": pos.get("contract"),
                "Qty": qty,
                "Entry ₹": entry if status == "Executed" else None,
                "Mark ₹": round(mark, 2) if status == "Executed" else None,
                "Open P&L ₹": pnl if status == "Executed" else None,
                "P&L %": pnl_pct,
            }
        )
    return rows


def fetch_broker_open_legs(kite) -> list[dict[str, Any]]:
    import live_kite

    book = kite.positions()
    legs: list[dict[str, Any]] = []
    for row in book.get("net") or []:
        if not live_kite._is_tracked_position(row):
            continue
        qty = abs(int(row.get("quantity") or 0))
        if qty < 1:
            continue
        tok = int(row.get("instrument_token") or 0)
        legs.append(
            {
                "tradingsymbol": str(row.get("tradingsymbol") or ""),
                "instrument_token": tok,
                "quantity": qty,
                "average_price": float(row.get("average_price") or 0),
                "last_price": float(row.get("last_price") or 0),
            }
        )
    return legs


def broker_open_pnl_from_ws(legs: list[dict[str, Any]]) -> tuple[float, list[dict[str, Any]], str]:
    import kite_ws

    if not legs:
        return 0.0, [], "no open NFO legs"
    tokens = [int(l["instrument_token"]) for l in legs if int(l.get("instrument_token") or 0) > 0]
    kite_ws.start()
    kite_ws.subscribe_tokens(tokens)

    total = 0.0
    rows: list[dict[str, Any]] = []
    ws_hits = 0
    for leg in legs:
        qty = int(leg["quantity"])
        avg = float(leg["average_price"])
        tok = int(leg.get("instrument_token") or 0)
        ltp = kite_ws.ltp_for_token(tok) if tok else None
        if ltp:
            ws_hits += 1
        else:
            ltp = float(leg.get("last_price") or avg)
        pnl = round((ltp - avg) * qty, 2)
        total += pnl
        rows.append(
            {
                "Contract": leg["tradingsymbol"],
                "Qty": qty,
                "Avg ₹": round(avg, 2),
                "Mark ₹": round(ltp, 2),
                "Open P&L ₹": pnl,
            }
        )
    src = "WebSocket" if ws_hits else "REST last"
    age = kite_ws.last_tick_age_seconds()
    note = f"{src} marks"
    if age is not None and ws_hits:
        note += f" · last tick {age:.1f}s ago"
    return round(total, 2), rows, note
