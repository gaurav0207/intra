"""Optional mirror of paper option fills to real Zerodha NFO orders.

Controlled by ``live_trading_enabled`` in the shared paper ledger (dashboard toggle).
Orders use MIS limit orders on NFO. Register your server static IP in the Kite app.
"""

from __future__ import annotations

import math
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import kite_client
import options

IST = timezone(timedelta(hours=5, minutes=30))

PRODUCT = os.getenv("LIVE_KITE_PRODUCT", "MIS").strip().upper() or "MIS"
DEFAULT_MAX_CAPITAL = 50_000.0
MARGIN_CACHE_SECONDS = 45
# When exiting, limit SELL at bid (or LTP minus this) — never market (blocked on illiquid stock options).
EXIT_SLIPPAGE_PCT = max(0.0, min(0.25, float(os.getenv("LIVE_EXIT_SLIPPAGE_PCT", "0.05"))))
_margin_cache: tuple[float, float] | None = None  # (monotonic time, live_balance)


def is_enabled(state: dict[str, Any] | None) -> bool:
    if os.getenv("LIVE_KITE_HARD_DISABLE", "").strip().lower() in {"1", "true", "yes"}:
        return False
    if not state:
        return False
    return bool(state.get("live_trading_enabled", False))


def status_line(state: dict[str, Any] | None = None) -> str:
    if not is_enabled(state):
        return "Real Zerodha orders OFF — paper simulation only."
    snap = fund_snapshot(state)
    src = "Zerodha" if snap.get("source") == "kite" else "configured cap"
    return (
        f"Real Zerodha orders ON — NFO {PRODUCT} limit orders, "
        f"₹{snap['free']:,.0f} free ({src})."
    )


def optional_capital_ceiling() -> float | None:
    raw = os.getenv("LIVE_KITE_MAX_CAPITAL", "").strip()
    if not raw:
        return None
    try:
        return max(1_000.0, float(raw))
    except ValueError:
        return None


def static_capital_limit() -> float:
    ceiling = optional_capital_ceiling()
    if ceiling is not None:
        return ceiling
    return DEFAULT_MAX_CAPITAL


def invalidate_margin_cache() -> None:
    global _margin_cache
    _margin_cache = None


def fetch_equity_live_balance(kite, *, force: bool = False) -> float:
    """Cash available for new MIS/F&O trades (equity segment)."""
    global _margin_cache
    now = time.monotonic()
    if not force and _margin_cache and now - _margin_cache[0] < MARGIN_CACHE_SECONDS:
        return _margin_cache[1]
    payload = kite.margins()
    equity = payload.get("equity") or {}
    available = equity.get("available") or {}
    live = float(available.get("live_balance") or 0)
    cash = float(available.get("cash") or 0)
    balance = live if live > 0 else cash
    _margin_cache = (now, balance)
    return balance


def fund_snapshot(state: dict[str, Any] | None, *, force: bool = False) -> dict[str, Any]:
    """Cap / deployed / free for the sidebar (Kite live balance when mirroring)."""
    deployed = deployed_premium(state or {})
    if state and is_enabled(state):
        try:
            balance = fetch_equity_live_balance(_kite(), force=force)
            free = balance
            ceiling = optional_capital_ceiling()
            if ceiling is not None:
                free = min(free, max(0.0, ceiling - deployed))
            cap = free + deployed
            if ceiling is not None:
                cap = min(cap, ceiling)
            return {
                "cap": round(cap, 2),
                "deployed": round(deployed, 2),
                "free": round(max(0.0, free), 2),
                "source": "kite",
            }
        except Exception:
            pass
    cap = static_capital_limit()
    free = max(0.0, cap - deployed)
    return {
        "cap": round(cap, 2),
        "deployed": round(deployed, 2),
        "free": round(free, 2),
        "source": "config",
    }


def capital_limit(state: dict[str, Any] | None = None) -> float:
    return fund_snapshot(state)["cap"]


def deployed_premium(state: dict[str, Any]) -> float:
    """Premium tied up in open option legs (paper book = what Zerodha would mirror)."""
    total = 0.0
    for pos in (state.get("open_positions") or {}).values():
        if not pos.get("contract"):
            continue
        qty = int(pos.get("quantity") or 0)
        total += qty * float(pos.get("entry_price") or 0)
    return round(total, 2)


