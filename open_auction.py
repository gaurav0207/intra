"""9:15 open-auction mode: buy a pre-picked ATM option from the tape.

The regular book waits for the 15-minute opening range and a 5-minute
stock score (entries from 09:30). This mode is separate:

1. From 09:00, map a strong daily bias to an ATM CE or PE and subscribe it.
2. From 09:15–09:40, buy if that option (or the underlying) has already
   gapped and confidence clears — a ≥50% premium gap is itself the
   confidence; smaller gaps need the 5-minute side to agree.
"""

from __future__ import annotations

import datetime as dt
import os
from typing import Any

from data import IST, fetch_daily
from indicators import compute_all
from options import OptionContract, pick_chain_row, premium_levels
from signals import Signal, attach_tomorrow_forecast, entry_side

_signal_cache: dict[str, tuple[float, Any]] = {}
_signal_cache_ttl = 25.0

PREP_START = dt.time(9, 0)
ENTRY_START = dt.time(9, 15)
# Late prints (stale WS, REST catch-up) still fill for a few minutes after 09:30.
ENTRY_END = dt.time(9, 40)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def enabled() -> bool:
    raw = os.getenv("OPEN_AUCTION_ENABLED", "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def max_names() -> int:
    return max(1, min(12, _env_int("OPEN_AUCTION_MAX_NAMES", 8)))


def max_positions() -> int:
    return max(1, min(3, _env_int("OPEN_AUCTION_MAX_POSITIONS", 1)))


def min_premium_gap() -> float:
    return max(0.05, min(2.0, _env_float("OPEN_AUCTION_MIN_PREMIUM_GAP_PCT", 0.25)))


def min_spot_gap() -> float:
    return max(0.005, min(0.08, _env_float("OPEN_AUCTION_MIN_SPOT_GAP_PCT", 0.015)))


def min_confidence() -> int:
    """Same floor as the 09:30 book unless OPEN_AUCTION_MIN_CONFIDENCE is set."""
    import trading_policy

    return max(
        0,
        min(100, _env_int("OPEN_AUCTION_MIN_CONFIDENCE", trading_policy.min_confidence())),
    )


def _now_ist() -> dt.datetime:
    return dt.datetime.now(IST)


def _today() -> str:
    return _now_ist().date().isoformat()


def in_prep_window(now: dt.time | None = None) -> bool:
    now = now or _now_ist().time()
    return PREP_START <= now < ENTRY_START


def in_entry_window(now: dt.time | None = None) -> bool:
    now = now or _now_ist().time()
    return ENTRY_START <= now < ENTRY_END


def bias_to_option(bias: str) -> str | None:
    bias = (bias or "").upper()
    if bias == "BULLISH":
        return "CE"
    if bias == "BEARISH":
        return "PE"
    return None


def tape_gap_confidence(premium: float, prev_premium: float) -> int:
    """Map option-premium stretch to the same 0–100 scale as the 5m book.

    A 50% gap is 70% confidence (the floor). 90%+ is ~94–100. A 25% gap
    is only ~55, so those still need the 5-minute side to agree.
    """
    if prev_premium <= 0 or premium <= 0:
        return 0
    gap = (float(premium) - float(prev_premium)) / float(prev_premium)
    return max(0, min(100, int(round(70 + (gap - 0.50) * 60))))


def tape_should_fire(
    option_type: str,
    premium: float,
    prev_premium: float,
    spot: float,
    spot_ref: float,
    min_prem: float | None = None,
    min_spot: float | None = None,
) -> tuple[bool, str]:
    """True when the option tape or the underlying has already gapped our way."""
    option_type = option_type.upper()
    min_prem = min_premium_gap() if min_prem is None else min_prem
    min_spot = min_spot_gap() if min_spot is None else min_spot
    reasons: list[str] = []
    if prev_premium > 0 and premium > 0:
        gap = (premium - prev_premium) / prev_premium
        if gap >= min_prem:
            reasons.append(f"option +{gap:.0%} vs prior close ₹{prev_premium:.2f}")
    if spot > 0 and spot_ref > 0:
        gap = (spot - spot_ref) / spot_ref
        if option_type == "PE" and gap <= -min_spot:
            reasons.append(f"spot {gap:.1%} vs ₹{spot_ref:.2f}")
        elif option_type == "CE" and gap >= min_spot:
            reasons.append(f"spot +{gap:.1%} vs ₹{spot_ref:.2f}")
    if not reasons:
        return False, ""
    return True, "; ".join(reasons)


def signal_allows_option(sig: Signal | None, option_type: str, floor: int | None = None) -> tuple[bool, str]:
    """Tape gap is not enough — the 5m score must agree and clear the confidence floor."""
    floor = min_confidence() if floor is None else int(floor)
    option_type = (option_type or "").upper()
    if sig is None:
        return False, "no 5m signal yet"
    conf = int(sig.confidence or 0)
    if conf < floor:
        return False, f"confidence {conf}% < {floor}%"
    want = "LONG" if option_type == "CE" else "SHORT"
    action = str(sig.what_to_do or "").upper()
    if "EXIT" in action:
        return False, f"{sig.what_to_do} · {conf}%"
    side = entry_side(sig)
    if side == want:
        return True, f"{sig.what_to_do} · {conf}%"
    if want == "SHORT" and (int(sig.score) <= -4 or "SHORT" in action):
        return True, f"{sig.what_to_do} · score {sig.score} · {conf}%"
    if want == "LONG" and (int(sig.score) >= 4 or "LONG" in action):
        return True, f"{sig.what_to_do} · score {sig.score} · {conf}%"
    return False, f"{sig.what_to_do} · {conf}% does not confirm {option_type}"


def _index_frame(kite):
    from data import fetch_index

    try:
        raw = fetch_index(interval="5m", kite=kite)
        if raw is not None and not raw.empty:
            return compute_all(raw)
    except Exception:
        pass
    return None


def cached_signal(symbol: str, kite, index_df=None):
    """Reuse a 5m signal for a few seconds so the 09:15 loop stays fast."""
    import time

    import scanner

    now = time.monotonic()
    hit = _signal_cache.get(symbol)
    if hit and now - hit[0] < _signal_cache_ttl:
        return hit[1]
    try:
        result = scanner._one(
            symbol, "5m", kite, 1.5, 2.5, False, index_df
        )
        sig = result[1] if result else None
    except Exception:
        sig = None
    _signal_cache[symbol] = (now, sig)
    return sig


def _dummy_signal(symbol: str, close: float) -> Signal:
    return Signal(
        symbol=symbol,
        action="HOLD",
        score=0,
        price=close,
        reasons=[],
        stop_loss=None,
        target=None,
        rsi=0.0,
        vwap=close,
    )


def _daily_score(daily) -> int:
    if daily is None or len(daily) < 2:
        return 0
    from signals import _score_daily

    score, _ = _score_daily(daily.iloc[-2], daily.iloc[-1])
    return int(score)


def _option_prev_close(kite, token: int) -> float:
    if kite is None or int(token) <= 0:
        return 0.0
    now = _now_ist()
    start = now - dt.timedelta(days=12)
    try:
        rows = kite.historical_data(int(token), start, now, "day") or []
    except Exception:
        return 0.0
    today = now.date()
    closes: list[float] = []
    for row in rows:
        stamp = row.get("date")
        day = stamp.date() if hasattr(stamp, "date") else None
        if day is None or day >= today:
            continue
        close = float(row.get("close") or 0)
        if close > 0:
            closes.append(close)
    return closes[-1] if closes else 0.0


def _spot_quote(kite, symbol: str) -> tuple[int, float]:
    import kite_client

    key = kite_client._nse_tradingsymbol(symbol)
    try:
        raw = kite.ltp([key]) or {}
    except Exception:
        return 0, 0.0
    row = raw.get(key) or {}
    token = int(row.get("instrument_token") or 0)
    last = float(row.get("last_price") or 0)
    return token, last


def _live_ltp(token: int) -> float:
    if int(token) <= 0:
        return 0.0
    try:
        import kite_ws

        last = kite_ws.ltp_for_token(int(token))
        return float(last) if last else 0.0
    except Exception:
        return 0.0


def subscribe_plan(plan: list[dict[str, Any]]) -> None:
    tokens = []
    for row in plan:
        for key in ("instrument_token", "spot_token"):
            tok = int(row.get(key) or 0)
            if tok > 0:
                tokens.append(tok)
    if not tokens:
        return
    try:
        import kite_ws

        if kite_ws.is_running() or kite_ws.start():
            kite_ws.subscribe_tokens(tokens)
            kite_ws.wait_for_ticks(tokens, timeout=0.8)
    except Exception:
        pass


def _spot_row(symbol: str, close: float, bias: str, score: int, kite) -> dict[str, Any]:
    spot_token, spot_ltp = _spot_quote(kite, symbol) if kite is not None else (0, 0.0)
    return {
        "underlying": symbol,
        "bias": bias,
        "spot_ref": round(float(close if close > 0 else spot_ltp), 2),
        "spot_token": int(spot_token),
        "score": int(score),
    }


def _option_row(
    symbol: str,
    option_type: str,
    bias: str,
    spot_ref: float,
    instruments: list,
    kite,
    score: int = 0,
    spot_token: int = 0,
) -> dict[str, Any] | None:
    row = pick_chain_row(instruments, symbol, option_type, spot_ref)
    if row is None:
        return None
    return {
        "underlying": symbol,
        "bias": bias,
        "option_type": option_type,
        "tradingsymbol": row["tradingsymbol"],
        "instrument_token": int(row["instrument_token"]),
        "strike": float(row["strike"]),
        "expiry": str(row["expiry"])[:10],
        "lot_size": int(row["lot_size"]),
        "spot_ref": round(float(spot_ref), 2),
        "spot_token": int(spot_token),
        "prev_premium": round(_option_prev_close(kite, int(row["instrument_token"])), 2),
        "score": int(score),
    }


def build_plan(kite) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Strong daily bias → ATM option rows. Also keep every name's prior close."""
    import adaptive_policy
    import options

    if kite is None:
        return [], []
    instruments = options.load_nfo_instruments(kite)
    ranked: list[tuple[int, dict[str, Any]]] = []
    spots: list[dict[str, Any]] = []
    for symbol in adaptive_policy.AI_UNIVERSE:
        try:
            raw = fetch_daily(symbol, kite=kite)
            if raw is None or raw.empty:
                continue
            daily = compute_all(raw)
            close = float(daily.iloc[-1]["close"])
            sig = attach_tomorrow_forecast(
                _dummy_signal(symbol, close), daily, long_only=False
            )
            score = abs(_daily_score(daily))
            spots.append(_spot_row(symbol, close, sig.tomorrow_bias, score, kite))
            option_type = bias_to_option(sig.tomorrow_bias)
            if option_type is None:
                continue
            built = _option_row(
                symbol,
                option_type,
                sig.tomorrow_bias,
                close,
                instruments,
                kite,
                score=score,
                spot_token=int(spots[-1]["spot_token"]),
            )
            if built:
                ranked.append((score, built))
        except Exception:
            continue
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [row for _, row in ranked[: max_names()]], spots


def discover_live_gaps(kite, spots: list[dict[str, Any]], existing: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """At 9:15, names that already gapped vs prior close get an ATM option on the fly."""
    import kite_client
    import options

    if kite is None or not spots:
        return []
    have = {(r["underlying"], r["option_type"]) for r in existing}
    keys = [kite_client._nse_tradingsymbol(r["underlying"]) for r in spots]
    try:
        raw = kite.ltp(keys) or {}
    except Exception:
        return []
    instruments = options.load_nfo_instruments(kite)
    extra: list[dict[str, Any]] = []
    for ref, key in zip(spots, keys):
        last = float((raw.get(key) or {}).get("last_price") or 0)
        token = int((raw.get(key) or {}).get("instrument_token") or ref.get("spot_token") or 0)
        spot_ref = float(ref.get("spot_ref") or 0)
        if last <= 0 or spot_ref <= 0:
            continue
        gap = (last - spot_ref) / spot_ref
        if gap <= -min_spot_gap():
            option_type = "PE"
            bias = "GAP DOWN"
        elif gap >= min_spot_gap():
            option_type = "CE"
            bias = "GAP UP"
        else:
            continue
        symbol = str(ref["underlying"])
        if (symbol, option_type) in have:
            continue
        built = _option_row(
            symbol,
            option_type,
            bias,
            last,
            instruments,
            kite,
            score=int(abs(gap) * 100),
            spot_token=token,
        )
        if not built:
            continue
        built["spot_ref"] = round(spot_ref, 2)
        extra.append(built)
        have.add((symbol, option_type))
    extra.sort(key=lambda r: -float(r.get("score") or 0))
    return extra[: max_names()]


def ensure_plan(state: dict[str, Any], kite) -> list[dict[str, Any]]:
    import paper_trader

    today = _today()
    plan = list(state.get("open_auction_plan") or [])
    if state.get("open_auction_date") == today and (
        plan or state.get("open_auction_spots")
    ):
        subscribe_plan(plan)
        return plan
    plan, spots = build_plan(kite) if kite is not None else ([], [])
    state["open_auction_plan"] = plan
    state["open_auction_spots"] = spots
    state["open_auction_date"] = today
    state["open_auction_taken"] = []
    paper_trader.save_state(state)
    subscribe_plan(plan)
    return plan


def plan_to_contract(row: dict[str, Any], premium: float) -> OptionContract | None:
    premium = float(premium)
    if premium <= 0.5:
        return None
    stop, target = premium_levels(premium)
    return OptionContract(
        underlying=str(row["underlying"]),
        tradingsymbol=str(row["tradingsymbol"]),
        instrument_token=int(row["instrument_token"]),
        option_type=str(row["option_type"]),
        strike=float(row["strike"]),
        expiry=str(row["expiry"])[:10],
        lot_size=int(row["lot_size"]),
        premium=round(premium, 2),
        stop=stop,
        target=target,
        oi_lots=float(options_min_oi_ok()),
    )


def options_min_oi_ok() -> float:
    import options

    return float(options.MIN_MIS_OI_LOTS)


def _rest_ltp(kite, symbol: str, is_option: bool) -> float:
    if kite is None:
        return 0.0
    key = f"NFO:{symbol}" if is_option else None
    if not is_option:
        import kite_client

        key = kite_client._nse_tradingsymbol(symbol)
    try:
        raw = kite.ltp([key]) or {}
        return float((raw.get(key) or {}).get("last_price") or 0)
    except Exception:
        return 0.0


def _best_ltp(ws: float, rest: float, prev: float = 0.0) -> float:
    """Do not let a stale WebSocket print hide a REST gap (GRASIM-style miss)."""
    ws = float(ws or 0)
    rest = float(rest or 0)
    prev = float(prev or 0)
    if ws <= 0:
        return rest
    if rest <= 0:
        return ws
    if prev > 0 and abs(ws - prev) / prev <= 0.08 and rest > prev * 1.15:
        return rest
    return max(ws, rest)


def evaluate_row(row: dict[str, Any], kite=None) -> tuple[bool, str, float, float]:
    prev = float(row.get("prev_premium") or 0)
    premium = _best_ltp(
        _live_ltp(int(row.get("instrument_token") or 0)),
        _rest_ltp(kite, str(row["tradingsymbol"]), True),
        prev,
    )
    spot = _best_ltp(
        _live_ltp(int(row.get("spot_token") or 0)),
        _rest_ltp(kite, str(row["underlying"]), False),
        0.0,
    )
    if spot <= 0:
        spot = float(row.get("spot_ref") or 0)
    fire, why = tape_should_fire(
        str(row["option_type"]),
        premium,
        prev,
        spot,
        float(row.get("spot_ref") or 0),
    )
    return fire, why, premium, spot


def evaluate_candidate(
    row: dict[str, Any],
    kite=None,
    index_df=None,
) -> tuple[bool, str, float, float, int]:
    """Tape gap plus confidence. A ≥50% premium gap is itself the confidence."""
    fire, why, premium, spot = evaluate_row(row, kite)
    prev = float(row.get("prev_premium") or 0)
    gap = ((premium - prev) / prev) if prev > 0 and premium > 0 else 0.0
    large_gap = gap >= 0.50
    sig = cached_signal(str(row["underlying"]), kite, index_df) if kite is not None else None
    ok_sig, sig_why = signal_allows_option(sig, str(row["option_type"]))
    sig_conf = int(getattr(sig, "confidence", 0) or 0)
    gap_conf = tape_gap_confidence(premium, prev) if fire else 0
    if large_gap:
        conf = max(sig_conf, gap_conf)
        sig_why = f"tape gap {gap:.0%} · conf {conf}%"
        ok_sig = conf >= min_confidence()
    else:
        conf = sig_conf
    if not fire:
        return False, why or "waiting for gap", premium, spot, conf
    if not ok_sig:
        return False, sig_why, premium, spot, conf
    return True, f"{why}; {sig_why}", premium, spot, conf


def fill_block_reason(
    state: dict[str, Any] | None,
    row: dict[str, Any],
    premium: float,
    kite=None,
) -> str:
    """Why try_entries would still skip a tape-ready name."""
    import live_kite
    import paper_trader
    import trading_policy

    symbol = str(row.get("underlying") or "")
    if not enabled():
        return "open auction off"
    if not in_entry_window():
        return f"window closed {ENTRY_END:%H:%M}"
    if state is None:
        return ""
    if not state.get("auto_trading_enabled", True):
        return "auto trading off"
    halt = paper_trader.session_entry_block_reason(
        state, {}, None, trading_policy.daily_loss_cap(), None
    )
    if halt:
        return halt
    blocked = trading_policy.entry_block_reason(state)
    if blocked:
        return blocked
    if symbol in (state.get("open_positions") or {}):
        return "already in the book"
    if symbol in (state.get("open_auction_taken") or []):
        return "already taken this session"
    already = sum(
        1
        for pos in (state.get("open_positions") or {}).values()
        if str(pos.get("entry_style") or "").startswith("open_auction")
        or str(pos.get("signal_key") or "").endswith("OPEN_AUCTION")
    )
    if already >= max_positions():
        return f"auction slot used ({already}/{max_positions()})"
    contract = plan_to_contract(row, premium)
    if contract is None:
        return "premium too small"
    budget = min(float(state.get("cash") or 0), live_kite.remaining_capital(state))
    need = contract.cost_per_lot()
    if budget < need:
        return f"need ₹{need:,.0f} for 1 lot, have ₹{budget:,.0f}"
    probe = {
        "contract": contract.tradingsymbol,
        "lots": 1,
        "lot_size": int(contract.lot_size),
        "entry_price": float(contract.premium),
        "quantity": int(contract.lot_size),
        "amount_invested": need,
    }
    try:
        ok, note = live_kite.validate_entry(state, probe)
    except Exception as exc:  # noqa: BLE001
        return str(exc)
    if not ok:
        return note or "broker rejected"
    return ""


def diagnose(
    row: dict[str, Any],
    kite=None,
    state: dict[str, Any] | None = None,
    index_df=None,
) -> dict[str, Any]:
    """Dashboard/trader status that matches what try_entries will actually do."""
    ok, why, premium, spot, conf = evaluate_candidate(row, kite, index_df)
    symbol = str(row.get("underlying") or "")
    taken = set((state or {}).get("open_auction_taken") or [])
    if symbol in taken or symbol in ((state or {}).get("open_positions") or {}):
        status = "TAKEN"
    elif not in_entry_window():
        status = "MISSED — window closed" if ok else "watching"
        if ok:
            why = f"{why}; buys stop at {ENTRY_END:%H:%M}" if why else f"buys stop at {ENTRY_END:%H:%M}"
    elif not ok:
        status = "watching"
    else:
        block = fill_block_reason(state, row, premium, kite)
        if block:
            status = "BLOCKED"
            why = block
        else:
            status = "READY TO BUY"
    return {
        "ok": bool(ok and status == "READY TO BUY"),
        "manual_ok": bool(ok) and symbol not in taken
        and symbol not in ((state or {}).get("open_positions") or {}),
        "status": status,
        "why": why or "—",
        "premium": premium,
        "spot": spot,
        "confidence": conf,
    }


def try_entries(state: dict[str, Any], kite) -> list[dict[str, Any]]:
    """Buy at most max_positions names whose tape has already gapped."""
    import live_kite
    import paper_trader
    import trading_policy

    events: list[dict[str, Any]] = []
    if not enabled() or not in_entry_window():
        return events
    if not state.get("auto_trading_enabled", True):
        return events
    if kite is None:
        return events
    halt = paper_trader.session_entry_block_reason(
        state, {}, None, trading_policy.daily_loss_cap(), None
    )
    if halt:
        return events
    if trading_policy.entry_block_reason(state):
        return events

    plan = ensure_plan(state, kite)
    gaps = discover_live_gaps(kite, list(state.get("open_auction_spots") or []), plan)
    if gaps:
        plan = plan + gaps
        state["open_auction_plan"] = plan
        paper_trader.save_state(state)
    subscribe_plan(plan)
    taken = list(state.get("open_auction_taken") or [])
    today = _today()
    already = sum(
        1
        for pos in (state.get("open_positions") or {}).values()
        if str(pos.get("entry_style") or "").startswith("open_auction")
        or str(pos.get("signal_key") or "").endswith("OPEN_AUCTION")
    )
    slots = max(0, max_positions() - already)
    if slots < 1:
        return events

    index_df = _index_frame(kite)
    scored: list[tuple[float, dict[str, Any], str, float, float, int]] = []
    for row in plan:
        symbol = str(row["underlying"])
        if symbol in state.get("open_positions", {}):
            continue
        if symbol in taken:
            continue
        key = f"{symbol}|{today}|{row['option_type']}|OPEN_AUCTION"
        if key in (state.get("seen_entries") or []):
            continue
        ok, why, premium, spot, conf = evaluate_candidate(row, kite, index_df)
        if not ok or premium <= 0.5:
            continue
        block = fill_block_reason(state, row, premium, kite)
        if block:
            skips = list(state.get("open_auction_skips") or [])
            note = f"{row.get('tradingsymbol')}: {block}"
            if note not in skips[-8:]:
                skips.append(note)
                state["open_auction_skips"] = skips[-20:]
                paper_trader.save_state(state)
            print(f"Open auction skip {note}", flush=True)
            continue
        stretch = 0.0
        prev = float(row.get("prev_premium") or 0)
        if prev > 0:
            stretch = (premium - prev) / prev
        scored.append((stretch, row, why, premium, spot, conf))
    scored.sort(key=lambda item: item[0], reverse=True)

    for _, row, why, premium, spot, conf in scored[:slots]:
        contract = plan_to_contract(row, premium)
        if contract is None:
            continue
        # Open-auction may spend a full lot even if that is more than the 30% slot.
        budget = min(float(state["cash"]), live_kite.remaining_capital(state))
        if budget < contract.cost_per_lot():
            continue
        symbol = str(row["underlying"])
        key = f"{symbol}|{today}|{row['option_type']}|OPEN_AUCTION"
        event = paper_trader.open_position(
            state,
            symbol,
            "LONG" if row["option_type"] == "CE" else "SHORT",
            spot,
            budget,
            contract.stop,
            contract.target,
            conf,
            int(row.get("score") or 0),
            key,
            contract=contract,
            spot_at_entry=spot,
            extra={
                "entry_style": "open_auction",
                "open_auction_reason": why,
            },
        )
        if event:
            taken.append(symbol)
            state["open_auction_taken"] = taken
            paper_trader.save_state(state)
            events.append(event)
            if len(events) >= slots:
                break
    return events


def manual_enter(state: dict[str, Any], row: dict[str, Any], kite=None) -> tuple[dict[str, Any] | None, str]:
    """User clicked Enter now. Ignores the 09:15–09:40 clock and the 1-name slot."""
    import live_kite
    import paper_trader

    ok, why, premium, spot, conf = evaluate_candidate(row, kite)
    if not ok:
        return None, why or "setup is not a buy"
    symbol = str(row["underlying"])
    if symbol in (state.get("open_positions") or {}):
        return None, "already in the book"
    contract = plan_to_contract(row, premium)
    if contract is None:
        return None, "premium too small"
    budget = min(float(state.get("cash") or 0), live_kite.remaining_capital(state))
    if budget < contract.cost_per_lot():
        return None, (
            f"need ₹{contract.cost_per_lot():,.0f} for 1 lot, have ₹{budget:,.0f}"
        )
    event, err = paper_trader.manual_enter_option(
        state,
        symbol,
        contract,
        conf,
        int(row.get("score") or 0),
        budget,
        spot=spot,
        extra={
            "entry_style": "open_auction_manual",
            "open_auction_reason": f"manual · {why}",
        },
    )
    if event:
        taken = list(state.get("open_auction_taken") or [])
        if symbol not in taken:
            taken.append(symbol)
            state["open_auction_taken"] = taken
            paper_trader.save_state(state)
        return event, ""
    return None, err


def summary_line() -> str:
    if not enabled():
        return "Open auction OFF"
    return (
        f"Open auction 09:15–{ENTRY_END:%H:%M} · ≤{max_positions()} name(s) · "
        f"conf ≥{min_confidence()}% (50%+ tape gap counts) · "
        f"premium gap ≥{min_premium_gap():.0%} or spot gap ≥{min_spot_gap():.1%}"
    )
