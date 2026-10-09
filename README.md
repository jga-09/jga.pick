# 📡 JGAPicksBot — Kalshi 15-Minute Directional Trading Bot

A Telegram-controlled bot that discovers Kalshi short-duration (15-minute) markets, scores them
**🟢 UP / 🔴 DOWN / ⚪ WAIT** with a 0–100 confidence, checks every trade against a
**🟢 LOW / 🟡 MEDIUM / 🔴 HIGH** risk profile, and executes in **PAPER mode by default**.
Live trading exists behind several independent safety gates.

> ⚠️ **Confidence is a signal-strength score, not a probability of winning.** It has not been
> statistically calibrated. Nothing here is a claim of profitability. Trading event contracts risks
> the loss of your entire stake.

---

## 1. What it does

```
Market discovery → Market data (REST poll + WebSocket) → Feature engine → Signal engine
      → Risk manager → Position sizer → Trade executor (paper | live) → Kalshi
```

* Finds active 15-minute markets for configured assets (BTC/ETH/SOL by default). There is no
  hardcoded market ticker.
* Streams and polls quotes, the order book and recent trades. It builds a normalized snapshot and
  never fabricates missing data.
* Generates explainable signals (`+ Strong positive momentum`, `- Order book mixed`, …).
* Enforces risk limits per profile: confidence, spread, liquidity, time to expiry, exposure, open
  positions, daily loss, cooldowns, stale data, duplicate orders, and trading-disabled state.
* Paper-trades with slippage, fee estimates, mark-to-market and settlement, and stores everything
  in SQLite.
* A compact Telegram dashboard that edits a single message in place, laid out as a two-column
  button grid.

## 2. Features

| Area | Highlights |
|---|---|
| Signal quality | 10-component Signal Quality score, setup grades, 19 no-trade filters, regimes, EV gate, adaptive mode |
| Research | Observation dataset, calibration, walk-forward backtests, filter ablation, feature importance, win/loss analysis |
| Telegram UI | Home dashboard, signal cards, markets list, status, trades, positions, history, strategy, config, risk menu with confirmation, emergency stop |
| Signals | Momentum, ROC, EMA trend, regression slope, acceleration, volatility, order-book imbalance, taker trade flow, volume acceleration, probability level, optional spot-vs-strike |
| Risk | 3 real profiles, env-configurable, Telegram customisation bounded by hard limits |
| Execution | Paper (default) and live (Kalshi V2 orders, IOC limit, `client_order_id`) |
| Safety | Admin allow-list, callback validation, one-shot tickets, per-market locks, persistent emergency stop, secret redaction |
| Data | SQLite (WAL), snapshot/signal history for backtesting, configurable retention |
| Alerts | Strong signal, flip, trade opened/closed, large loss, daily limit, Kalshi disconnect/reconnect, auth failure, with cooldowns |

## 3. Project structure

```
app/
  main.py            entry point (python -m app.main)
  config.py          pydantic-settings configuration (.env)
  errors.py          exception hierarchy
  state.py           persisted runtime state (running/paused/e-stop/risk/mode/auto)
  runtime.py         orchestrator wiring every component together
  kalshi/            auth (RSA-PSS signing), REST client, discovery, market data, websocket, order builders, fixtures
  strategy/          indicators, features, scoring, signal types, engine
  risk/              profiles, position sizing, RiskManager
  trading/           paper, live, executor, portfolio/P&L, positions, tickets, fees
  telegram/          bot wiring, handlers, callback router, dashboard/alerts, keyboards, message renderers
  database/          SQLAlchemy models, engine, repository
  data/underlying.py optional spot-price providers (none | coinbase)
  utils/             logging (redaction), retry/backoff/rate limit, time helpers
tests/               pytest suite (68 tests)
scripts/smoke_test.py  end-to-end demo with fixture data
```

## 4. Requirements

* Python **3.12+**
* A Telegram bot token, plus your numeric Telegram user id
* Optional for paper mode: Kalshi API key. Public market data needs no key, but WebSocket
  streaming and live trading do.

## 5–6. Installation and virtual environment

```bash
git clone <this repo> && cd <repo>
python3.12 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt  # runtime + pytest
cp .env.example .env
```

## 7–8. Create the Telegram bot and get the token

1. In Telegram, open **@BotFather** → `/newbot` → choose a name and username.
2. BotFather replies with a token like `1234567890:AA…`. Put it in `.env` as `TELEGRAM_BOT_TOKEN`.
3. Optionally run `/setprivacy` → *Enable* (the bot only needs commands and buttons).

## 9. Find your Telegram user id

