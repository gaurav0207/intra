import os
import time
from datetime import timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots
from streamlit_autorefresh import st_autorefresh

import adaptive_policy
import kite_client
import live_kite
import trading_policy
import open_auction
import options
import paper_trader
import pnl_realtime
import scanner
from data import kite_health, market_status, next_session_label
from signals import entry_side

st.set_page_config(page_title="Intraday NSE Dashboard", layout="wide")


def _cached_nfo_candles(kite, token: int, interval: str):
    key = f"nfo_ohlcv_{int(token)}_{interval}"
    now = time.time()
    hit = st.session_state.get(key) or {}
    if hit.get("df") is not None and now - float(hit.get("at") or 0) < 45:
        return hit["df"]
    df = options.fetch_nfo_intraday(kite, int(token), interval=interval)
    st.session_state[key] = {"at": now, "df": df}
    return df


def _nfo_premium_figure(df, title: str, last: float | None = None, stop=None, target=None):
    fig = go.Figure()
    if df is None or df.empty:
        return fig
    fig.add_trace(
        go.Candlestick(
            x=df.index,
            open=df["open"],
            high=df["high"],
            low=df["low"],
            close=df["close"],
            name="Premium",
        )
    )
    if last and last > 0:
        fig.add_hline(y=float(last), line_dash="solid", line_color="#7CFC9A", annotation_text=f"tape ₹{last:.2f}")
    if stop:
        fig.add_hline(y=float(stop), line_dash="dash", line_color="#FF8A80", annotation_text="stop")
    if target:
        fig.add_hline(y=float(target), line_dash="dash", line_color="#7CFC9A", annotation_text="target")
    fig.update_layout(
        title=title,
        height=340,
        xaxis_rangeslider_visible=False,
        template="plotly_dark",
        margin=dict(l=40, r=20, t=40, b=20),
        showlegend=False,
    )
    return fig


def _render_nfo_buy_charts(kite, targets: list[dict], key_prefix: str) -> None:
    """Premium candles for the option that would be bought — not the stock 5m chart."""
    if not kite or not targets:
        return
    uniq: dict[int, dict] = {}
    for row in targets:
        tok = int(row.get("token") or 0)
        if tok > 0:
            uniq[tok] = row
    rows = list(uniq.values())
    if not rows:
        return
    st.markdown("**NFO premium chart** — the option tape, not the stock 5m candle.")
    labels = [str(r.get("label") or r.get("token")) for r in rows]
    picked = (
        labels[0]
        if len(labels) == 1
        else st.selectbox("Contract to inspect", labels, key=f"{key_prefix}_pick")
    )
    row = next((r for r in rows if str(r.get("label") or r.get("token")) == picked), rows[0])
    interval = st.radio(
        "Candle",
        ("1 minute", "5 minute"),
        horizontal=True,
        key=f"{key_prefix}_iv",
        help="1m shows whether the 5m stock signal is asking you to buy a premium spike.",
    )
    kite_iv = "minute" if interval.startswith("1") else "5minute"
    df = _cached_nfo_candles(kite, int(row["token"]), kite_iv)
    last = float(row.get("premium") or 0)
    if df is None or df.empty:
        st.caption(f"No Kite history yet for {picked}.")
        return
    note = options.premium_peak_note(df, last)
    if note and "spike" in note:
        st.error(note)
    elif note:
        st.caption(note)
    st.plotly_chart(
        _nfo_premium_figure(df, picked, last, row.get("stop"), row.get("target")),
        width="stretch",
    )


def _veto_suggestion(symbol: str, option_type: str, contract: str = "") -> None:
    fresh = paper_trader.load_state()
    paper_trader.veto_setup(fresh, symbol, option_type, contract=str(contract or ""))
    st.info(f"Vetoed {contract or symbol} for today. The bot will look at the next name.")
    st.rerun()


def _render_today_vetoes(state: dict) -> None:
    keys = paper_trader.today_vetoes(state)
    if not keys:
        return
    st.caption("Vetoed today (bot will skip these and rank the next setup):")
    cols = st.columns(min(4, len(keys)))
    for idx, key in enumerate(keys):
        parts = str(key).split("|")
        label = f"{parts[1]} {parts[2]}" if len(parts) >= 3 else key
        with cols[idx % len(cols)]:
            if st.button(f"Undo {label}", key=f"unveto_{key}"):
                fresh = paper_trader.load_state()
                paper_trader.clear_veto(fresh, key)
                st.rerun()


def _show_manual_enter_result(event: dict | None, err: str, label: str) -> None:
    if event:
        st.success(
            f"Entered {event.get('contract', label)} · "
            f"{event.get('lots', event.get('quantity'))} lot(s) @ "
            f"₹{event['entry_price']:,.2f} · premium ₹{event['amount_invested']:,.2f}"
        )
        st.rerun()
        return
    st.error(f"Could not enter {label}: {err}")


DEFAULT_WATCHLIST = "RELIANCE, TCS, HDFCBANK, INFY, ICICIBANK, SBIN"

# ---------------------------------------------------------------- kite --
if "kite_token" not in st.session_state:
    st.session_state.kite_token = kite_client.load_saved_token()

st.sidebar.title("Zerodha Kite")
api_key = kite_client.api_key()
if not api_key:
    st.sidebar.error("Set KITE_API_KEY in .env")
else:
    st.sidebar.caption(f"API key `{api_key[:4]}…{api_key[-4:]}`")

# If Kite redirects back to this deployed dashboard, exchange the request token
# automatically so the user only has to click the morning email link and login.
redirect_request_token = st.query_params.get("request_token")
if redirect_request_token and api_key and not st.session_state.kite_token:
    secret_from_env = kite_client.api_secret()
    if secret_from_env:
        try:
            session = kite_client.exchange_request_token(
                str(redirect_request_token),
                secret_from_env,
                api_key,
            )
            st.session_state.kite_token = session["access_token"]
            st.query_params.clear()
            st.success("Kite login completed. The headless trader will connect on its next cycle.")
            st.rerun()
        except Exception as exc:  # noqa: BLE001
            st.error(f"Automatic Kite login failed: {exc}")
    else:
        st.warning("Kite redirected successfully, but KITE_API_SECRET is not configured.")

kite_ok = False
kite = None
if st.session_state.kite_token:
    try:
        kite = kite_client.make_kite(st.session_state.kite_token)
        profile = kite.profile()
        kite_ok = True
        st.sidebar.success(f"Connected as {profile.get('user_name', profile.get('user_id', 'Kite'))}")
        if st.sidebar.button("Disconnect Kite"):
            kite_client.clear_token()
            st.session_state.kite_token = None
            st.rerun()
    except Exception as exc:  # noqa: BLE001
        if kite_client.is_session_dead(exc):
            st.sidebar.warning(f"Kite login expired, log in again: {exc}")
            kite_client.clear_token()
            st.session_state.kite_token = None
        else:
            st.sidebar.warning(f"Kite unreachable, keeping the session: {exc}")
        kite = None