def remaining_capital(state: dict[str, Any]) -> float:
    return fund_snapshot(state)["free"]


def _is_tracked_position(row: dict[str, Any]) -> bool:
    return (
        str(row.get("exchange") or "").upper() == "NFO"
        and str(row.get("product") or "").upper() == PRODUCT
    )


def net_long_position(kite, tradingsymbol: str) -> dict[str, Any] | None:
    """Open long leg on Zerodha (any tracked product) for square-off."""
    symbol = tradingsymbol.strip().upper()
    book = kite.positions()
    for row in book.get("net") or []:
        if str(row.get("tradingsymbol") or "").upper() != symbol:
            continue
        if str(row.get("exchange") or "").upper() != "NFO":
            continue
        qty = int(row.get("quantity") or 0)
        if qty <= 0:
            continue
        product = str(row.get("product") or PRODUCT).upper()
        if product != PRODUCT:
            continue
        return row
    return None


def zerodha_portfolio_snapshot(
    state: dict[str, Any] | None,
    *,
    force_refresh: bool = False,
) -> dict[str, Any] | None:
    """Live balances and NFO positions from Kite (for dashboard comparison)."""
    if not state or not is_enabled(state):
        return None
    try:
        if force_refresh:
            invalidate_margin_cache()
        kite = _kite()
        margins = kite.margins().get("equity") or {}
        available = margins.get("available") or {}
        utilised = margins.get("utilised") or {}
        cash = float(available.get("live_balance") or available.get("cash") or 0)
        net_equity = float(margins.get("net") or 0)

        book = kite.positions()
        net_rows = [p for p in (book.get("net") or []) if int(p.get("quantity") or 0) != 0]
        day_rows = [p for p in (book.get("day") or []) if _is_tracked_position(p)]

        open_rows = [p for p in net_rows if _is_tracked_position(p)]
        invested = 0.0
        market_value = 0.0
        open_pnl = 0.0
        positions: list[dict[str, Any]] = []
        for p in open_rows:
            qty = abs(int(p["quantity"]))
            avg = float(p.get("average_price") or 0)
            last = float(p.get("last_price") or avg)
            pnl = float(p.get("pnl") if p.get("pnl") is not None else p.get("m2m") or 0)
            cost = avg * qty
            invested += cost
            market_value += last * qty
            open_pnl += pnl
            positions.append(
                {
                    "Contract": p.get("tradingsymbol"),
                    "Qty": qty,
                    "Avg ₹": round(avg, 2),
                    "LTP ₹": round(last, 2),
                    "Cost ₹": round(cost, 2),
                    "M2M P&L ₹": round(pnl, 2),
                    "Product": p.get("product"),
                }
            )

        day_pnl = sum(float(p.get("pnl") or 0) for p in day_rows)
        closed_day = sum(
            float(p.get("pnl") or 0)
            for p in day_rows
            if int(p.get("quantity") or 0) == 0
        )

        paper_by_contract = {
            str(pos.get("contract")): pos
            for pos in (state.get("open_positions") or {}).values()
            if pos.get("contract")
        }
        for row in positions:
            paper = paper_by_contract.get(str(row["Contract"]))
            if paper:
                entry = paper.get("live_kite_entry") or {}
                row["Paper contract"] = paper.get("contract")
                row["Zerodha order"] = entry.get("order_id") or "—"

        return {
            "equity": round(net_equity, 2),
            "cash": round(cash, 2),
            "invested_value": round(invested, 2),
            "market_value": round(market_value, 2),
            "open_pnl": round(open_pnl, 2),
            "realized_pnl_today": round(closed_day, 2),
            "day_pnl": round(day_pnl, 2),
            "utilised_debits": round(float(utilised.get("debits") or 0), 2),
            "positions": positions,
            "fetched_at": datetime.now(tz=IST).strftime("%H:%M:%S IST"),
            "source": "kite",
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "source": "kite"}


def apply_broker_marks_to_prices(
    state: dict[str, Any],
    prices: dict[str, float],
    snapshot: dict[str, Any] | None,
) -> dict[str, float]:
    """Align paper open-leg marks with Zerodha LTPs when live mirroring is on."""
    if not snapshot or snapshot.get("error"):
        return prices
    ltp_by_contract = {
        str(row["Contract"]): float(row["LTP ₹"])
        for row in snapshot.get("positions") or []
        if row.get("Contract")
    }
    out = dict(prices)
    for symbol, pos in (state.get("open_positions") or {}).items():
        contract = str(pos.get("contract") or "")
        if contract in ltp_by_contract:
            out[symbol] = ltp_by_contract[contract]
    return out


