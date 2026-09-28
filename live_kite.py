"""Optional mirror of paper option fills to real Zerodha NFO orders.

Controlled by ``live_trading_enabled`` in the shared paper ledger (dashboard toggle).
Orders use MIS limit orders on NFO. Register your server static IP in the Kite app.
"""

from __future__ import annotations

import math
import os
from typing import Any

import kite_client

PRODUCT = os.getenv("LIVE_KITE_PRODUCT", "MIS").strip().upper() or "MIS"
DEFAULT_MAX_CAPITAL = 50_000.0


def is_enabled(state: dict[str, Any] | None) -> bool:
    if os.getenv("LIVE_KITE_HARD_DISABLE", "").strip().lower() in {"1", "true", "yes"}:
        return False
    if not state:
        return False
    return bool(state.get("live_trading_enabled", False))


def status_line(state: dict[str, Any] | None = None) -> str:
    if not is_enabled(state):
        return "Real Zerodha orders OFF — paper simulation only."
    cap = capital_limit()
    return (
        f"Real Zerodha orders ON — NFO {PRODUCT} limit orders, "
        f"max premium deployed ₹{cap:,.0f}."
    )


def capital_limit() -> float:
    raw = os.getenv("LIVE_KITE_MAX_CAPITAL", str(int(DEFAULT_MAX_CAPITAL))).strip()
    try:
        return max(1_000.0, float(raw))
    except ValueError:
        return DEFAULT_MAX_CAPITAL


def deployed_premium(state: dict[str, Any]) -> float:
    total = 0.0
    for pos in (state.get("open_positions") or {}).values():
        entry = pos.get("live_kite_entry")
        if not (isinstance(entry, dict) and entry.get("ok")):
            continue
        qty = int(pos.get("live_quantity") or pos.get("quantity") or 0)
        total += qty * float(pos.get("entry_price") or 0)
    return round(total, 2)


def remaining_capital(state: dict[str, Any]) -> float:
    return round(max(0.0, capital_limit() - deployed_premium(state)), 2)


def apply_live_cap(state: dict[str, Any], position: dict[str, Any]) -> tuple[bool, str]:
    """Shrink lots/qty so live premium stays within the Zerodha capital cap."""
    if not is_enabled(state):
        return True, ""
    before_lots = int(position["lots"])
    remaining = remaining_capital(state)
    premium = float(position["entry_price"])
    lot_size = int(position["lot_size"])
    lot_cost = premium * lot_size
    if lot_cost <= 0:
        return False, "invalid lot cost"
    max_lots = math.floor(remaining / lot_cost)
    lots = min(before_lots, max_lots)
    if lots < 1:
        return (
            False,
            f"live Zerodha cap ₹{capital_limit():,.0f} full "
            f"(₹{remaining:,.0f} free for new premium)",
        )
    position["lots"] = lots
    position["quantity"] = lots * lot_size
    position["amount_invested"] = round(lots * lot_cost, 2)
    if lots < before_lots:
        return True, f"scaled to {lots} lot(s) for ₹{capital_limit():,.0f} live cap"
    return True, ""


def _tick_round(price: float, tick: float = 0.05) -> float:
    return round(round(float(price) / tick) * tick, 2)


def _kite():
    token = kite_client.load_saved_token()
    if not token:
        raise RuntimeError("No Kite session — log in on the dashboard first.")
    return kite_client.make_kite(token)


def place_option_order(
    transaction_type: str,
    tradingsymbol: str,
    quantity: int,
    limit_price: float,
) -> dict[str, Any]:
    """Place a single NFO limit order. transaction_type: BUY or SELL."""
    kite = _kite()
    side = transaction_type.strip().upper()
    if side not in {"BUY", "SELL"}:
        raise ValueError(f"Invalid transaction_type {transaction_type!r}")
    qty = int(quantity)
    if qty < 1:
        raise ValueError("quantity must be positive")
    price = _tick_round(limit_price)
    order_id = kite.place_order(
        variety="regular",
        exchange="NFO",
        tradingsymbol=tradingsymbol,
        transaction_type=side,
        quantity=qty,
        product=PRODUCT,
        order_type="LIMIT",
        price=price,
        validity="DAY",
    )
    return {
        "ok": True,
        "order_id": str(order_id),
        "exchange": "NFO",
        "tradingsymbol": tradingsymbol,
        "transaction_type": side,
        "quantity": qty,
        "limit_price": price,
        "product": PRODUCT,
    }


def mirror_entry(state: dict[str, Any], position: dict[str, Any]) -> dict[str, Any]:
    contract = position.get("contract")
    if not contract:
        return {"ok": False, "error": "not an option position"}
    ok, note = apply_live_cap(state, position)
    if not ok:
        return {"ok": False, "error": note}
    qty = int(position["quantity"])
    try:
        result = place_option_order(
            "BUY",
            str(contract),
            qty,
            float(position["entry_price"]),
        )
        position["live_quantity"] = qty
        if note:
            result["cap_note"] = note
        return result
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def mirror_exit(position: dict[str, Any], exit_price: float) -> dict[str, Any]:
    contract = position.get("contract")
    if not contract:
        return {"ok": False, "error": "not an option position"}
    qty = int(position.get("live_quantity") or position.get("quantity") or 0)
    if qty < 1:
        return {"ok": False, "error": "no live quantity to exit"}
    try:
        return place_option_order(
            "SELL",
            str(contract),
            qty,
            float(exit_price),
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
