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


def fetch_nfo_intraday(kite, token: int, interval: str = "minute", days: int = 2):
    """OHLCV for one NFO contract. ``minute`` shows spikes the 5m stock signal misses."""
    import pandas as pd

    from data import IST

    if kite is None or int(token or 0) <= 0:
        return pd.DataFrame()
    now = dt.datetime.now(IST)
    start = now - dt.timedelta(days=max(1, int(days)))
    try:
        records = kite.historical_data(int(token), start, now, interval) or []
    except Exception:
        return pd.DataFrame()
    if not records:
        return pd.DataFrame()
    df = pd.DataFrame(records)
    df = df.rename(columns=str.lower)
    df["date"] = pd.to_datetime(df["date"])
    if df["date"].dt.tz is None:
        df["date"] = df["date"].dt.tz_localize(IST)
    else:
        df["date"] = df["date"].dt.tz_convert(IST)
    df = df.set_index("date")
    keep = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
    return df[keep]


def premium_peak_note(df, last: float | None = None) -> str:
    """Warn when the suggested premium is sitting on today's high."""
    if df is None or getattr(df, "empty", True):
        return ""
    from data import IST

    today = dt.datetime.now(IST).date()
    idx = df.index.tz_convert(IST) if getattr(df.index, "tz", None) else df.index
    try:
        session = df.loc[idx.date == today]
    except Exception:
        session = df
    if session is None or session.empty:
        session = df
    hi = float(session["high"].max())
    mark = float(last if last and last > 0 else session["close"].iloc[-1])
    if hi <= 0 or mark <= 0:
        return ""
    pct = mark / hi
    lo = float(session["low"].min())
    if pct >= 0.92:
        return (
            f"Premium ₹{mark:.2f} is {pct:.0%} of today's high ₹{hi:.2f} "
            f"(low ₹{lo:.2f}) — 5m signal may be buying the spike"
        )
    return f"Premium ₹{mark:.2f} vs today's high ₹{hi:.2f} ({pct:.0%}) · low ₹{lo:.2f}"


def fetch_nfo_quotes(kite, tradingsymbols: list[str]) -> dict[str, dict[str, Any]]:
    """One batched ``kite.quote`` per chunk (fallback when WebSocket has no tick)."""
    if not kite or not tradingsymbols:
        return {}
    out: dict[str, dict[str, Any]] = {}
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
            out[symbol] = raw.get(key) or {}
    return out


def fetch_nfo_quote(kite, tradingsymbol: str) -> dict[str, Any]:
    return fetch_nfo_quotes(kite, [tradingsymbol]).get(tradingsymbol) or {}


def subscribe_contract_tokens(contracts: list[OptionContract] | dict[str, OptionContract]) -> None:
    """Keep candidate + open NFO tokens on the WebSocket so premiums stay live."""
    rows = contracts.values() if isinstance(contracts, dict) else contracts
    tokens = [int(c.instrument_token) for c in rows if int(getattr(c, "instrument_token", 0) or 0) > 0]
    if not tokens:
        return
    try:
        import kite_ws

        if kite_ws.is_running() or kite_ws.start():
            kite_ws.subscribe_tokens(tokens)
    except Exception:
        pass


def apply_live_premium(contract: OptionContract) -> bool:
    """Overwrite premium / stop / target from the latest WebSocket LTP."""
    try:
        import kite_ws

        last = kite_ws.ltp_for_token(int(contract.instrument_token))
    except Exception:
        return False
    if not last or last <= 0.5:
        return False
    contract.premium = round(last, 2)
    contract.stop, contract.target = premium_levels(last)
    return True


def apply_live_premiums(contracts: dict[str, OptionContract]) -> int:
    hits = 0
    for contract in contracts.values():
        if apply_live_premium(contract):
            hits += 1
    return hits


def _ws_fields(token: int) -> tuple[float, float | None]:
    """Return (last_price or 0, raw OI or None) from the ticker cache."""
    try:
        import kite_ws

        last = kite_ws.ltp_for_token(int(token)) or 0.0
        oi = kite_ws.oi_for_token(int(token))
        return float(last), oi
    except Exception:
        return 0.0, None


def _contract_from_row(
    underlying: str,
    option_type: str,
    row: dict[str, Any],
    quote: dict[str, Any] | None = None,
) -> OptionContract | None:
    lot_size = int(row["lot_size"])
    token = int(row["instrument_token"])
    ws_last, ws_oi = _ws_fields(token)
    last = ws_last if ws_last > 0 else float((quote or {}).get("last_price") or 0)
    if last <= 0.5:
        return None
    oi_lots = 0.0
    if ws_oi is not None and ws_oi > 0:
        oi_lots = open_interest_lots({"oi": ws_oi}, lot_size)
    if oi_lots < MIN_MIS_OI_LOTS and quote:
        oi_lots = open_interest_lots(quote, lot_size)
    if oi_lots < MIN_MIS_OI_LOTS:
        return None
    stop, target = premium_levels(last)
    return OptionContract(
        underlying=underlying,
        tradingsymbol=row["tradingsymbol"],
        instrument_token=token,
        option_type=option_type,
        strike=float(row["strike"]),
        expiry=str(row["expiry"])[:10],
        lot_size=lot_size,
        premium=round(last, 2),
        stop=stop,
        target=target,
        oi_lots=round(oi_lots, 1),
    )


def resolve_contract(kite, underlying: str, side: str, spot: float) -> OptionContract | None:
    """ATM CE for a long underlying bias, ATM PE for a short bias."""
    return resolve_many(kite, {underlying: (side, spot)}).get(underlying)


def resolve_many(kite, wanted: dict[str, tuple[str, float]]) -> dict[str, OptionContract]:
    """wanted: underlying -> (side, spot).

    ATM rows are picked from the cached NFO dump (no REST). Tokens are
    subscribed on the WebSocket immediately; one batched ``kite.quote``
    fills OI / LTP only for names that still have no tick.
    """
    if kite is None or not wanted:
        return {}
    instruments = load_nfo_instruments(kite)
    picks: dict[str, tuple[str, dict[str, Any]]] = {}
    for underlying, (side, spot) in wanted.items():
        option_type = "CE" if str(side).upper() == "LONG" else "PE"
        row = pick_chain_row(instruments, underlying, option_type, spot)
        if row is not None:
            picks[underlying] = (option_type, row)
    if not picks:
        return {}

    tokens = [int(row["instrument_token"]) for _, row in picks.values()]
    try:
        import kite_ws

        if kite_ws.is_running() or kite_ws.start():
            kite_ws.subscribe_tokens(tokens)
            missing = [t for t in tokens if not kite_ws.ltp_for_token(t)]
            if missing:
                kite_ws.wait_for_ticks(missing, timeout=0.5)
    except Exception:
        pass

    need_rest: list[str] = []
    for _, row in picks.values():
        last, oi = _ws_fields(int(row["instrument_token"]))
        if last <= 0 or not oi:
            need_rest.append(row["tradingsymbol"])
    quotes = fetch_nfo_quotes(kite, list(dict.fromkeys(need_rest)))

    found: dict[str, OptionContract] = {}
    for underlying, (option_type, row) in picks.items():
        try:
            contract = _contract_from_row(
                underlying, option_type, row, quotes.get(row["tradingsymbol"])
            )
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