if not kite_ok:
    st.sidebar.markdown("Login is required once per day for live Kite candles.")
    if api_key:
        st.sidebar.link_button("Open Kite login", kite_client.login_url())
    secret = st.sidebar.text_input(
        "API secret",
        value=kite_client.api_secret(),
        type="password",
        help="From Kite developer console. Stored only in this session / .env, not in git.",
    )
    request_token = st.sidebar.text_input(
        "Request token",
        help="After login, copy request_token from the redirect URL.",
    )
    if st.sidebar.button("Connect Kite", disabled=not (api_key and secret and request_token)):
        try:
            data = kite_client.exchange_request_token(request_token, secret, api_key)
            st.session_state.kite_token = data["access_token"]
            st.rerun()
        except Exception as exc:  # noqa: BLE001
            st.sidebar.error(f"Kite login failed: {exc}")

st.sidebar.markdown("---")
st.sidebar.title("Settings")

watchlist_raw = st.sidebar.text_area(
    "Watchlist (NSE symbols, comma separated)",
    DEFAULT_WATCHLIST,
    height=80,
    help="For viewing only. The paper trader buys from its own universe, not from this list.",
)
watchlist = [s.strip().upper() for s in watchlist_raw.split(",") if s.strip()]

refresh_secs = st.sidebar.slider("Auto-refresh every (seconds)", 15, 300, 60, step=15)
interval = "5m"
stop_mult = 1.5
target_mult = 2.5
long_only = False

st.sidebar.markdown("---")
st.sidebar.title("Paper trading")
initial_cash = st.sidebar.number_input(
    "Starting paper cash (₹)",
    min_value=5_000,
    max_value=10_000_000,
    value=int(paper_trader.default_initial_cash()),
    step=5_000,
    disabled=paper_trader.STATE_PATH.exists(),
)
trade_universe = adaptive_policy.AI_UNIVERSE
st.sidebar.info(
    "Paper book buys ATM NFO calls/puts on the scanned underlyings. "
    "LONG → CE, SHORT → PE. No cash-share entries."
)
require_kite_for_entry = True
headless_executor = os.getenv("PAPER_EXECUTOR", "dashboard") == "headless"
st.sidebar.caption("New entries buy ATM NFO options only. Cash shares are never opened.")
if headless_executor:
    st.sidebar.success("Headless trader is responsible for entries, exits, and emails.")
if paper_trader.email_ready():
    st.sidebar.success("Email alerts configured")
    if st.sidebar.button("Send test email"):
        sent, detail = paper_trader.send_email(
            "Intraday dashboard test",
            "Email alerts are configured. This is a test; no paper trade was made.",
        )
        if sent:
            st.sidebar.success(detail)
        else:
            st.sidebar.error(detail)
else:
    st.sidebar.warning("Email alerts not configured — trades will still be recorded")
paper_state = paper_trader.load_state()

_auto_saved = bool(paper_state.get("auto_trading_enabled", False))
_live_saved = bool(paper_state.get("live_trading_enabled", False))
_sl_saved = bool(paper_state.get("auto_stop_loss_exit", False))
for _name, _saved in (
    ("auto_trading", _auto_saved),
    ("live_trading", _live_saved),
    ("auto_stop_loss", _sl_saved),
):
    _disk = f"_{_name}_disk"
    if st.session_state.get(_disk) != _saved:
        st.session_state[_name] = _saved
        st.session_state[_disk] = _saved
auto_trading = st.sidebar.toggle(
    "Automatic trading",
    key="auto_trading",
    help="Default OFF. When ON, the bot buys without your click. "
    "When OFF, only Enter now opens a position.",
)
live_trading = st.sidebar.toggle(
    "Real Zerodha orders",
    key="live_trading",
    help="When ON, paper BUY/EXIT is mirrored to your Zerodha account (₹50k premium cap). "
    "Default OFF.",
)
auto_stop_loss = st.sidebar.toggle(
    "Auto stop/target exit",
    key="auto_stop_loss",
    help="Default OFF. When OFF, stop, target, and reversal ask you to click Exit. "
    "15:15 square-off still auto-exits MIS.",
)
if (
    auto_trading != _auto_saved
    or live_trading != _live_saved
    or auto_stop_loss != _sl_saved
):
    paper_state["auto_trading_enabled"] = auto_trading
    paper_state["live_trading_enabled"] = live_trading
    paper_state["auto_stop_loss_exit"] = auto_stop_loss
    paper_trader.save_state(paper_state)

if auto_trading:
    st.sidebar.error("Automatic trading ON — the bot will BUY without the Enter button.")
else:
    st.sidebar.warning("Manual entries only — the bot will not buy unless you click Enter now.")
if not auto_stop_loss:
    st.sidebar.warning("Manual exits — stop/target/reversal ask you. 15:15 still squares off.")
else:
    st.sidebar.error("Auto stop/target ON — the bot will flatten those without asking.")
if live_trading:
    _funds = live_kite.fund_snapshot(paper_state, force=True)
    _fund_src = "Zerodha equity margin" if _funds["source"] == "kite" else "configured cap"
    st.sidebar.error(
        "Real Zerodha orders ON. You can lose money. "
        f"Cap ₹{_funds['cap']:,.0f} · "
        f"deployed ₹{_funds['deployed']:,.0f} · "
        f"free ₹{_funds['free']:,.0f} ({_fund_src})"
    )
else:
    st.sidebar.caption(live_kite.status_line(paper_state))

st.sidebar.caption(trading_policy.policy_summary())
if trading_policy.entry_block_reason(paper_state):
    st.sidebar.warning(trading_policy.entry_block_reason(paper_state))

st.sidebar.caption(
    f"Paper cash ₹{float(paper_state['cash']):,.2f} · "
    f"starting ₹{float(paper_state.get('initial_cash', paper_state['cash'])):,.2f} · "
    f"{len(paper_state['open_positions'])} open · NFO options"
)
try:
    import kite_ws

    if kite_ws.is_running():
        st.sidebar.caption("Zerodha WebSocket: open legs + Will-buy-next + 9:15 auction")
    elif kite_ok:
        if kite_ws.start():
            st.sidebar.caption("Zerodha WebSocket: open legs + Will-buy-next + 9:15 auction")
except Exception:
    pass
st.sidebar.caption(
    f"Entries {paper_trader.ENTRY_START:%H:%M}–{paper_trader.ENTRY_END:%H:%M} IST · "
    f"square-off {paper_trader.SQUARE_OFF:%H:%M} IST"
)
st.sidebar.caption(open_auction.summary_line())

st.sidebar.markdown("---")
st.sidebar.caption(
    "Signals combine VWAP, EMA, RSI, MACD, volume, Bollinger, Supertrend, ADX, "
    "stochastic, opening-range, prior-day levels, engulfing, 15m trend, and Nifty relative strength. "
    "Still not a guarantee — Kite is live-ish, Yahoo is delayed."
)

