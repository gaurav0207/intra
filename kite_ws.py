"""Zerodha Kite WebSocket stream (KiteTicker).

REST is still used for historical candles. Ticks update last prices (and OI)
for open option legs and for ATM contracts being searched / shown under
Will buy next — so entries do not wait on ``kite.quote`` / ``kite.ltp``.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import kite_client

_lock = threading.Lock()
_ltp_by_token: dict[int, float] = {}
_tick_at_by_token: dict[int, float] = {}
_ticks: dict[int, dict[str, Any]] = {}
_subscribed: set[int] = set()
_ticker: Any = None
_running = False


def _mode(ws) -> str:
    return getattr(ws, "MODE_FULL", None) or getattr(ws, "MODE_QUOTE", None) or ws.MODE_LTP


def _on_ticks(_ws, ticks: list[dict[str, Any]]) -> None:
    now = time.monotonic()
    with _lock:
        for tick in ticks:
            token = int(tick.get("instrument_token") or 0)
            last = tick.get("last_price")
            if token and last is not None:
                _ltp_by_token[token] = float(last)
                _tick_at_by_token[token] = now
                _ticks[token] = tick


def _on_connect(_ws, _response) -> None:
    with _lock:
        if _subscribed:
            tokens = list(_subscribed)
            _ws.subscribe(tokens)
            _ws.set_mode(_mode(_ws), tokens)


def _on_close(_ws, code, reason) -> None:
    global _running
    _running = False


def start() -> bool:
    """Start the threaded KiteTicker if a session exists."""
    global _ticker, _running
    key = kite_client.api_key()
    token = kite_client.load_saved_token()
    if not key or not token:
        return False
    with _lock:
        if _running and _ticker is not None:
            return True
    try:
        from kiteconnect import KiteTicker

        ticker = KiteTicker(key, token)
        ticker.on_ticks = _on_ticks
        ticker.on_connect = _on_connect
        ticker.on_close = _on_close
        ticker.connect(threaded=True)
        with _lock:
            _ticker = ticker
            _running = True
        return True
    except Exception:
        with _lock:
            _running = False
            _ticker = None
        return False


def stop() -> None:
    global _ticker, _running
    with _lock:
        if _ticker is not None:
            try:
                _ticker.close()
            except Exception:
                pass
        _ticker = None
        _running = False
        _subscribed.clear()


def subscribe_tokens(tokens: list[int]) -> None:
    """Subscribe to instrument tokens (NSE/NFO) in FULL mode (LTP + OI + depth)."""
    clean = [int(t) for t in tokens if int(t) > 0]
    if not clean:
        return
    if not _running:
        start()
    with _lock:
        new = [t for t in clean if t not in _subscribed]
        if not new:
            return
        _subscribed.update(new)
        if _ticker is not None:
            try:
                _ticker.subscribe(new)
                _ticker.set_mode(_mode(_ticker), list(_subscribed))
            except Exception:
                pass


def wait_for_ticks(tokens: list[int], timeout: float = 0.5) -> int:
    """Block briefly until subscribed tokens have a last price. Returns hits."""
    wanted = [int(t) for t in tokens if int(t) > 0]
    if not wanted or timeout <= 0:
        return sum(1 for t in wanted if ltp_for_token(t))
    deadline = time.monotonic() + timeout
    missing = set(wanted)
    while missing and time.monotonic() < deadline:
        with _lock:
            missing = {t for t in missing if t not in _ltp_by_token}
        if missing:
            time.sleep(0.04)
    return len(wanted) - len(missing)


def ltp_for_token(token: int) -> float | None:
    with _lock:
        last = _ltp_by_token.get(int(token))
    return last if last and last > 0 else None


def tick_for_token(token: int) -> dict[str, Any] | None:
    with _lock:
        tick = _ticks.get(int(token))
    return dict(tick) if tick else None


def oi_for_token(token: int) -> float | None:
    tick = tick_for_token(token)
    if not tick:
        return None
    oi = tick.get("oi")
    if oi is None:
        return None
    return float(oi)


def last_tick_age_seconds(token: int | None = None) -> float | None:
    """Seconds since the most recent tick (one token or any subscribed)."""
    with _lock:
        if token is not None:
            ts = _tick_at_by_token.get(int(token))
            return None if ts is None else max(0.0, time.monotonic() - ts)
        if not _tick_at_by_token:
            return None
        latest = max(_tick_at_by_token.values())
    return max(0.0, time.monotonic() - latest)


def is_running() -> bool:
    with _lock:
        return _running