Message **@userinfobot** (or **@RawDataBot**). It replies with your numeric `Id`. Put it in
`TELEGRAM_ADMIN_IDS` (separate multiple admins with commas). **Only these ids can use the bot.**
Everyone else gets `⛔ Unauthorized`. Start a chat with your bot (`/start`) so it can message you.

## 10. Kalshi credentials

1. On kalshi.com go to **Account → API Keys → Create key**. Save the **Key ID** and download the
   **private key** (`.pem`). Kalshi only shows it once.
2. Store the key outside the repo, e.g. `~/.kalshi/kalshi.pem` (`chmod 600`).
3. Set `KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH` in `.env`.
4. Use `KALSHI_ENV=demo` to work against the demo exchange (needs a separate demo key).

How authentication works (matches the official Kalshi SDK): each request carries
`KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP` (ms) and `KALSHI-ACCESS-SIGNATURE`. The signature
is a base64 RSA-PSS/SHA-256 signature (Ed25519 keys are also supported) over
`timestamp + METHOD + path`, where the path excludes the query string.

## 11. `.env` configuration

Everything is documented inline in [`.env.example`](.env.example). Key settings:

| Variable | Default | Meaning |
|---|---|---|
| `PAPER_TRADING` / `LIVE_TRADING` | `true` / `false` | Live requires **both** `LIVE_TRADING=true` and `PAPER_TRADING=false` |
| `AUTO_TRADING` | `false` | Auto-execute risk-approved signals (paper) |
| `ALLOW_LIVE_AUTOTRADE` | `false` | Extra gate required for live auto-trading |
| `DATA_SOURCE` | `kalshi` | `fixture` = synthetic data for dry runs (labelled 🧪, can never go live) |
| `ASSETS` / `SERIES_TICKERS` | `BTC,ETH,SOL` / empty | Discovery universe |
| `DEFAULT_RISK_LEVEL` | `low` | Starting risk profile |
| `LOW_* / MEDIUM_* / HIGH_*` | see table below | Risk-profile overrides |
| `UNDERLYING_PROVIDER` | `none` | `coinbase` enables spot-vs-strike features |

## 12. Start in paper mode

```bash
source .venv/bin/activate
python -m app.main --check-config   # validates config and exits
python -m app.main                  # starts the Telegram bot (PAPER)
```

In Telegram, send `/start` (or `/menu`) to open the dashboard, then tap **🟢 Start** to start the
scanner and signal engine.

No Kalshi access yet? Run the full bot on synthetic data:

```bash
python -m app.main --fixture        # or DATA_SOURCE=fixture in .env
```

## 13. Docker

```bash
cp .env.example .env   # fill in TELEGRAM_BOT_TOKEN and TELEGRAM_ADMIN_IDS
docker compose up -d --build
docker compose logs -f bot
```

The database lives in the named volume `bot-data`. Secrets are read from `.env` at runtime and are
never copied into the image (`.dockerignore` excludes `.env` and `*.pem`). To use a Kalshi key,
uncomment the read-only key mount and `KALSHI_PRIVATE_KEY_PATH` in `docker-compose.yml`.

## 13b. Deploy to a cloud server (runs 24/7 without your computer)

Any small Ubuntu 22.04/24.04 server works (1 GB RAM, ~25 GB disk, **US region** - Kalshi is US-only).
From the machine that currently runs the bot:

```bash
# 1. copy code + .env + bot.db (not the venv or replay cache), and the Kalshi key
tar czf - --exclude=.venv --exclude=data/replay_cache --exclude='data/replay.db*' -C ~ jga.pick \
  | ssh root@SERVER_IP 'mkdir -p /home/bot && tar xzf - -C /home/bot'
scp ~/.kalshi/kalshi.pem root@SERVER_IP:/root/kalshi.pem     # skip if you have no key
# 2. stop the local copy (only ONE bot may poll Telegram at a time)
tmux kill-session -t bot; pkill -f app.main
# 3. install + start as a service
ssh root@SERVER_IP 'bash /home/bot/jga.pick/scripts/server_setup.sh'
```

`server_setup.sh` installs Python 3.12 and dependencies, runs the bot as the `jgapicks` systemd service
(starts on boot, restarts after crashes), enables `AUTO_START` (paper mode only), adds swap and a firewall
that only allows SSH. Logs: `journalctl -u jgapicks -f`. Update: `cd /home/bot/jga.pick && sudo -u bot git pull
&& systemctl restart jgapicks`.

## 14. Testing

```bash
python -m pytest -q             # 68 tests, no network, no credentials
python scripts/smoke_test.py    # end-to-end demo: dashboard, UP/DOWN/WAIT, risk, paper trade, P&L
```