def apply_broker_cap(state: dict[str, Any], position: dict[str, Any]) -> tuple[bool, str]:
    """Shrink lots so total NFO premium stays within the Zerodha cap (paper + live)."""
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
            f"Zerodha funds ₹{remaining:,.0f} free — not enough for 1 lot "
            f"(need ₹{lot_cost:,.0f})",
        )
    position["lots"] = lots
    position["quantity"] = lots * lot_size
    position["amount_invested"] = round(lots * lot_cost, 2)
    if lots < before_lots:
        return True, f"scaled to {lots} lot(s) for available Zerodha margin"
    return True, ""


def validate_entry(state: dict[str, Any], position: dict[str, Any]) -> tuple[bool, str]:
    """True only if Zerodha would accept a MIS BUY for this position (paper parity)."""
    contract = position.get("contract")
    if not contract:
        return False, "not an option position"
    try:
        kite = _kite()
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    lot_size = int(position.get("lot_size") or 0)
    if PRODUCT == "MIS" and lot_size:
        try:
            quote = options.fetch_nfo_quote(kite, str(contract))
            oi_lots = options.open_interest_lots(quote, lot_size)
            if oi_lots < options.MIN_MIS_OI_LOTS:
                return False, (
                    f"MIS blocked for {contract}: OI {oi_lots:.0f} lots "
                    f"(Zerodha requires ≥ {options.MIN_MIS_OI_LOTS} lots)"
                )
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
    return apply_broker_cap(state, position)


def _tick_round(price: float, tick: float = 0.05) -> float:
    return round(round(float(price) / tick) * tick, 2)


def sell_limit_price(kite, tradingsymbol: str, reference: float) -> tuple[float, str]:
    """Aggressive LIMIT price for exiting long options (Zerodha blocks MARKET on many stock options)."""
    quote = options.fetch_nfo_quote(kite, tradingsymbol)
    bid = options.best_bid(quote)
    ltp = float(quote.get("last_price") or reference or 0)
    ref = reference if reference > 0 else ltp
    if bid > 0:
        # Slightly below best bid improves fill when the book is thin.
        px = bid * (1 - min(EXIT_SLIPPAGE_PCT, 0.02)) if EXIT_SLIPPAGE_PCT else bid
        return _tick_round(max(0.05, px)), f"limit at bid ₹{bid:.2f}"
    if ltp > 0:
        px = ltp * (1 - EXIT_SLIPPAGE_PCT)
        return _tick_round(max(0.05, px)), f"no bid — limit {EXIT_SLIPPAGE_PCT:.0%} below LTP ₹{ltp:.2f}"
    return _tick_round(max(0.05, ref * (1 - EXIT_SLIPPAGE_PCT))), "limit from paper mark (no quote)"


def fetch_broker_book() -> tuple[dict[str, dict[str, Any]], dict[str, int]] | None:
    """Today's orders and open net quantity by contract.

    None means Kite could not be read — callers must not drop paper legs on that.
    An empty order book with a successful read is a real empty book (cancelled
    orders from a previous session are not in today's list).
    """
    try:
        kite = _kite()
        rows = kite.orders() or []
        book = kite.positions() or {}
    except Exception:  # noqa: BLE001
        return None
    orders = {str(row.get("order_id")): row for row in rows if row.get("order_id")}
    net_qty: dict[str, int] = {}
    for row in book.get("net") or []:
        symbol = str(row.get("tradingsymbol") or "")
        qty = int(row.get("quantity") or 0)
        if symbol and qty > 0:
            net_qty[symbol] = net_qty.get(symbol, 0) + qty
    return orders, net_qty


def fetch_orders_by_id() -> dict[str, dict[str, Any]]:
    """Today's Kite orders keyed by order id. Empty dict if the session cannot be read."""
    book = fetch_broker_book()
    if book is None:
        return {}
    return book[0]