st_autorefresh(interval=refresh_secs * 1000, key="auto_refresh")

# ------------------------------------------------------------------ header --
status = market_status()
status_label = {
    "open": ("🟢 Market open", "green"),
    "closed": ("🔴 Market closed (after hours)", "red"),
    "pre_open": ("🟡 Pre-market", "orange"),
    "closed_weekend": ("🔴 Market closed (weekend)", "red"),
}[status]

st.title("📈 Intraday NFO Options Dashboard")
c1, c2, c3 = st.columns([1, 2, 2])
c1.markdown(f"**{status_label[0]}**")
c2.caption(f"Last updated: {pd.Timestamp.now(tz='Asia/Kolkata').strftime('%H:%M:%S IST')}")
c3.caption(f"Next session forecast: **{next_session_label()}**")
src = "Zerodha Kite" if kite_ok else "Yahoo Finance (delayed)"
st.caption(f"Data source: **{src}**")

st.warning(
    "Educational tool only — not investment advice. More factors improve confirmation, "
    "they do not predict the future. Always size positions and set your own risk limits.",
    icon="⚠️",
)

# --------------------------------------------------------------- fetch all --
token_key = st.session_state.kite_token or ""


# The trader scans its own universe; the watchlist is only rendered.
# Held positions are always scanned so their exits keep working.
scan_symbols = sorted(set(trade_universe) | set(watchlist) | set(paper_state["open_positions"]))


@st.cache_data(ttl=refresh_secs, show_spinner="Scanning stocks…")
def run_scan(symbols: tuple[str, ...], interval: str, token: str, stop: float, target: float, longs: bool):
    client = kite_client.make_kite(token) if token else None
    return scanner.scan(
        list(symbols), interval, kite=client, stop_mult=stop, target_mult=target, long_only=longs
    )


signals, chart_data, errors = run_scan(
    tuple(scan_symbols), interval, token_key, stop_mult, target_mult, long_only
)

kite_client_for_health = kite_client.make_kite(token_key) if token_key else None
if kite_client_for_health:
    try:
        from data import fetch_index
        from indicators import compute_all

        raw_index = fetch_index(interval=interval, kite=kite_client_for_health)
        if not raw_index.empty:
            chart_data["NIFTY 50"] = compute_all(raw_index)
    except Exception:  # noqa: BLE001
        pass
kite_data_ok, kite_data_reason = kite_health(kite_client_for_health)
if kite_data_ok and signals and all(sig.data_source != "kite" for sig in signals.values()):
    kite_data_ok = False
    kite_data_reason = "Kite login exists, but every candle came from Yahoo Finance instead of Zerodha."
fetch_alert = (
    None
    if headless_executor
    else paper_trader.sync_kite_fetch(paper_state, kite_data_ok, kite_data_reason)
)
if fetch_alert is not None:
    sent, detail = fetch_alert
    if sent:
        st.error(f"Could not fetch Zerodha data — an email was sent to ALERT_EMAIL. {kite_data_reason}")
    else:
        st.error(f"Could not fetch Zerodha data ({kite_data_reason}). Alert email failed: {detail}")
elif not kite_data_ok:
    st.warning(f"Zerodha data is unavailable: {kite_data_reason}")

rows = [
    {
        "Symbol": sig.symbol,
        "Price": sig.price,
        "Do now": sig.what_to_do,
        "Conf %": sig.confidence,
        "Enter at": sig.entry,
        "Get out (stop)": sig.stop_loss,
        "Take profit": sig.target,
        "Signal": sig.action,
        "Tomorrow": sig.tomorrow_bias,
        "Tomorrow plan": sig.tomorrow_plan,
    }
    for sym in watchlist
    if (sig := signals.get(sym)) is not None
]

# --------------------------------------------------------- paper portfolio --
tradable_signals = {
    sym: sig
    for sym, sig in signals.items()
    if sym in trade_universe or sym in paper_state["open_positions"]
}
prices = {symbol: sig.price for symbol, sig in signals.items()}
wanted = {}
for sym, sig in tradable_signals.items():
    side = entry_side(sig)
    if side is not None:
        wanted[sym] = (side, float(sig.price))
contracts = {}
if kite_client_for_health:
    ranked_wanted = sorted(wanted, key=lambda s: -tradable_signals[s].confidence)[:12]
    contracts = options.resolve_many(
        kite_client_for_health, {s: wanted[s] for s in ranked_wanted}
    )
    options.subscribe_contract_tokens(contracts)
    options.apply_live_premiums(contracts)
option_marks: dict[str, float] = {}
open_contracts = [
    pos["contract"]
    for pos in paper_state["open_positions"].values()
    if pos.get("contract")
]
if kite_client_for_health and open_contracts:
    if live_kite.is_enabled(paper_state):
        exit_marks = options.fetch_option_exit_marks(
            kite_client_for_health, open_contracts
        )
        for symbol, pos in paper_state["open_positions"].items():
            mark = exit_marks.get(str(pos.get("contract") or ""))
            if mark:
                option_marks[symbol] = mark
                prices[symbol] = mark
    else:
        token_map = {
            pos["contract"]: int(pos["instrument_token"])
            for pos in paper_state["open_positions"].values()
            if pos.get("contract") and pos.get("instrument_token")
        }
        ltps = options.fetch_option_ltps(
            kite_client_for_health, open_contracts, instrument_tokens=token_map
        )
        for symbol, pos in paper_state["open_positions"].items():
            last = ltps.get(pos.get("contract", ""))
            if last:
                option_marks[symbol] = last
                prices[symbol] = last
zerodha_snap: dict | None = None
if live_kite.is_enabled(paper_state) and kite_client_for_health:
    zerodha_snap = live_kite.zerodha_portfolio_snapshot(
        paper_state, force_refresh=True
    )
    prices = live_kite.apply_broker_marks_to_prices(paper_state, prices, zerodha_snap)
st.session_state["_pnl_base_prices"] = dict(prices)
auction_events: list[dict] = []
if open_auction.enabled() and kite_client_for_health:
    if open_auction.in_prep_window() or open_auction.in_entry_window():
        if headless_executor:
            open_auction.subscribe_plan(paper_state.get("open_auction_plan") or [])
        else:
            open_auction.ensure_plan(paper_state, kite_client_for_health)
            paper_state = paper_trader.load_state()
    if not headless_executor and open_auction.in_entry_window():
        auction_events = open_auction.try_entries(paper_state, kite_client_for_health)
        paper_state = paper_trader.load_state()
adaptive_plan = adaptive_policy.decide(
    paper_state, tradable_signals, chart_data, prices, contracts=contracts
)
options.apply_live_premiums(adaptive_plan.candidate_contracts)
if headless_executor:
    paper_events = []
    paper_state = paper_trader.load_state()