The tests cover all safety requirements: LOW threshold rejection, MEDIUM limits, HIGH exposure
cap, daily loss limit, stale data, expiry proximity, paper mode never calling live execution,
live disabled without both flags, unauthorized users, duplicate orders, sizing caps, UP/DOWN/WAIT
signals, risk change confirmation and the emergency stop. They also cover signing, parsing,
discovery, client error mapping, callback validation and the dashboard/alert transport.

## 15. Risk profiles

| | 🟢 LOW | 🟡 MEDIUM | 🔴 HIGH |
|---|---|---|---|
| Min confidence | 80 | 70 | 60 |
| Max position | $5 | $15 | $30 |
| Max open positions | 1 | 2 | 3 |
| Max daily loss | $10 | $30 | $60 |
| Min time remaining | 180 s | 120 s | 60 s |
| Max spread | 5¢ | 8¢ | 12¢ |
| Max total exposure | $5 | $30 | $90 |
| Min book liquidity | 20 | 10 | 5 contracts |
| Loss / trade cooldown | 15 / 5 min | 10 / 2 min | 5 / 1 min |
| Entry price band | 10–85¢ | 8–90¢ | 5–95¢ |
| Max balance fraction | 2% | 5% | 10% |

Position size scales from 50% to 100% of the max as confidence rises above the threshold. It is
also capped by remaining exposure, the balance fraction, available balance and the liquidity at
the best ask, and it is priced at the worst-case limit (ask + `ORDER_PRICE_TOLERANCE_CENTS`).
**HIGH never disables safeguards.** Hard limits in `app/risk/profiles.py` bound every profile and
every Telegram customisation. **Risk level and paper/live mode are independent:** choosing HIGH
never enables live trading.

## 15b. Signal quality, no-trade filter and research system

Every evaluation now produces a **SetupAnalysis** on top of the direction/confidence:

| Piece | What it does |
|---|---|
| **10 components** | A momentum · B trend · C order book · D volume/aggressive flow · E volatility · F probability movement · G time · H underlying · I acceleration · J liquidity/spread (+ signal stability). Moves are measured in *normalised* (probit) units: a 15-min binary naturally swings more as expiry nears, so raw cents are not comparable across the window. |
| **Signal Quality 0-100** | Points per component (configurable `SIGNAL_WEIGHTS`). Agreement earns points, disagreement subtracts. Separate from confidence. |
| **Setup grade** | A+ (≥90, no warnings) · A (≥80) · B (≥70) · C (≥60) · NO TRADE. LOW trades A+/A, MEDIUM/HIGH A+/A/B; C only if `HIGH_ALLOWED_GRADES` includes it. |
| **No-trade filter** | 19 named filters (conflict, weak momentum, low volume, abnormal spread, thin liquidity, sideways, extreme volatility, too little time, flip-flopping, coin-flip near strike, underlying conflict/divergence, weak book, extended move, adverse-selection spike, deceleration, …). Hard filters always block; soft ones count against the profile's `MAX_SOFT_FLAGS`. Disable one with `DISABLED_FILTERS` only when research shows it hurts. |
| **Regime** | STRONG/MODERATE TREND, SIDEWAYS, HIGH VOLATILITY, CHAOTIC, LOW LIQUIDITY. |
| **Historical model** | Every graded setup is stored once per market-minute in `observations` and labelled with the real result when the market settles. Statistics count *distinct markets*, show "⚠️ INSUFFICIENT DATA" below `HIST_MIN_SAMPLES`, and edge estimates are shrunk towards zero (`CALIBRATION_PRIOR_STRENGTH`). |
| **EV gate** | Estimated EV per contract after fees + slippage must exceed the profile's `MIN_EV_CENTS` once data exists. By default the gate uses the conservative 80% lower bound of EV (`EV_GATE=lower`). LOW, MEDIUM and any live trade require a supported EV estimate; HIGH paper trades are allowed while data is collected and are marked INSUFFICIENT DATA. The research dataset is built from observations of *every* setup, so no trades are needed to learn. |
| **Historically-poor conditions** | A regime / time window / price range / grade is blocked only when the *upper* bound of its win-rate interval is still below break-even. |
| **Adaptive mode** | CAUTION (stricter) or PAUSED when volatility is beyond the 95th/99th percentile of history, data is stale, or the last 20 trades are far below the model's expectation. Never reacts to a handful of trades. |
| **Win / loss analysis** | Each closed trade is tagged (momentum reversal, order-book flip, late entry, …) by comparing entry vs. last-seen conditions. |

