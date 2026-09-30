"""Zerodha Kite WebSocket LTP stream (KiteTicker).

REST is still used for historical candles; ticks update last prices for open
options and marks without polling ``kite.ltp`` every cycle.
"""

from __future__ import annotations

import threading
from typing import Any

import kite_client

_lock = threading.Lock()
_ltp_by_token: dict[int, float] = {}
_tick_at_by_token: dict[int, float] = {}
_subscribed: set[int] = set()
_ticker: Any = None
_running = False


def _on_ticks(_ws, ticks: list[dict[str, Any]]) -> None:
    import time as _time

    now = _time.monotonic()
    with _lock:
        for tick in ticks:
            token = int(tick.get("instrument_token") or 0)
            last = tick.get("last_price")
            if token and last is not None:
                _ltp_by_token[token] = float(last)
                _tick_at_by_token[token] = now


def _on_connect(_ws, _response) -> None:
    with _lock:
        if _subscribed:
            _ws.subscribe(list(_subscribed))
            _ws.set_mode(_ws.MODE_LTP, list(_subscribed))


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
    """Subscribe to instrument tokens (NSE/NFO)."""
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
                _ticker.set_mode(_ticker.MODE_LTP, new)
            except Exception:
                pass


def ltp_for_token(token: int) -> float | None:
    with _lock:
        last = _ltp_by_token.get(int(token))
    return last if last and last > 0 else None


def last_tick_age_seconds(token: int | None = None) -> float | None:
    """Seconds since the most recent tick (one token or any subscribed)."""
    import time as _time

    with _lock:
        if token is not None:
            ts = _tick_at_by_token.get(int(token))
            return None if ts is None else max(0.0, _time.monotonic() - ts)
        if not _tick_at_by_token:
            return None
        latest = max(_tick_at_by_token.values())
    return max(0.0, _time.monotonic() - latest)


def is_running() -> bool:
    with _lock:
        return _running
