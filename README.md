# 📡 DirectionalBot — Kalshi 15-Minute Directional Trading Bot

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