Telegram: signal cards show quality, grade, regime, momentum state, underlying confirmation, similar-setup
statistics, edge and EV; **🧠 Why?** and **📋 Checklist** explain every decision; **🧠 Strategy → 📈 Analytics**
has detailed stats, calibration, loss/win analysis, the V0-V6 strategy comparison, filter ablation and
feature performance.

### Research workflow

```bash
python scripts/research.py              # REAL data: calibration, regimes, time/price buckets,
                                        # feature importance, walk-forward V0-V6, filter ablation
python scripts/simulate.py              # SYNTHETIC sanity check of the machinery (not market evidence)
python scripts/replay.py --days 14      # REAL past Kalshi markets replayed through the engine (see below)
python scripts/test_strategy.py         # robustness check of one fixed rule (default V4) on live data
python scripts/diagnose.py              # plain-language "why is the bot not trading?"
```

**Historical replay** (`scripts/replay.py`) downloads settled 15-minute markets (1-minute candlesticks +
trades, falling back to `/historical/*` endpoints for older markets) and Coinbase 1-minute spot prices,
caches them in `data/replay_cache/`, replays every market minute by minute through the bot's own engine and
stores the observations in `data/replay.db` (never mixed with live statistics). Limits: Kalshi keeps no
order-book history, so V3/V4 cannot be replayed - **V4L** (V4 without the order book) can; quotes are
1-minute closes rather than 5-second snapshots; spot is Coinbase, a proxy for Kalshi's settlement index.

Walk-forward = rolling train → validate → out-of-sample test over chronological chunks of markets
(or `--unit days`). Only V6 learns anything, and only from data earlier than its test period.
Nothing is deployed automatically: change thresholds/filters in `.env` only when out-of-sample results
with ≥30 trades support it.

## 16. Enabling live trading (deliberately)

All of the following are required:

1. `LIVE_TRADING=true` **and** `PAPER_TRADING=false` in `.env`.
2. Valid `KALSHI_API_KEY_ID` + `KALSHI_PRIVATE_KEY_PATH`, with `DATA_SOURCE=kalshi`. The bot
   refuses to start if live is requested without credentials.
3. In Telegram: **⚙️ Config → 📝 Paper/Live → ⚠️ Live → ✅ Enable LIVE**. Admins only.
4. Every live trade shows **⚠️ LIVE TRADE CONFIRMATION** and needs **✅ CONFIRM LIVE TRADE**. The
   ticket is one-shot and expires after 60 s. Risk is re-checked against fresh data immediately
   before submission.
5. Live *auto*-trading additionally needs `ALLOW_LIVE_AUTOTRADE=true`.

Live orders use Kalshi's V2 endpoint `POST /portfolio/events/orders` as immediate-or-cancel limit
orders with a unique `client_order_id`. Order submissions are **never retried automatically**,
which avoids duplicates. Start with `KALSHI_ENV=demo`.

## 17. Security warnings

* Never commit `.env` or `.pem` files (both are git-ignored). Rotate any key that leaks.
* Logs redact Telegram tokens, PEM blocks, signatures and registered secrets. `httpx` request
  logging is silenced because it would print the bot token.
* Only `TELEGRAM_ADMIN_IDS` can view or control the bot. Callback data is validated against an
  allow-list.
* **🚨 E-Stop** immediately blocks all orders, turns auto trade off, persists across restarts, and
  needs an admin confirmation to reset.
* Restarting never resumes the trading loops automatically; tap **🟢 Start**.
* Test on the demo exchange and with small limits before risking real money.

## 18. Troubleshooting

| Symptom | Fix |
|---|---|
| `CONFIG_INVALID TELEGRAM_BOT_TOKEN is required` | Fill `.env`; check `python -m app.main --check-config` |
| `⛔ Unauthorized` | Your id isn't in `TELEGRAM_ADMIN_IDS` (must be the numeric id) |
| Dashboard shows `🔍 Searching for active 15M markets` | No matching open market right now, or series naming differs. Set `SERIES_TICKERS` explicitly and check `/debug` for the resolved series |
| `🔴 AUTH FAILED` | Wrong key id, wrong PEM, or wrong `KALSHI_ENV` (demo keys don't work on prod) |
| `Market Data: 🟢 POLLING` instead of STREAMING | WebSocket needs credentials; polling is the automatic fallback |
| `Stale market data` rejections | Network/rate-limit issues; lower `POLL_INTERVAL_SEC` pressure or raise `KALSHI_READ_RPS` within your tier |
| `database is locked` | Run only one bot instance per database file |
| Positions not settling | Settlement waits for Kalshi to publish the market `result`; it never guesses |