else:
    paper_events = paper_trader.run_cycle(
        paper_state,
        tradable_signals,
        chart_data,
        enabled=bool(paper_state.get("auto_trading_enabled", True)),
        market_open=status == "open",
        budget_per_trade=0,
        max_positions=adaptive_plan.max_positions,
        minimum_confidence=adaptive_plan.confidence_required,
        require_kite=require_kite_for_entry,
        candidate_budgets=adaptive_plan.candidate_budgets,
        candidate_contracts=adaptive_plan.candidate_contracts,
        option_marks=option_marks,
        daily_profit_target=adaptive_plan.daily_profit_target,
        daily_loss_limit=adaptive_plan.daily_loss_limit,
    )
paper_events = list(auction_events) + list(paper_events)

st.subheader("Paper portfolio — simulated NFO options only")
if paper_state.get("auto_trading_enabled"):
    st.error(
        "Automatic trading is **ON** — the bot will buy without **Enter now**. "
        "Turn it OFF in the sidebar for manual execution."
    )
else:
    st.warning(
        "Manual execution: the bot will **not** buy unless you click **Enter now**. "
        "Stop/target ask you to **Exit**. 15:15 IST still squares off MIS."
    )
if live_kite.is_enabled(paper_state):
    st.error("Real Zerodha mirroring is **ON** — paper fills also send live NFO orders.")
st.info(
    f"Adaptive plan: **{adaptive_plan.regime}** · "
    f"confidence ≥ {adaptive_plan.confidence_required}% · "
    f"up to {adaptive_plan.max_positions} option positions · "
    f"daily objective ₹{adaptive_plan.daily_profit_target:,.0f} "
    f"(sizing guide, no hard stop) · loss stop ₹{adaptive_plan.daily_loss_limit:,.0f}"
)
_now_ist = pd.Timestamp.now(tz="Asia/Kolkata").time()
if open_auction.enabled() and open_auction.in_entry_window():
    st.caption("Open auction window: buying from the option tape until 09:30 IST.")
elif status != "open":
    st.caption(f"Market {status.replace('_', ' ')} — open auction arms at 09:00, tape buys 09:15–09:30.")
elif _now_ist < paper_trader.ENTRY_START:
    st.caption("Regular 5m entries start at 09:30 IST. Open auction (if armed) can buy from 09:15.")
elif _now_ist >= paper_trader.ENTRY_END:
    st.caption(f"Entry window closed at {paper_trader.ENTRY_END:%H:%M} IST — exits only until square-off.")
else:
    st.caption(f"Entry window open until {paper_trader.ENTRY_END:%H:%M} IST.")


@st.fragment(run_every=timedelta(seconds=pnl_realtime.PNL_REFRESH_SECONDS))
def render_realtime_pnl() -> None:
    state = paper_trader.load_state()
    paper_trader.sync_with_broker(state)
    state = paper_trader.load_state()
    base = st.session_state.get("_pnl_base_prices") or {}
    prices, mark_note = pnl_realtime.ws_prices_for_state(state, base)
    summary = paper_trader.portfolio_summary(state, prices)

    st.markdown("#### Paper P&L (live marks)")
    st.caption(mark_note)
    p1, p2, p3, p4, p5 = st.columns(5)
    p1.metric(
        "Total equity",
        f"₹{summary['equity']:,.2f}",
        f"₹{summary['total_pnl']:+,.2f}",
    )
    p2.metric("Cash", f"₹{summary['cash']:,.2f}")
    p3.metric("Invested value", f"₹{summary['market_value']:,.2f}")
    p4.metric("Realized P&L", f"₹{summary['realized_pnl']:+,.2f}")
    p5.metric("Open P&L", f"₹{summary['unrealized_pnl']:+,.2f}")

    leg_rows = pnl_realtime.paper_open_leg_rows(state, prices)
    if leg_rows:
        st.dataframe(pd.DataFrame(leg_rows), width="stretch", hide_index=True)

    asks = paper_trader.pending_stop_asks(state)
    if asks:
        st.error(
            "Stop-loss hit — the bot did **not** exit. Click Exit if you want out: "
            + ", ".join(
                f"{a['contract']} (₹{a['mark']})" if a.get("mark") else str(a["contract"])
                for a in asks
            )
        )
        ask_cols = st.columns(min(3, len(asks)))
        for idx, ask in enumerate(asks):
            with ask_cols[idx % len(ask_cols)]:
                if st.button(
                    f"Exit {ask['contract']}",
                    type="primary",
                    key=f"stop_ask_exit_{ask['symbol']}",
                ):
                    exited = paper_trader.manual_exit_positions(
                        [ask["symbol"]],
                        prices,
                        float(state.get("initial_cash") or 0),
                        reason=str(ask.get("reason") or "Manual exit after stop ask"),
                    )
                    if exited:
                        st.success(f"Exit sent for {ask['contract']}")
                        st.rerun()
                    else:
                        st.warning("Could not exit — reload if it already closed.")

    if not live_kite.is_enabled(state) or not st.session_state.kite_token:
        return

    st.markdown("#### Zerodha P&L")
    now = time.time()
    if now - float(st.session_state.get("z_snap_at") or 0) > pnl_realtime.BROKER_LEGS_REFRESH_SECONDS:
        st.session_state["z_snap"] = live_kite.zerodha_portfolio_snapshot(
            state, force_refresh=True
        )
        st.session_state["z_snap_at"] = now
    z_snap = st.session_state.get("z_snap") or {}

    if now - float(st.session_state.get("broker_legs_at") or 0) > pnl_realtime.BROKER_LEGS_REFRESH_SECONDS:
        try:
            kite = kite_client.make_kite(st.session_state.kite_token)
            st.session_state["broker_legs"] = pnl_realtime.fetch_broker_open_legs(kite)
            st.session_state["broker_legs_at"] = now
        except Exception as exc:  # noqa: BLE001
            st.session_state["broker_legs_error"] = str(exc)

    broker_legs = st.session_state.get("broker_legs") or []
    broker_open, broker_rows, broker_note = pnl_realtime.broker_open_pnl_from_ws(broker_legs)

    if z_snap and not z_snap.get("error"):
        st.caption(
            f"Margin snapshot {z_snap.get('fetched_at', '—')} (refreshed ~"
            f"{pnl_realtime.BROKER_LEGS_REFRESH_SECONDS}s) · "
            f"Open leg marks: **{broker_note}** every {pnl_realtime.PNL_REFRESH_SECONDS}s"
        )
        z1, z2, z3, z4, z5 = st.columns(5)
        z1.metric("Total equity", f"₹{z_snap['equity']:,.2f}")
        z2.metric("Cash (margin)", f"₹{z_snap['cash']:,.2f}")
        z3.metric("Invested (NFO)", f"₹{z_snap['invested_value']:,.2f}")
        z4.metric("Realized today", f"₹{z_snap['realized_pnl_today']:+,.2f}")
        z5.metric("Open P&L (live)", f"₹{broker_open:+,.2f}")
        st.caption(
            f"Day P&L (Kite day book): ₹{z_snap.get('day_pnl', 0):+,.2f} · "
            f"REST open P&L at snapshot: ₹{z_snap.get('open_pnl', 0):+,.2f}"
        )
    elif z_snap.get("error"):
        st.warning(f"Zerodha margin snapshot: {z_snap['error']}")

    if broker_rows:
        st.dataframe(pd.DataFrame(broker_rows), width="stretch", hide_index=True)
    elif live_kite.is_enabled(state):
        st.caption("No open NFO MIS legs on Zerodha.")


