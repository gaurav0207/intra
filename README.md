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

Paper trading is enabled from the sidebar. The starting paper account is
₹100,000. Exposure is adaptive rather than configured with fixed defaults:
the engine scans the broad liquid NSE universe and classifies the session as
`RISK-ON`, `MIXED`, or `RISK-OFF`. It then chooses:

- confidence threshold (60–80%);
- maximum positions (1–5);
- rupee amount per stock from stop distance, liquidity and 0.25–0.60% account
  risk per trade;
- eligible shares from signal quality and reward/risk.

The engine can hold LONG and SHORT paper positions at the same time. In
`RISK-ON` it favours longs; in `RISK-OFF` it favours shorts; in `MIXED` it can
take either side. Shorts are simulations of intraday short selling—the app
still never submits a real broker order.

The daily profit objective is ₹5,000, but it is not guaranteed. If combined
realized and open P&L reaches that amount, all positions are closed and no more
entries are made that day. A protective daily loss stop (normally 1.5% of
equity) does the same on the downside.

Entries run only between **9:30 and 15:00 IST** on weekdays, enforced against
the clock. Exits keep running until the 15:15 square-off, so a position can
never be stranded by the entry cut-off. New entries require Zerodha Kite data
by default. Turning that off emails `ALERT_EMAIL` once, then allows paper
fills against delayed Yahoo prices. The same address is emailed when Kite
login exists but market data cannot be fetched (expired token, missing
permissions, or empty candles). That fetch-failure mail is sent once per
reason, then at most every four hours until Kite recovers.

The engine selects the strongest fresh `ENTER LONG` signals in the trading
universe. The number of shares is `floor(maximum per trade / current price)`.
Open positions exit automatically when the stop or target is touched, the
signal reverses, or square-off is reached. Disabling automatic paper trading
stops new entries; protective exits continue.

### Running it unattended

Streamlit only executes while a browser session is connected, so the dashboard
cannot trade on its own. Use the headless runner on a machine that stays awake:

```bash
python3 auto_trader.py --loop     # all day, sleeps overnight
python3 auto_trader.py --once     # single pass, for cron
```

It shares `paper_trades.json` and the same email alerts as the dashboard.
The trader emails `ALERT_EMAIL` when Kite connects or disconnects, once per
session with today's invest plan (window, budget, closest names), when a
fresh LONG/SHORT setup appears, and instantly on each paper BUY/SHORT and EXIT.
The adaptive engine chooses universe, sizing, confidence and position count.
Operational flags remain for `--interval`, `--every` and initial `--cash`.
Kite data is required by default; add `--no-require-kite` to allow entries
from delayed Yahoo prices.

## Deploying on a server

Streamlit Community Cloud will not work for this: it runs the script only while
a browser session is connected, and its disk is wiped on every restart, so the
ledger does not survive. Use a small always-on host instead. Any of these are
enough, since the workload is one small Python process:

| Host | Cost |
|---|---|
| Oracle Cloud Always Free (ARM VM) | free |
| Google Cloud `e2-micro` free tier | free |
| Hetzner CX22 | ~€4/mo |
| DigitalOcean / Vultr / Lightsail | ~$5/mo |

### Docker (recommended)

Two services share one volume, so the daily Kite login done in the browser is
picked up by the trader automatically.

```bash
git clone https://github.com/gaurav0207/intra.git
cd intra
cp .env.example .env        # fill in SMTP + Kite values
docker compose up -d --build
docker compose logs -f trader
```

The dashboard is then on port 8501 and the trader runs continuously. Both
restart automatically if the machine reboots.

For a public HTTPS deployment with password protection, point a domain at the
server, set `DOMAIN`, `APP_URL`, `DASHBOARD_USER` and
`DASHBOARD_PASSWORD_HASH` in `.env`, then use:

```bash
docker compose -f compose.cloud.yml up -d --build
```

Generate the Caddy password hash with:

```bash
docker run --rm caddy:2 caddy hash-password --plaintext 'your-password'
```

Escape each `$` as `$$` when pasting the hash into `.env`. Set the Kite
developer redirect URL to exactly the same HTTPS `APP_URL`. At 09:30 IST the
trader emails the Kite login link and app link; after login, Kite redirects to
the dashboard and the request token is exchanged automatically.

### Without Docker (systemd)

```bash
sudo useradd -r -m -d /opt/intraday-dashboard intraday
sudo -u intraday git clone https://github.com/gaurav0207/intra.git /opt/intraday-dashboard
cd /opt/intraday-dashboard
sudo -u intraday python3 -m venv venv
sudo -u intraday venv/bin/pip install -r requirements.txt
sudo -u intraday mkdir -p state
sudo cp deploy/*.service /etc/systemd/system/
sudo systemctl enable --now intraday-dashboard intraday-trader
journalctl -u intraday-trader -f
```

### Daily Zerodha login

Kite access tokens expire every morning. At 09:30 IST the trader sends a login
email. Click its Kite link and complete login. If the Kite redirect URL matches
`APP_URL`, the dashboard captures and exchanges `request_token` automatically.
The token is written to the shared volume, so the trader picks it up on its
next cycle without restarting.

At 15:15 IST it sends a day-ended email confirming there are no further
entries, with completed trades, wins/losses, realized P&L and account equity.

Do not expose port 8501 to the open internet without protection — anyone who
reaches it can use your Kite session. Prefer an SSH tunnel:

```bash
ssh -L 8501:localhost:8501 user@your-server
```

Then use `http://localhost:8501` on your own machine. If you do want it public,
put it behind a reverse proxy with TLS and a password.

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