def completed_sell_average(orders: dict[str, dict[str, Any]], tradingsymbol: str) -> float:
    """Latest completed SELL average for this contract in today's order book."""
    latest: dict[str, Any] | None = None
    latest_ts = ""
    for order in orders.values():
        if str(order.get("tradingsymbol") or "") != tradingsymbol:
            continue
        if str(order.get("transaction_type") or "").upper() != "SELL":
            continue
        if str(order.get("status") or "").upper() != "COMPLETE":
            continue
        if int(order.get("filled_quantity") or 0) <= 0:
            continue
        stamp = str(order.get("order_timestamp") or order.get("exchange_timestamp") or "")
        if latest is None or stamp >= latest_ts:
            latest = order
            latest_ts = stamp
    if not latest:
        return 0.0
    return float(latest.get("average_price") or 0)


def order_phase(order: dict[str, Any] | None) -> str:
    """Map a Kite order to Open, Executed, or Closed.

    Open: still working (or unknown). Executed: fully filled.
    Closed: cancelled or rejected with no fill.
    """
    if not order:
        return "Open"
    status = str(order.get("status") or "").upper()
    filled = int(order.get("filled_quantity") or 0)
    if status == "COMPLETE" and filled > 0:
        return "Executed"
    if status in {"CANCELLED", "REJECTED"} and filled <= 0:
        return "Closed"
    return "Open"


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
    lot_size: int | None = None,
    *,
    product: str | None = None,
) -> dict[str, Any]:
    """Place a single NFO limit order. transaction_type: BUY or SELL."""
    kite = _kite()
    side = transaction_type.strip().upper()
    order_product = (product or PRODUCT).strip().upper() or PRODUCT
    if order_product == "MIS" and side == "BUY" and lot_size:
        quote = options.fetch_nfo_quote(kite, tradingsymbol)
        oi_lots = options.open_interest_lots(quote, int(lot_size))
        if oi_lots < options.MIN_MIS_OI_LOTS:
            raise RuntimeError(
                f"MIS blocked for {tradingsymbol}: OI {oi_lots:.0f} lots "
                f"(Zerodha requires ≥ {options.MIN_MIS_OI_LOTS} lots)"
            )
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
        product=order_product,
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
        "product": order_product,
    }


def mirror_entry(state: dict[str, Any], position: dict[str, Any]) -> dict[str, Any]:
    contract = position.get("contract")
    if not contract:
        return {"ok": False, "error": "not an option position"}
    qty = int(position["quantity"])
    try:
        result = place_option_order(
            "BUY",
            str(contract),
            qty,
            float(position["entry_price"]),
            lot_size=int(position.get("lot_size") or 0),
        )
        position["live_quantity"] = qty
        return result
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def mirror_exit(position: dict[str, Any], exit_price: float) -> dict[str, Any]:
    contract = position.get("contract")
    if not contract:
        return {"ok": False, "error": "not an option position"}
    requested = int(position.get("live_quantity") or position.get("quantity") or 0)
    if requested < 1:
        return {"ok": False, "error": "no quantity to exit"}
    try:
        kite = _kite()
        broker_row = net_long_position(kite, str(contract))
        if not broker_row:
            return {
                "ok": False,
                "error": (
                    f"No open long {PRODUCT} leg on Zerodha for {contract}. "
                    "A fresh SELL would be a naked short and needs large SPAN margin — "
                    "check Kite → Positions (paper book may be out of sync)."
                ),
            }
        broker_qty = int(broker_row["quantity"])
        if broker_qty < 1:
            return {
                "ok": False,
                "error": f"Zerodha net quantity is {broker_qty} for {contract} (not a long).",
            }
        qty = min(requested, broker_qty)
        order_product = str(broker_row.get("product") or PRODUCT).upper()
        limit_px, pricing_note = sell_limit_price(kite, str(contract), float(exit_price))
        if qty < requested:
            pricing_note += f" · selling {qty} qty (broker holds {broker_qty}, paper {requested})"
        result = place_option_order(
            "SELL",
            str(contract),
            qty,
            limit_px,
            lot_size=int(position.get("lot_size") or 0),
            product=order_product,
        )
        result["pricing_note"] = pricing_note
        result["broker_quantity"] = broker_qty
        return result
    except Exception as exc:  # noqa: BLE001
        err = str(exc)
        if "margin" in err.lower():
            err += (
                " — usually means Zerodha treated this as a new short (wrong qty/product) "
                f"or free cash is negative; square off only the open long qty under {PRODUCT}."
            )
        return {"ok": False, "error": err}