render_realtime_pnl()

if paper_events:
    for event in paper_events:
        if event["type"] in {"BUY", "SHORT"}:
            st.success(
                f"Paper {event['type']}: {event.get('contract', event['symbol'])} · "
                f"{event.get('lots', event['quantity'])} lots @ "
                f"₹{event['entry_price']:,.2f} · premium ₹{event['amount_invested']:,.2f}"
            )
        elif event["type"] == "STOP_ASK":
            st.error(
                f"Stop-loss ask: {event.get('contract', event.get('symbol'))} @ "
                f"₹{event.get('mark', 0):,.2f} — {event.get('reason')}. "
                "The bot did not exit. Use Exit below if you want out."
            )
        else:
            st.info(
                f"Paper EXIT: {event['symbol']} · received ₹{event['proceeds']:,.2f} · "
                f"P&L ₹{event['pnl']:+,.2f} ({event['pnl_pct']:+.2f}%)"
            )

session_halt = paper_trader.trading_halt_reason(
    paper_state,
    prices,
    adaptive_plan.daily_profit_target,
    adaptive_plan.daily_loss_limit,
)
auction_plan = paper_state.get("open_auction_plan") or []
if open_auction.enabled() and (auction_plan or open_auction.in_prep_window() or open_auction.in_entry_window()):
    st.subheader("Open auction — 09:15 option tape")
    st.caption(
        "Pre-picked ATM from the daily bias. Auto-buy is 09:15–09:40 when the option "
        f"has gapped and confidence is ≥{open_auction.min_confidence()}% "
        "(a 50%+ premium gap counts). An **Enter now** button appears when the tape "
        "says go in — including after 09:40 — so you can still take it."
    )
    if auction_plan:
        open_auction.subscribe_plan(auction_plan)
        auction_rows = []
        auction_ready: list[tuple[dict, dict]] = []
        taken = set(paper_state.get("open_auction_taken") or [])
        auction_index = (
            open_auction._index_frame(kite_client_for_health)
            if kite_client_for_health
            else None
        )
        for row in auction_plan:
            info = open_auction.diagnose(
                row, kite_client_for_health, paper_state, auction_index
            )
            auction_rows.append(
                {
                    "Contract": row.get("tradingsymbol"),
                    "Bias": row.get("bias"),
                    "Type": row.get("option_type"),
                    "Conf %": info["confidence"] or None,
                    "Prior premium": row.get("prev_premium") or None,
                    "Tape ₹": round(info["premium"], 2) if info["premium"] else None,
                    "Spot": round(info["spot"], 2) if info["spot"] else None,
                    "Spot ref": row.get("spot_ref"),
                    "Status": (
                        "TAKEN"
                        if row.get("underlying") in taken
                        else info["status"]
                    ),
                    "Why": info["why"] or "—",
                }
            )
            if info.get("manual_ok"):
                auction_ready.append((row, info))
        st.dataframe(pd.DataFrame(auction_rows), width="stretch", hide_index=True)
        auction_charts = []
        for plan_row, table_row in zip(auction_plan, auction_rows):
            auction_charts.append(
                {
                    "label": f"{plan_row.get('tradingsymbol')} ({plan_row.get('underlying')})",
                    "token": int(plan_row.get("instrument_token") or 0),
                    "premium": table_row.get("Tape ₹"),
                    "stop": None,
                    "target": None,
                }
            )
        _render_nfo_buy_charts(kite_client_for_health, auction_charts, "auction_nfo")
        if auction_ready:
            st.markdown("**Enter now** — tape says go in; you confirm the fill.")
            if live_kite.is_enabled(paper_state):
                st.caption("Live mirroring is ON — this button also sends the Zerodha MIS buy.")
            cols = st.columns(min(3, len(auction_ready)))
            for idx, (row, _info) in enumerate(auction_ready):
                label = str(row.get("tradingsymbol") or row.get("underlying"))
                with cols[idx % len(cols)]:
                    if st.button(
                        f"Enter {label} now",
                        type="primary",
                        key=f"manual_auction_{row.get('underlying')}_{label}",
                    ):
                        fresh = paper_trader.load_state()
                        event, err = open_auction.manual_enter(
                            fresh, row, kite_client_for_health
                        )
                        _show_manual_enter_result(event, err, label)
                    if st.button(
                        f"Veto {label}",
                        key=f"veto_auction_{row.get('underlying')}_{label}",
                    ):
                        _veto_suggestion(
                            str(row.get("underlying")),
                            str(row.get("option_type") or ""),
                            label,
                        )
        skippable = [
            r
            for r in auction_plan
            if r.get("underlying") not in taken
            and not paper_trader.is_vetoed(
                paper_state, str(r.get("underlying")), str(r.get("option_type") or "")
            )
            and not any(
                r.get("underlying") == ready_row.get("underlying")
                for ready_row, _ in auction_ready
            )
        ]
        if skippable:
            st.caption("Veto a name so auction looks at the next tape:")
            vcols = st.columns(min(4, len(skippable)))
            for idx, row in enumerate(skippable):
                label = str(row.get("tradingsymbol") or row.get("underlying"))
                with vcols[idx % len(vcols)]:
                    if st.button(
                        f"Veto {label}",
                        key=f"veto_auction_watch_{row.get('underlying')}_{label}",
                    ):
                        _veto_suggestion(
                            str(row.get("underlying")),
                            str(row.get("option_type") or ""),
                            label,
                        )
        _render_today_vetoes(paper_state)
    elif open_auction.in_prep_window():
        st.caption("Arming overnight ATM list… headless trader builds this from 09:00 IST.")
    else:
        st.caption("No strong daily CE/PE was armed for this session.")

st.session_state["_will_buy"] = {
    "contracts": contracts,
    "candidate_contracts": adaptive_plan.candidate_contracts,
    "candidate_budgets": adaptive_plan.candidate_budgets,
    "confidence": adaptive_plan.confidence_required,
    "max_positions": adaptive_plan.max_positions,
    "in_window": status == "open",
    "kite_ok": kite_data_ok,
    "daily_profit_target": adaptive_plan.daily_profit_target,
    "daily_loss_limit": adaptive_plan.daily_loss_limit,
    "regime": adaptive_plan.regime,
    "session_halt": session_halt,
    "last_plan_at": paper_state.get("last_option_plan_at"),
}


