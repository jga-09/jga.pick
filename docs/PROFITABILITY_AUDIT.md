# BTC 15-minute profitability audit

Run the full, reproducible audit on your machine (it needs your downloaded market history and bot database):

    sudo docker compose run --rm bot python scripts/btc_audit.py              # LOW profile filters
    sudo docker compose run --rm bot python scripts/btc_audit.py --risk high  # looser filters, more samples

It writes `data/audit_report.md` / `.json`. Method:

- Real cached Kalshi markets + real closed paper positions only (bot DB and its backups, read-only, de-duplicated).
- Each decision uses only data available at that minute; outcomes only score trades afterwards.
- Fixed, pre-registered rules (no parameter fitting). Chronological split: development days, then a final
  **holdout** (default 7 days) that is evaluated once.
- Baselines: B0 no trade, B1 simple 5-min momentum, B2 current BTC engine (calibration gate off so it can be
  measured), REF model edge (the core signal candidates are measured against).
- Candidates change ONE thing each (price cap 65c, ≥5 min left, 50/50 blend with market price, ≥5-pt
  disagreement with market, skip volatile regimes, early exit at model odds < 40%), each with a hypothesis.
- ACCEPT only if: ≥30 development trades, development EV 95% interval above 0, day-paired improvement over
  the baseline with 95% interval above 0, and not worse/losing on the untouched holdout. Otherwise the baseline
  is retained.
- Report: net EV/contract with bootstrap 95% intervals, P&L, trade count, max drawdown, fees, slippage,
  win rate vs break-even win rate, calibration (ECE, Brier vs market, favoured-side overconfidence, promised vs
  realised win rate on selected trades, by minutes left), segment tables (regime, entry price, minutes left,
  model probability, side, spread) with minimum-sample flags, cost sensitivity, and paper-record segments.

Machinery checks (synthetic data, not market evidence): on efficiently priced quotes the audit finds no edge,
the simple baseline loses about the costs, and nothing is accepted; on quotes with a planted lag it finds the
edge. Both are covered by `tests/test_audit.py`.

## Evidence available so far (results you ran and shared, Sep 12 – Oct 12, 782 markets)

| Evidence | Result |
|---|---|
| Calibration, 777 markets | ECE 0.012 (well calibrated) but Brier model 0.1583 vs Kalshi mid 0.1571 → market slightly more accurate → NOT_CALIBRATED |
| Walk-forward, best taker variant (momentum + model) | 78 trades, 85% win, EV −0.3¢/contract, 95% −8.4..+7.0¢ |
| Full BTC engine | 0 out-of-sample trades: no parameter was profitable on earlier days |
| Maker (post bid 1–3¢ below ask), maker fee 0.0175 and 0 | ≤7 fills per variant; never profitable on earlier days or tiny/negative samples |
| Paper, old "confirm" engine (160 trades) | 71% win at ~73¢ average entry → break-even ≈ 75%; net −$98.77 (−4% ROI); 70–79 confidence bucket −$123 |

## Decision

No change has been shown to produce sustainable positive net expectancy. **Baseline retained; no strategy
parameters were changed.** Live trading remains disabled. The best validated choice for real money is the
no-trade baseline until an audit run shows an ACCEPTED change that also holds up in paper trading.
