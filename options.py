"""Map an underlying long/short signal onto a liquid NFO call or put.

Signals stay on the cash/index chart. The paper book only buys options
(debit): LONG → ATM CE, SHORT → ATM PE. Selling options is not used.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

CACHE_PATH = Path(os.getenv("NFO_CACHE_FILE", ".nfo_instruments.json"))

# Skip same-day expiry; 0DTE premium is a lottery ticket, not a repeatable edge.
MIN_DAYS_TO_EXPIRY = 1
# Index weeklies: stay in the near weekly band for intraday gamma.
INDEX_MAX_DAYS_TO_EXPIRY = 14
# Stock F&O is mostly monthly; the next series is often 3–5 weeks out.
STOCK_MAX_DAYS_TO_EXPIRY = 45

STOP_FRACTION = 0.35
TARGET_FRACTION = 0.50
# Zerodha blocks MIS when contract OI is below this (lots). See Kite OI restrictions.
MIN_MIS_OI_LOTS = int(os.getenv("MIN_MIS_OI_LOTS", "500"))

QUOTE_MAP = {
    "NIFTY": "NSE:NIFTY 50",
    "NIFTY 50": "NSE:NIFTY 50",
    "BANKNIFTY": "NSE:NIFTY BANK",
    "NIFTY BANK": "NSE:NIFTY BANK",
}


@dataclass
class OptionContract:
    underlying: str
    tradingsymbol: str
    instrument_token: int
    option_type: str
    strike: float
    expiry: str
    lot_size: int
    premium: float
    stop: float
    target: float
    oi_lots: float = 0.0

    def cost_per_lot(self) -> float:
        return round(self.premium * self.lot_size, 2)

    def as_position_fields(self) -> dict[str, Any]:
        data = asdict(self)
        data["contract"] = self.tradingsymbol
        return data

    def mis_eligible(self) -> bool:
        return self.oi_lots >= MIN_MIS_OI_LOTS


def open_interest_lots(quote: dict[str, Any], lot_size: int) -> float:
    """Open interest in lots (NSE OI from Kite is usually in shares)."""
    oi = float(quote.get("oi") or 0)
    if oi <= 0 or lot_size <= 0:
        return 0.0
    if oi >= lot_size:
        return oi / lot_size
    return oi


def best_bid(quote: dict[str, Any]) -> float:
    """Top bid from Kite depth (best price to hit when selling)."""
    for level in (quote.get("depth") or {}).get("buy") or []:
        price = float(level.get("price") or 0)
        qty = int(level.get("quantity") or 0)
        if price > 0 and qty > 0:
            return price
    return 0.0


def best_ask(quote: dict[str, Any]) -> float:
    for level in (quote.get("depth") or {}).get("sell") or []:
        price = float(level.get("price") or 0)
        qty = int(level.get("quantity") or 0)
        if price > 0 and qty > 0:
            return price
    return 0.0


def nfo_quote_key(tradingsymbol: str) -> str:
    return f"NFO:{tradingsymbol}"


def underlying_quote_key(symbol: str) -> str:
    symbol = symbol.strip().upper().replace(".NS", "").replace(".BO", "")
    return QUOTE_MAP.get(symbol, f"NSE:{symbol}")


def _today() -> dt.date:
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=5, minutes=30))).date()


def load_nfo_instruments(kite) -> list[dict[str, Any]]:
    today = _today().isoformat()
    if CACHE_PATH.exists():
        try:
            payload = json.loads(CACHE_PATH.read_text())
            if payload.get("date") == today and payload.get("instruments"):
                return payload["instruments"]
        except (OSError, json.JSONDecodeError, TypeError):
            pass
    raw = kite.instruments("NFO")
    slim = []
    for row in raw:
        if row.get("instrument_type") not in {"CE", "PE"}:
            continue
        expiry = row.get("expiry")
        if hasattr(expiry, "isoformat"):
            expiry = expiry.isoformat()
        slim.append(
            {
                "tradingsymbol": row["tradingsymbol"],
                "instrument_token": int(row["instrument_token"]),
                "name": str(row.get("name") or "").upper(),
                "expiry": str(expiry)[:10],
                "strike": float(row.get("strike") or 0),
                "instrument_type": row["instrument_type"],
                "lot_size": int(row.get("lot_size") or 0),
            }
        )
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps({"date": today, "instruments": slim}))
    return slim


def _nfo_name(symbol: str) -> str:
    symbol = symbol.strip().upper().replace(".NS", "").replace(".BO", "")
    if symbol in {"NIFTY", "NIFTY50", "NIFTY 50"}:
        return "NIFTY"
    if symbol in {"BANKNIFTY", "NIFTY BANK"}:
        return "BANKNIFTY"
    return symbol


def _max_days_to_expiry(underlying: str) -> int:
    name = _nfo_name(underlying)
    if name in {"NIFTY", "BANKNIFTY"}:
        return INDEX_MAX_DAYS_TO_EXPIRY
    return STOCK_MAX_DAYS_TO_EXPIRY


def pick_chain_row(
    instruments: list[dict[str, Any]],
    underlying: str,
    option_type: str,
    spot: float,
    today: dt.date | None = None,
) -> dict[str, Any] | None:
    today = today or _today()
    name = _nfo_name(underlying)
    option_type = option_type.upper()
    max_dte = _max_days_to_expiry(underlying)
    eligible = []
    for row in instruments:
        if row.get("name") != name or row.get("instrument_type") != option_type:
            continue
        try:
            expiry = dt.date.fromisoformat(str(row["expiry"])[:10])
        except ValueError:
            continue
        dte = (expiry - today).days
        if not MIN_DAYS_TO_EXPIRY <= dte <= max_dte:
            continue
        if float(row.get("strike") or 0) <= 0 or int(row.get("lot_size") or 0) < 1:
            continue
        eligible.append((dte, abs(float(row["strike"]) - spot), float(row["strike"]), row))
    if not eligible:
        return None
    eligible.sort()
    return eligible[0][3]


def premium_levels(premium: float) -> tuple[float, float]:
    premium = float(premium)
    stop = round(max(premium * (1 - STOP_FRACTION), 0.05), 2)
    target = round(premium * (1 + TARGET_FRACTION), 2)
    return stop, target


def fetch_nfo_quote(kite, tradingsymbol: str) -> dict[str, Any]:
    key = nfo_quote_key(tradingsymbol)
    payload = kite.quote([key])
    return payload.get(key) or {}


def resolve_contract(kite, underlying: str, side: str, spot: float) -> OptionContract | None:
    """ATM CE for a long underlying bias, ATM PE for a short bias."""
    option_type = "CE" if side.upper() == "LONG" else "PE"
    row = pick_chain_row(load_nfo_instruments(kite), underlying, option_type, spot)
    if row is None:
        return None
    lot_size = int(row["lot_size"])
    quote = fetch_nfo_quote(kite, row["tradingsymbol"])
    last = float(quote.get("last_price") or 0)
    if last <= 0.5:
        return None
    oi_lots = open_interest_lots(quote, lot_size)
    if oi_lots < MIN_MIS_OI_LOTS:
        return None
    stop, target = premium_levels(last)
    return OptionContract(
        underlying=underlying,
        tradingsymbol=row["tradingsymbol"],
        instrument_token=int(row["instrument_token"]),
        option_type=option_type,
        strike=float(row["strike"]),
        expiry=str(row["expiry"])[:10],
        lot_size=lot_size,
        premium=round(last, 2),
        stop=stop,
        target=target,
        oi_lots=round(oi_lots, 1),
    )


def resolve_many(kite, wanted: dict[str, tuple[str, float]]) -> dict[str, OptionContract]:
    """wanted: underlying -> (side, spot)."""
    if kite is None or not wanted:
        return {}
    load_nfo_instruments(kite)
    found: dict[str, OptionContract] = {}
    for underlying, (side, spot) in wanted.items():
        try:
            contract = resolve_contract(kite, underlying, side, spot)
        except Exception:
            continue
        if contract is not None:
            found[underlying] = contract
    return found


def fetch_option_exit_marks(
    kite,
    tradingsymbols: list[str],
) -> dict[str, float]:
    """Conservative marks for long-option exits (bid preferred, like live LIMIT sells)."""
    if not kite or not tradingsymbols:
        return {}
    slippage = max(0.0, min(0.25, float(os.getenv("LIVE_EXIT_SLIPPAGE_PCT", "0.05"))))
    out: dict[str, float] = {}
    keys = [nfo_quote_key(symbol) for symbol in tradingsymbols]
    chunk = 400
    for start in range(0, len(tradingsymbols), chunk):
        batch_syms = tradingsymbols[start : start + chunk]
        batch_keys = keys[start : start + chunk]
        try:
            raw = kite.quote(batch_keys)
        except Exception:  # noqa: BLE001
            continue
        for symbol, key in zip(batch_syms, batch_keys):
            quote = raw.get(key) or {}
            bid = best_bid(quote)
            ltp = float(quote.get("last_price") or 0)
            if bid > 0:
                px = bid * (1 - min(slippage, 0.02)) if slippage else bid
            elif ltp > 0:
                px = ltp * (1 - slippage)
            else:
                continue
            out[symbol] = round(max(0.05, px), 2)
    return out


def fetch_option_ltps(
    kite,
    tradingsymbols: list[str],
    instrument_tokens: dict[str, int] | None = None,
) -> dict[str, float]:
    if not tradingsymbols:
        return {}
    out: dict[str, float] = {}
    instrument_tokens = instrument_tokens or {}
    try:
        import kite_ws

        if kite_ws.is_running() or kite_ws.start():
            tokens = [
                int(instrument_tokens[s])
                for s in tradingsymbols
                if s in instrument_tokens and int(instrument_tokens[s]) > 0
            ]
            if tokens:
                kite_ws.subscribe_tokens(tokens)
            for symbol in tradingsymbols:
                token = instrument_tokens.get(symbol)
                if token:
                    last = kite_ws.ltp_for_token(int(token))
                    if last:
                        out[symbol] = last
    except Exception:
        pass
    missing = [s for s in tradingsymbols if s not in out]
    if not missing or kite is None:
        return out
    keys = [nfo_quote_key(symbol) for symbol in missing]
    raw = kite.ltp(keys)
    for symbol in missing:
        key = nfo_quote_key(symbol)
        last = float((raw.get(key) or {}).get("last_price") or 0)
        if last > 0:
            out[symbol] = last
    return out