@st.fragment(run_every=timedelta(seconds=pnl_realtime.PNL_REFRESH_SECONDS))
def render_will_buy_next() -> None:
    state = paper_trader.load_state()
    ctx = st.session_state.get("_will_buy") or {}
    search = dict(ctx.get("contracts") or {})
    chosen = dict(ctx.get("candidate_contracts") or {})
    options.subscribe_contract_tokens(search)
    options.subscribe_contract_tokens(chosen)
    extra = pnl_realtime.candidate_tokens_from_plan(state.get("last_option_plan") or [])
    pnl_realtime.subscribe_open_instruments(state, extra_tokens=extra)
    options.apply_live_premiums(search)
    hits = options.apply_live_premiums(chosen)

    plan_rows = state.get("last_option_plan") or []
    ws_note = ""
    if chosen:
        plan_rows = paper_trader.option_plan_rows(
            state,
            tradable_signals,
            chosen,
            ctx.get("candidate_budgets") or {},
            chart_data=chart_data,
            minimum_confidence=int(ctx.get("confidence") or 0),
            max_positions=int(ctx.get("max_positions") or 5),
            in_window=bool(ctx.get("in_window")),
            kite_ok=bool(ctx.get("kite_ok")),
            daily_profit_target=ctx.get("daily_profit_target"),
            daily_loss_limit=ctx.get("daily_loss_limit"),
            prices=st.session_state.get("_pnl_base_prices") or {},
        )
        ws_note = pnl_realtime.candidate_ws_note(hits)
    elif plan_rows:
        plan_rows, ws_note = pnl_realtime.overlay_ws_on_plan_rows(plan_rows)

    st.subheader("Will buy next — NFO options only")
    halt = ctx.get("session_halt")
    if halt:
        st.warning(f"No new option entries this session: {halt}.")
    if ws_note:
        st.caption(ws_note)
    if plan_rows:
        show = pd.DataFrame(plan_rows)
        drop = [
            c
            for c in show.columns
            if str(c).startswith("_") or c == "instrument_token"
        ]
        st.dataframe(show.drop(columns=drop, errors="ignore"), width="stretch", hide_index=True)
        will_charts = []
        for row in plan_rows:
            tok = int(row.get("instrument_token") or 0)
            if tok <= 0 and row.get("Underlying") in chosen:
                tok = int(getattr(chosen[row["Underlying"]], "instrument_token", 0) or 0)
            will_charts.append(
                {
                    "label": f"{row.get('Contract')} ({row.get('Underlying')})",
                    "token": tok,
                    "premium": row.get("Premium"),
                    "stop": row.get("Stop"),
                    "target": row.get("Target"),
                }
            )
        _render_nfo_buy_charts(kite_client_for_health, will_charts, "willbuy_nfo")
        ready = [
            row
            for row in plan_rows
            if str(row.get("Status") or "").startswith("WILL BUY")
            and row.get("Underlying") in chosen
        ]
        if ready:
            st.markdown("**Enter now** — or **Veto** so the next-best name can take the slot.")
            if live_kite.is_enabled(state):
                st.caption("Live mirroring is ON — this button also sends the Zerodha MIS buy.")
            cols = st.columns(min(3, len(ready)))
            for idx, row in enumerate(ready):
                symbol = str(row["Underlying"])
                label = str(row.get("Contract") or symbol)
                with cols[idx % len(cols)]:
                    if st.button(
                        f"Enter {label} now",
                        type="primary",
                        key=f"manual_willbuy_{symbol}_{label}",
                    ):
                        fresh = paper_trader.load_state()
                        contract = chosen[symbol]
                        budget = float(
                            (ctx.get("candidate_budgets") or {}).get(
                                symbol, fresh.get("cash") or 0
                            )
                        )
                        event, err = paper_trader.manual_enter_option(
                            fresh,
                            symbol,
                            contract,
                            int(row.get("Conf %") or 0),
                            int(row.get("Score") or 0),
                            budget,
                            extra={"entry_style": "manual", "manual_reason": row.get("Status")},
                        )
                        _show_manual_enter_result(event, err, label)
                    if st.button(
                        f"Veto {label}",
                        key=f"veto_willbuy_{symbol}_{label}",
                    ):
                        _veto_suggestion(symbol, str(row.get("Type") or ""), label)
        watching = [
            row
            for row in plan_rows
            if not str(row.get("Status") or "").startswith("WILL BUY")
            and row.get("Underlying")
            and row.get("Type")
        ]
        if watching:
            st.caption("Veto a spike so it drops and the next name can surface:")
            wcols = st.columns(min(4, len(watching)))
            for idx, row in enumerate(watching):
                symbol = str(row["Underlying"])
                label = str(row.get("Contract") or symbol)
                with wcols[idx % len(wcols)]:
                    if st.button(
                        f"Veto {label}",
                        key=f"veto_watch_{symbol}_{label}",
                    ):
                        _veto_suggestion(symbol, str(row.get("Type") or ""), label)
        _render_today_vetoes(state)
        if ctx.get("last_plan_at"):
            st.caption(f"Last plan from the headless trader: {ctx['last_plan_at']}")
    else:
        watch_rows = paper_trader.option_watch_rows(
            tradable_signals,
            search,
            int(ctx.get("confidence") or 0),
            str(ctx.get("regime") or ""),
            state=state,
        )
        if watch_rows and not halt:
            st.caption(
                "No premium sized for a fill this refresh — closest entry-eligible setups:"
            )
            st.dataframe(pd.DataFrame(watch_rows), width="stretch", hide_index=True)
            vcols = st.columns(min(4, len(watch_rows)))
            for idx, row in enumerate(watch_rows):
                symbol = str(row.get("Underlying") or "")
                label = str(row.get("Contract") or symbol)
                side = str(row.get("Side") or "")
                option_type = "PE" if side == "SHORT" else "CE"
                if not symbol:
                    continue
                with vcols[idx % len(vcols)]:
                    if st.button(f"Veto {label}", key=f"veto_closest_{symbol}_{label}"):
                        _veto_suggestion(symbol, option_type, label)
            watch_charts = []
            for row in watch_rows:
                contract = search.get(row.get("Underlying"))
                if contract is None:
                    continue
                watch_charts.append(
                    {
                        "label": f"{contract.tradingsymbol} ({row.get('Underlying')})",
                        "token": int(contract.instrument_token or 0),
                        "premium": contract.premium,
                        "stop": contract.stop,
                        "target": contract.target,
                    }
                )
            _render_nfo_buy_charts(kite_client_for_health, watch_charts, "watch_nfo")
        else:
            st.caption("No ATM option is sized for a fill on this cycle.")
            if not halt:
                st.caption(
                    "The scan has no **ENTER/HOLD** short/long with room left toward target "
                    "(high-confidence **WAIT** rows in the table above are not buy signals yet)."
                )


render_will_buy_next()

