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
# Far-weeklies have too much time premium for an intraday square-off book.
MAX_DAYS_TO_EXPIRY = 14

STOP_FRACTION = 0.35
TARGET_FRACTION = 0.50

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

    def cost_per_lot(self) -> float:
        return round(self.premium * self.lot_size, 2)

    def as_position_fields(self) -> dict[str, Any]:
        data = asdict(self)
        data["contract"] = self.tradingsymbol
        return data


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
    eligible = []
    for row in instruments:
        if row.get("name") != name or row.get("instrument_type") != option_type:
            continue
        try:
            expiry = dt.date.fromisoformat(str(row["expiry"])[:10])
        except ValueError:
            continue
        dte = (expiry - today).days
        if not MIN_DAYS_TO_EXPIRY <= dte <= MAX_DAYS_TO_EXPIRY:
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


def resolve_contract(kite, underlying: str, side: str, spot: float) -> OptionContract | None:
    """ATM CE for a long underlying bias, ATM PE for a short bias."""
    option_type = "CE" if side.upper() == "LONG" else "PE"
    row = pick_chain_row(load_nfo_instruments(kite), underlying, option_type, spot)
    if row is None:
        return None
    quote = kite.ltp(nfo_quote_key(row["tradingsymbol"]))
    key = nfo_quote_key(row["tradingsymbol"])
    last = float((quote.get(key) or {}).get("last_price") or 0)
    if last <= 0.5:
        return None
    stop, target = premium_levels(last)
    return OptionContract(
        underlying=underlying,
        tradingsymbol=row["tradingsymbol"],
        instrument_token=int(row["instrument_token"]),
        option_type=option_type,
        strike=float(row["strike"]),
        expiry=str(row["expiry"])[:10],
        lot_size=int(row["lot_size"]),
        premium=round(last, 2),
        stop=stop,
        target=target,
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


def fetch_option_ltps(kite, tradingsymbols: list[str]) -> dict[str, float]:
    if kite is None or not tradingsymbols:
        return {}
    keys = [nfo_quote_key(symbol) for symbol in tradingsymbols]
    raw = kite.ltp(keys)
    out: dict[str, float] = {}
    for symbol in tradingsymbols:
        key = nfo_quote_key(symbol)
        last = float((raw.get(key) or {}).get("last_price") or 0)
        if last > 0:
            out[symbol] = last
    return out
