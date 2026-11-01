# JGAPicksBot audit (Phase 1)

Scope: the merged code base (the deployed `claude/kalshi-ai-saas-ivlmc2` branch plus the research branch).
Severity: **C** critical, **H** high, **M** medium, **L** low. "Fixed" means fixed in this upgrade, with tests.

## 1. What already works (kept)

- **Two engines.**
  - `STRATEGY_MODE=btc` (default): the BTC 15-minute engine. It is a probability model `Phi(ln(S/K)/(sigma*sqrt(T_eff)))` with an uncertainty band, reads verified contract terms, nets out fees, and has explicit NO TRADE reasons.
  - `quality` / `confirm`: the legacy multi-asset signal engine, with grades, regimes and an empirical historical EV.
- **Safety.**
  - PAPER is the default. LIVE needs both `LIVE_TRADING=true` and `PAPER_TRADING=false`. Live auto-trading also needs `ALLOW_LIVE_AUTOTRADE=true`, and the BTC engine never auto-trades live.
  - Telegram admin-only guard, duplicate-callback guard, one-shot trade tickets (60 s).
  - Per-market execution lock, a risk re-check right before submit, IOC orders, reduce-only exits.
  - The order row is saved before sending. POST is never retried.
  - Hard limits on risk profiles, loss cooldowns, a losing-streak pause, and a persisted emergency stop.
- **Research tooling.** Historical replay of settled markets, walk-forward, filter ablation, bootstrap, the BTC backtest, fair value, VWAP, favourite and tennis tests.
- **Telegram.** Dashboard, BTC views with model probability, EV and NO TRADE reasons, P&L images, `/setkey`.

## 2. Bugs and weaknesses found

| # | Sev | Area | Finding | Status |
|---|-----|------|---------|--------|
| 1 | C | live exits | Every exit retry reused client_order_id `<coid>-x`, which is a UNIQUE column, so after one partial or unfilled exit the position could never be sold | **Fixed**: new id per attempt |
| 2 | C | live | No reconciliation against Kalshi `/portfolio/positions` | **Fixed**: `reconcile_live`, at startup and every 5 min; mismatches block the market and alert |
| 3 | H | live | Fill assumed from the POST reply; never read back from the exchange | **Fixed**: fills from `GET /portfolio/fills?order_id=`, then the order object, then the POST reply |
| 4 | H | live | Unknown-outcome lookup read one page and concluded "not placed" after one miss | **Fixed**: all pages, `min_ts`, needs 2 clean misses, otherwise the market is blocked |
| 5 | H | live | Partial exits lost their P&L and fees, so the daily loss was undercounted | **Fixed**: `Portfolio.close_partial` books a closed sub-position, trade row and daily P&L |
| 6 | H | live | No crash recovery for orders left `submitting`/`unknown`/`accepted` | **Fixed**: `recover_orders` at startup; an untracked fill blocks the market |
| 7 | H | research | `HistoricalModel` pooled BTC/ETH/SOL; one asset's history could approve another's trades | **Fixed**: per-asset sub-models; a pooled estimate is labelled and never sufficient |
| 8 | H | runtime | Any non-Kalshi exception silently killed the poll, discovery or maintenance loop (scanner stops) | **Fixed**: logged with traceback, loop continues |
| 9 | M | live | A non-JSON 2xx order reply was treated as a rejection (it may have been placed) | **Fixed**: treated as unknown, then reconciled |
| 10 | M | live | Live order states incomplete (no filled / partial / no_fill) | **Fixed** |
| 11 | M | live | Settlement could close a position twice, racing a manual or early exit | **Fixed**: per-market lock, re-check, `close()` refuses closed positions |
| 12 | M | risk | Remaining-daily-loss stake cap applied on the BTC path only | **Fixed**: every path |
| 13 | M | risk | Emergency stop also blocked confirmed reduce-only exits (you could not get out) | **Fixed**: e-stop blocks new exposure; confirmed exits allowed |
| 14 | M | data | `BTC_SPOT_SOURCE=index` never took effect live (Coinbase spot always passed in) | **Fixed** |
| 15 | M | discovery | `series_matches_duration`: "5M" matched "15M"; `detect_asset`: "BTC" matched "KXBTCD" | **Fixed** |
| 16 | M | research | Legacy EV uses an unrounded fee; Kalshi rounds the fee up per order (1-contract trades cost more) | Documented; the new model framework uses the exact per-order fee |
| 17 | L | runtime | Boot defaulted to LIVE when the flags allowed it | **Fixed**: always boots in PAPER |
| 18 | L | data | Discovery hardcodes the "Crypto" category; no non-crypto assets; `detect_asset` has no Gold | Phase 3 |
| 19 | L | ui | `CallbackGuard` extends the debounce on rejected taps; `/pnl` image rendered on the event loop | Open (cosmetic) |
| 20 | L | db | `accel_state` is String(14) but BTC rows store 17 characters (SQLite ignores this) | Open (SQLite only) |
| 21 | L | time | Daily loss limit uses the UTC day; dashboard uses Eastern | Open (documented) |

## 3. Missing data sources

- No order-book history from Kalshi, so backtests cannot replay book depth; the legacy order-book component is missing in replay.
- No non-crypto underlying providers before this upgrade (Gold: Phase 3).
- No RSI / MACD / VWAP / Bollinger / ATR features before this upgrade (Phase 4).
- ML libraries were not installed (scikit-learn added as an optional research dependency).

## 4. Research and backtesting limitations

- Replays use 1-minute candles (YES bid/ask closes), not live 5-second snapshots.
- Legacy observations are recorded only for VALID directional setups, so the sample is biased; rows are per minute with one outcome per market, so splits must keep each market together.
- The walk-forward has no embargo between train and test (about 15 minutes of overlap); the new framework adds one.
- The evidence so far: momentum, fair value, late entry, buy-the-favourite and VWAP/volume were all **no edge after fees** on real out-of-sample BTC history. The paper bot's recent +3.7-point edge over 195 trades is about 1.2 standard errors, which is not significant.
