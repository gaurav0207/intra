import os

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots
from streamlit_autorefresh import st_autorefresh

import adaptive_policy
import kite_client
import paper_trader
import scanner
from data import kite_health, market_status, next_session_label

st.set_page_config(page_title="Intraday NSE Dashboard", layout="wide")

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
        st.sidebar.warning(f"Kite token expired or invalid: {exc}")
        kite_client.clear_token()
        st.session_state.kite_token = None
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
long_only = True

st.sidebar.markdown("---")
st.sidebar.title("Paper trading")
paper_enabled = st.sidebar.toggle(
    "Automatic paper trading",
    value=True,
    help="Simulates trades only. This never sends a Zerodha order.",
)
initial_cash = st.sidebar.number_input(
    "Starting paper cash (₹)",
    min_value=5_000,
    max_value=10_000_000,
    value=100_000,
    step=5_000,
    disabled=paper_trader.STATE_PATH.exists(),
)
trade_universe = adaptive_policy.AI_UNIVERSE
st.sidebar.info(
    "Adaptive mode chooses the universe, confidence, number of positions, "
    "and rupee amount from market breadth, volatility, liquidity, and risk."
)
require_kite_for_entry = True
headless_executor = os.getenv("PAPER_EXECUTOR", "dashboard") == "headless"
st.sidebar.caption("New entries require Zerodha data.")
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
paper_state = paper_trader.load_state(float(initial_cash))
st.sidebar.caption(
    f"Paper cash ₹{paper_state['cash']:,.2f} · "
    f"{len(paper_state['open_positions'])} open · "
    f"{len(trade_universe)} AI-scanned · no real orders"
)
st.sidebar.caption(
    f"Entries {paper_trader.ENTRY_START:%H:%M}–{paper_trader.ENTRY_END:%H:%M} IST · "
    f"square-off {paper_trader.SQUARE_OFF:%H:%M} IST"
)

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

st.title("📈 Intraday NSE Dashboard")
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
adaptive_plan = adaptive_policy.decide(paper_state, tradable_signals, chart_data, prices)
if headless_executor:
    paper_events = []
    paper_state = paper_trader.load_state(float(initial_cash))
else:
    paper_events = paper_trader.run_cycle(
        paper_state,
        tradable_signals,
        chart_data,
        enabled=paper_enabled,
        market_open=status == "open",
        budget_per_trade=0,
        max_positions=adaptive_plan.max_positions,
        minimum_confidence=adaptive_plan.confidence_required,
        require_kite=require_kite_for_entry,
        candidate_budgets=adaptive_plan.candidate_budgets,
        daily_profit_target=adaptive_plan.daily_profit_target,
        daily_loss_limit=adaptive_plan.daily_loss_limit,
    )
paper_summary = paper_trader.portfolio_summary(paper_state, prices)

st.subheader("Paper portfolio — simulated money only")
st.info(
    f"Adaptive plan: **{adaptive_plan.regime}** · "
    f"confidence ≥ {adaptive_plan.confidence_required}% · "
    f"up to {adaptive_plan.max_positions} positions · "
    f"risk ₹{adaptive_plan.risk_per_trade:,.0f}/trade · "
    f"daily objective ₹{adaptive_plan.daily_profit_target:,.0f} "
    f"(not guaranteed) · loss stop ₹{adaptive_plan.daily_loss_limit:,.0f}"
)
_now_ist = pd.Timestamp.now(tz="Asia/Kolkata").time()
if status != "open":
    st.caption(f"Market {status.replace('_', ' ')} — entries resume at {paper_trader.ENTRY_START:%H:%M} IST.")
elif _now_ist < paper_trader.ENTRY_START:
    st.caption(f"Waiting for the entry window to open at {paper_trader.ENTRY_START:%H:%M} IST.")
elif _now_ist >= paper_trader.ENTRY_END:
    st.caption(f"Entry window closed at {paper_trader.ENTRY_END:%H:%M} IST — exits only until square-off.")
else:
    st.caption(f"Entry window open until {paper_trader.ENTRY_END:%H:%M} IST.")
p1, p2, p3, p4, p5 = st.columns(5)
p1.metric("Total equity", f"₹{paper_summary['equity']:,.2f}", f"₹{paper_summary['total_pnl']:+,.2f}")
p2.metric("Cash", f"₹{paper_summary['cash']:,.2f}")
p3.metric("Invested value", f"₹{paper_summary['market_value']:,.2f}")
p4.metric("Realized P&L", f"₹{paper_summary['realized_pnl']:+,.2f}")
p5.metric("Open P&L", f"₹{paper_summary['unrealized_pnl']:+,.2f}")

if paper_events:
    for event in paper_events:
        if event["type"] == "BUY":
            st.success(
                f"Paper BUY: {event['symbol']} · {event['quantity']} shares @ "
                f"₹{event['entry_price']:,.2f} · invested ₹{event['amount_invested']:,.2f}"
            )
        else:
            st.info(
                f"Paper EXIT: {event['symbol']} · received ₹{event['proceeds']:,.2f} · "
                f"P&L ₹{event['pnl']:+,.2f} ({event['pnl_pct']:+.2f}%)"
            )

if paper_state["open_positions"]:
    position_rows = []
    for symbol, position in paper_state["open_positions"].items():
        current = float(prices.get(symbol, position["entry_price"]))
        pnl = current * position["quantity"] - position["amount_invested"]
        position_rows.append(
            {
                "Share": symbol,
                "Shares": position["quantity"],
                "Entry": position["entry_price"],
                "Current": round(current, 2),
                "Invested": position["amount_invested"],
                "Stop": position["stop"],
                "Target": position["target"],
                "Open P&L": round(pnl, 2),
                "Entered": position["entry_time"],
            }
        )
    st.dataframe(pd.DataFrame(position_rows), width="stretch", hide_index=True)
else:
    st.caption("No paper positions are open. The adaptive engine is scanning its broad liquid NSE universe.")

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
        f"Closest candidates (currently needs {adaptive_plan.confidence_required}% "
        "and a fresh ENTER LONG):"
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
            f"{symbol} (up to ₹{adaptive_plan.candidate_budgets[symbol]:,.0f})"
            for symbol in adaptive_plan.selected
        )
    )
with st.expander("Why the adaptive engine chose these limits"):
    for reason in adaptive_plan.explanation:
        st.markdown(f"- {reason}")

with st.expander("Paper trade history and records"):
    if paper_state["trades"]:
        history = pd.DataFrame(paper_state["trades"])
        columns = [
            "symbol", "quantity", "entry_price", "exit_price", "amount_invested",
            "proceeds", "pnl", "pnl_pct", "entry_time", "exit_time", "exit_reason",
        ]
        st.dataframe(history[[c for c in columns if c in history.columns]], width="stretch", hide_index=True)
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

    st.dataframe(
        df_table.style.map(color_now, subset=["Do now"])
        .map(color_signal, subset=["Signal"])
        .map(color_bias, subset=["Tomorrow"]),
        width="stretch",
        hide_index=True,
    )
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
