# Intraday NSE Dashboard

A local web dashboard that watches a list of NSE stocks intraday and
gives you a rule-based **BUY / SELL / HOLD** signal for each one, with
a suggested **stop-loss** and **target**, plus the reasoning behind
every call.

## What it does

- Pulls intraday candles (1m / 5m / 15m) for your watchlist.
- Computes VWAP, EMA9/EMA21, RSI(14), MACD, ATR and volume-vs-average.
- Combines them into a transparent scoring system → BUY / SELL / HOLD.
- Suggests a stop-loss and target using ATR multiples, so it also
  tells you **when to get out**, not just when to get in.
- Auto-refreshes on a timer you control.
- Interactive chart per symbol (candlesticks + VWAP + EMAs, RSI, MACD)
  with the exact reasons behind the current signal.
- Automatically simulates paper trades from fresh entry signals, monitors
  stops/targets/reversals, and records every entry and exit.
- Tracks cash, invested value, realized/unrealized P&L and total equity in
  `paper_trades.json`.
- Optionally emails each simulated entry and exit. It never places a real
  Zerodha order.

## ⚠️ Important limitations — read before using

1. **Data is not true real-time.** This uses `yfinance` (free, Yahoo
   Finance), which is the only no-signup option. NSE data through
   Yahoo is generally delayed and can occasionally be missing or
   stale intraday, especially for less-liquid stocks. It is good
   enough to learn the workflow and backtest the logic, but treat any
   single reading with a grain of salt — always cross-check against
   your broker's live quote before acting.
2. **This is not financial advice.** The signal is a mechanical
   combination of common technical indicators. It has no knowledge of
   news, fundamentals, order-book depth, or your risk tolerance. It
   can and will be wrong. Paper-trade it for a while before risking
   real money, and never risk more than you can afford to lose.
3. **Indicators lag.** All of VWAP, EMA, RSI, MACD are calculated from
   past prices — by definition they confirm a move after it starts,
   they don't predict it. Fast-moving news-driven spikes will not be
   caught early.

## Getting real-time data later (optional upgrade)

When you're ready for true real-time (tick-level) data, swap out
`data.py`'s `fetch_intraday()` for a broker API — the rest of the app
(indicators, signals, UI) doesn't need to change:

- **Zerodha Kite Connect** — most popular, paid API access (~₹2000/mo
  historically), WebSocket ticks.
- **Upstox API** — free for Upstox account holders, REST + WebSocket.
- **Angel One SmartAPI** — free for Angel One account holders.
- **Dhan API** — free for Dhan account holders, has a generous free tier.

All four give you live LTP (last traded price) and depth via
WebSocket, which is what makes a signal genuinely "real-time" instead
of refreshed-every-60-seconds.

## Setup

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

Then open the local URL Streamlit prints (usually `http://localhost:8501`).

## Automatic paper trading

The watchlist is for viewing charts only. The paper trader buys from its own
universe, chosen in the sidebar and defined in `universe.py`:

- **Liquid 20 (default)** — the most heavily traded NSE large caps.
- **Nifty 50** — the full index.
- **High beta movers** — faster, noisier names.

Every symbol in the universe is scanned in parallel each refresh, so a 20-name
scan takes roughly 7 seconds. Stocks you hold are always scanned, even if you
switch universes, so their exits keep working.

Paper trading is enabled from the sidebar. Defaults are ₹100,000 starting
cash, at most ₹5,000 per trade, three open positions, and 50% minimum signal
confidence. New entries require connected Zerodha data by default; this avoids
simulating fills against delayed Yahoo prices.

The engine selects the strongest fresh `ENTER LONG` signal in the watchlist.
The number of shares is `floor(maximum per trade / current price)`. Open
positions exit automatically when the stop or target is touched, the signal
reverses, or the intraday square-off time is reached. Disabling automatic
paper trading stops new entries; protective exits continue.

The complete ledger is written atomically to `paper_trades.json`, which is
ignored by git and can also be downloaded from the app. Deleting that file
starts a new portfolio; back it up first if you need the history.

### Email alerts

Copy these settings from `.env.example` into `.env`:

```dotenv
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=you@gmail.com
SMTP_PASSWORD=your_app_password
SMTP_FROM=you@gmail.com
ALERT_EMAIL=you@gmail.com
```

For Gmail, enable two-step verification and create an App Password. Do not use
your normal Gmail password. Restart Streamlit after changing `.env`. Entry
emails include share, fill price, quantity and amount invested. Exit emails
include total proceeds, profit/loss in rupees and percent, and the exit reason.

## Using it

1. Edit the watchlist in the sidebar — comma-separated NSE symbols,
   no need to add `.NS` (e.g. `RELIANCE, TCS, INFY`).
2. Pick a candle interval — `5m` is a good default for intraday.
3. Set your auto-refresh rate.
4. Adjust the ATR multiples for stop-loss/target to match your risk
   appetite (tighter stop = smaller loss but more false stop-outs).
5. Watch the table for BUY/SELL signals; click into a symbol to see
   the chart and the exact reasons behind its current signal.

## Tuning the signal logic

All the rules live in `signals.py` in one function, `_score_row()` —
each rule adds or subtracts from a score, in plain English comments.
Common tweaks:

- Change indicator weights (e.g. make MACD count for 2 points).
- Add new rules (Bollinger Band breakout is already computed in
  `indicators.py`, just unused — easy to wire in).
- Change the BUY/SELL score thresholds.
- Change `atr_stop_mult` / `atr_target_mult` defaults.

## Files

| File | Purpose |
|---|---|
| `app.py` | Streamlit UI, layout, charts |
| `data.py` | Fetches OHLCV candles (Kite, falling back to yfinance) |
| `indicators.py` | VWAP, EMA, RSI, MACD, ATR, Bollinger calculations |
| `signals.py` | Combines indicators into BUY/SELL/HOLD + stop/target |
| `universe.py` | The stock lists the app is allowed to trade |
| `scanner.py` | Scans the universe in parallel each refresh |
| `paper_trader.py` | Simulated portfolio, JSON ledger, email alerts |
| `kite_client.py` | Zerodha Kite login and candle helpers |