paper_trader.sync_with_broker(paper_state)
paper_state = paper_trader.load_state()
if paper_state["open_positions"]:
    position_rows = []
    for symbol, position in paper_state["open_positions"].items():
        status = str(position.get("order_status") or "Open")
        current = float(position["entry_price"])
        if position.get("contract"):
            mark = float(prices.get(symbol, position["entry_price"]))
            entry = float(position["entry_price"])
            current = entry if mark > max(entry * 8, entry + 50) else mark
        else:
            current = float(prices.get(symbol, position["entry_price"]))
        entry_px = float(position["entry_price"])
        if position.get("contract") or position.get("side") in {"CE", "PE"}:
            pnl = (current - entry_px) * position["quantity"]
        elif position.get("side", "LONG") == "SHORT":
            pnl = (entry_px - current) * position["quantity"]
        else:
            pnl = current * position["quantity"] - position["amount_invested"]
        invested = float(position.get("amount_invested") or 0)
        pnl_pct = (pnl / invested) * 100 if invested and status == "Executed" else None
        position_rows.append(
            {
                "Status": status,
                "Contract": position.get("contract", symbol),
                "Underlying": symbol,
                "Type": position.get("side", "LONG"),
                "Lots": position.get("lots", position["quantity"]),
                "Premium in": position["entry_price"],
                "Premium now": round(current, 2) if status == "Executed" else None,
                "Paid": position["amount_invested"],
                "P&L ₹": round(pnl, 2) if status == "Executed" else None,
                "P&L %": round(pnl_pct, 2) if pnl_pct is not None else None,
                "Entered": position["entry_time"],
            }
        )
    st.dataframe(pd.DataFrame(position_rows), width="stretch", hide_index=True)
    sl_col, _ = st.columns([1, 3])
    with sl_col:
        if paper_state.get("auto_stop_loss_exit"):
            if st.button(
                "Disable auto stop/target exit",
                type="primary",
                key="disable_auto_stop_loss",
            ):
                paper_trader.set_auto_stop_loss_exit(paper_state, False)
                st.rerun()
        else:
            st.caption("Auto stop/target is OFF. The bot will ask — click Exit if you want out.")
    stop_asks = paper_trader.pending_stop_asks(paper_state)
    if stop_asks:
        st.error(
            "Stop-loss hit — waiting for you: "
            + ", ".join(
                f"{a['contract']} ({a['reason']})"
                for a in stop_asks
            )
        )
    st.markdown("**Manual exit**")
    if live_kite.is_enabled(paper_state):
        st.caption(
            "Places a **Zerodha LIMIT SELL** only for qty you **already hold long** (same **MIS** product). "
            "If Kite says **need more margin**, you may be selling more than your open long (naked short) — "
            "use Kite → Positions and exit **exactly** that qty with **Limit**, not Market. "
            "Paper closes only after the broker order is accepted."
        )
    else:
        st.caption(
            "Paper only — uses the latest option premium shown above. "
            "Turn on live mirroring in the sidebar to send matching LIMIT exits to Zerodha."
        )
    exit_all_col, _ = st.columns([1, 3])
    with exit_all_col:
        exit_all = st.button(
            "Exit all open positions",
            type="primary",
            key="manual_exit_all",
        )
    if exit_all:
        exited = paper_trader.manual_exit_positions(
            None,
            prices,
            float(paper_state.get("initial_cash", initial_cash)),
        )
        if exited:
            for event in exited:
                label = event.get("contract") or event["symbol"]
                if event.get("type") == "EXIT_FAILED":
                    st.error(f"Zerodha exit blocked for {label}: {event.get('error')}")
                elif event.get("type") == "EXIT_WORKING":
                    st.info(f"{label}: exit order is Open at Zerodha (not filled yet).")
                else:
                    st.success(
                        f"Exited {label} @ ₹{event['exit_price']:,.2f} · "
                        f"P&L ₹{event['pnl']:+,.2f}"
                    )
            st.rerun()
        else:
            st.warning("Nothing to exit — reload the page if a position just closed.")
    per_row = st.columns(min(3, len(paper_state["open_positions"])))
    for idx, (symbol, position) in enumerate(paper_state["open_positions"].items()):
        label = position.get("contract") or symbol
        with per_row[idx % len(per_row)]:
            if st.button(f"Exit {label}", key=f"manual_exit_{symbol}"):
                exited = paper_trader.manual_exit_positions(
                    [symbol],
                    prices,
                    float(paper_state.get("initial_cash", initial_cash)),
                )
                if exited:
                    event = exited[0]
                    if event.get("type") == "EXIT_FAILED":
                        st.error(f"Zerodha exit blocked: {event.get('error')}")
                    elif event.get("type") == "EXIT_WORKING":
                        st.info(f"{label}: exit order is Open at Zerodha (not filled yet).")
                    else:
                        st.success(
                            f"Exited {label} @ ₹{event['exit_price']:,.2f} · "
                            f"P&L ₹{event['pnl']:+,.2f}"
                        )
                    st.rerun()
                else:
                    st.warning("Could not exit — the position may already be closed.")
else:
    st.caption("No paper option positions are open.")

ranked = sorted(
    (
        (sig.confidence, sig.score, sym, sig.what_to_do)
        for sym, sig in tradable_signals.items()
        if sym not in paper_state["open_positions"]
    ),
    reverse=True,
)[:5]
if ranked:
    st.caption(
        f"Underlying scan only (not the fill list). Needs {adaptive_plan.confidence_required}% "
        "and a mapped ATM CE/PE before it appears above:"
    )
    st.dataframe(
        pd.DataFrame(
            [
                {"Share": sym, "Conf %": conf, "Score": score, "Status": instruction}
                for conf, score, sym, instruction in ranked
            ]
        ),
        width="stretch",
        hide_index=True,
    )
if adaptive_plan.selected:
    st.success(
        "AI-selected for the next eligible fill: "
        + ", ".join(
            f"{symbol} {adaptive_plan.candidate_sides[symbol]} "
            f"(up to ₹{adaptive_plan.candidate_budgets[symbol]:,.0f})"
            for symbol in adaptive_plan.candidate_budgets
        )
    )
with st.expander("Why the adaptive engine chose these limits"):
    for reason in adaptive_plan.explanation:
        st.markdown(f"- {reason}")

with st.expander("Paper trade history and records"):
    if paper_state["trades"]:
        history = pd.DataFrame(paper_state["trades"])
        if "order_status" not in history.columns:
            history["order_status"] = "Closed"
        else:
            history["order_status"] = history["order_status"].fillna("Closed")
        columns = [
            "order_status", "symbol", "contract", "quantity", "entry_price", "exit_price",
            "amount_invested", "proceeds", "pnl", "pnl_pct", "entry_time", "exit_time", "exit_reason",
        ]
        show_hist = history[[c for c in columns if c in history.columns]].rename(
            columns={
                "order_status": "Status",
                "symbol": "Underlying",
                "contract": "Contract",
                "pnl": "P&L ₹",
                "pnl_pct": "P&L %",
            }
        )
        st.dataframe(show_hist, width="stretch", hide_index=True)
    else:
        st.caption("No completed paper trades yet.")
    if paper_trader.STATE_PATH.exists():
        st.download_button(
            "Download complete JSON record",
            data=paper_trader.STATE_PATH.read_bytes(),
            file_name="paper_trades.json",
            mime="application/json",
        )

# ----------------------------------------------------------- action now --
urgent = [
    s for s in tradable_signals.values()
    if s.what_to_do.startswith("ENTER") or s.what_to_do == "EXIT NOW"
]
if urgent:
    st.subheader("Act now")
    cols = st.columns(min(3, len(urgent)))
    for i, s in enumerate(urgent):
        with cols[i % len(cols)]:
            message = f"**{s.symbol}** — {s.what_to_do}"
            if s.what_to_do == "EXIT NOW":
                st.error(message)
            else:
                st.success(message)
            for line in s.playbook[:3]:
                st.caption(line)

# ------------------------------------------------------------------ table --
st.subheader("Watchlist — enter, exit, tomorrow")

if rows:
    df_table = pd.DataFrame(rows)

    def color_now(val):
        text = str(val)
        if "ENTER" in text or "HOLD LONG" in text:
            return "background-color: #103b1f; color: #7CFC9A"
        if "EXIT" in text or "STAY OUT" in text or "HOLD SHORT" in text:
            return "background-color: #3b1010; color: #FF8A80"
        return "background-color: #333; color: #ddd"

    def color_bias(val):
        text = str(val)
        if "BULLISH" in text:
            return "background-color: #103b1f; color: #7CFC9A"
        if "BEARISH" in text:
            return "background-color: #3b1010; color: #FF8A80"
        return "background-color: #333; color: #ddd"

    def color_signal(val):
        if "BUY" in str(val):
            return "background-color: #103b1f; color: #7CFC9A"
        if "SELL" in str(val):
            return "background-color: #3b1010; color: #FF8A80"
        return "background-color: #333; color: #ddd"

    styled = df_table.style
    if "Do this" in df_table.columns:
        styled = styled.map(color_now, subset=["Do this"])
    if "Do now" in df_table.columns:
        styled = styled.map(color_now, subset=["Do now"])
    if "Signal" in df_table.columns:
        styled = styled.map(color_signal, subset=["Signal"])
    if "Tomorrow" in df_table.columns:
        styled = styled.map(color_bias, subset=["Tomorrow"])
    st.dataframe(styled, width="stretch", hide_index=True)
else:
    st.info("No data loaded yet.")

if errors:
    st.caption(f"Could not load: {', '.join(errors)}")

# ------------------------------------------------------------------ detail --
st.subheader("Chart & playbook")

if chart_data:
    selected = st.selectbox("Symbol", list(chart_data.keys()))
    df = chart_data[selected]
    sig = signals[selected]

    st.markdown(f"### Right now: {sig.what_to_do}")
    st.caption(f"Confidence {sig.confidence}% · score {sig.score} · {sig.data_source}")
    for line in sig.playbook:
        st.markdown(f"- {line}")

    col_a, col_b, col_c, col_d = st.columns(4)
    col_a.metric("Signal", sig.action, f"score {sig.score}")
    col_b.metric("Entry", sig.entry if sig.entry else "—")
    col_c.metric("Get out (stop)", sig.stop_loss if sig.stop_loss else "—")
    col_d.metric("Take profit", sig.target if sig.target else "—")

    st.markdown(f"### Tomorrow ({sig.tomorrow_session or next_session_label()}): {sig.tomorrow_bias}")
    if sig.tomorrow_plan:
        st.info(sig.tomorrow_plan)
    fc1, fc2, fc3, fc4 = st.columns(4)
    fc1.metric("Buy zone", sig.tomorrow_entry if sig.tomorrow_entry else "—")
    fc2.metric("Tomorrow stop", sig.tomorrow_stop if sig.tomorrow_stop else "—")
    fc3.metric("Tomorrow target", sig.tomorrow_target if sig.tomorrow_target else "—")
    fc4.metric("Invalid below/above", sig.tomorrow_invalid if sig.tomorrow_invalid else "—")
    if sig.tomorrow_reasons:
        with st.expander("Why this tomorrow bias"):
            for r in sig.tomorrow_reasons:
                st.markdown(f"- {r}")

    fig = make_subplots(
        rows=3,
        cols=1,
        shared_xaxes=True,
        row_heights=[0.55, 0.2, 0.25],
        vertical_spacing=0.03,
        subplot_titles=(f"{selected} — price / VWAP / Supertrend", "RSI + Stoch", "MACD"),
    )

    fig.add_trace(go.Candlestick(x=df.index, open=df["open"], high=df["high"], low=df["low"], close=df["close"], name="Price"), row=1, col=1)
    fig.add_trace(go.Scatter(x=df.index, y=df["vwap"], name="VWAP", line=dict(color="orange")), row=1, col=1)
    fig.add_trace(go.Scatter(x=df.index, y=df["ema9"], name="EMA9", line=dict(color="cyan", width=1)), row=1, col=1)
    fig.add_trace(go.Scatter(x=df.index, y=df["ema21"], name="EMA21", line=dict(color="magenta", width=1)), row=1, col=1)
    if "supertrend" in df.columns:
        fig.add_trace(go.Scatter(x=df.index, y=df["supertrend"], name="Supertrend", line=dict(color="white", width=1, dash="dot")), row=1, col=1)

    if sig.entry:
        fig.add_hline(y=sig.entry, line_dash="dash", line_color="cyan", annotation_text="entry", row=1, col=1)
    if sig.stop_loss:
        fig.add_hline(y=sig.stop_loss, line_dash="dash", line_color="red", annotation_text="stop", row=1, col=1)
    if sig.target:
        fig.add_hline(y=sig.target, line_dash="dash", line_color="lime", annotation_text="target", row=1, col=1)

    fig.add_trace(go.Scatter(x=df.index, y=df["rsi"], name="RSI", line=dict(color="yellow")), row=2, col=1)
    if "stoch_k" in df.columns:
        fig.add_trace(go.Scatter(x=df.index, y=df["stoch_k"], name="Stoch %K", line=dict(color="aqua", width=1)), row=2, col=1)
    fig.add_hline(y=70, line_dash="dot", line_color="red", row=2, col=1)
    fig.add_hline(y=30, line_dash="dot", line_color="green", row=2, col=1)

    fig.add_trace(go.Scatter(x=df.index, y=df["macd"], name="MACD", line=dict(color="cyan")), row=3, col=1)
    fig.add_trace(go.Scatter(x=df.index, y=df["macd_signal"], name="Signal", line=dict(color="orange")), row=3, col=1)
    fig.add_trace(go.Bar(x=df.index, y=df["macd_hist"], name="Hist", marker_color="gray"), row=3, col=1)

    fig.update_layout(height=750, xaxis_rangeslider_visible=False, template="plotly_dark", legend=dict(orientation="h"))
    st.plotly_chart(fig, width="stretch")

    st.markdown("**Why (all factors):**")
    for r in sig.reasons:
        st.markdown(f"- {r}")
else:
    st.info("Add valid NSE symbols in the sidebar to see charts.")

st.caption("Refreshes automatically — see sidebar to adjust.")
