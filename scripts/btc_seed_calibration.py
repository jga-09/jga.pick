"""Fast-forward the BTC odds check using markets you already downloaded.

    python scripts/replay.py --days 30            # 1) download settled BTC markets (if not done yet)
    python scripts/btc_seed_calibration.py        # 2) score the bot's odds on them

The BTC probability model has no fitted parameters, so replaying settled markets minute by
minute (using only data available at that minute) gives genuine out-of-sample predictions.
Each is stored as a calibration observation with its REAL result, marked ``backfill`` so it
can always be told apart from live ones. Markets already recorded are skipped.

Differences from live: history uses 1-minute candle prices (not the live order book), so
treat a pass here as a strong hint and keep watching the live numbers.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.database.db import Database  # noqa: E402
from app.database.repository import Repository  # noqa: E402
from app.research.btc_backtest import Step, load_dataset  # noqa: E402
from app.research.btc_calibration import calibration_report  # noqa: E402
from app.research.dataset import Observation  # noqa: E402
from app.strategy.btc_model import ModelConfig  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def observation(step: Step) -> Observation | None:
    est = step.estimate
    if est.p_yes is None:
        return None
    mid = (step.yes_bid + step.yes_ask) / 2 if step.yes_bid is not None and step.yes_ask is not None else None
    f = step.features
    feats = {"p_yes": est.p_yes, "p_yes_low": est.p_yes_low, "p_yes_high": est.p_yes_high, "yes_mid": mid,
             "dist_sigma": f.dist_sigma, "spot": f.spot, "threshold": f.threshold, "backfill": 1.0}
    return Observation(
        ts=step.ts, ticker=step.ticker, asset="BTC", close_time=step.close, minute=int(step.t_left // 60),
        time_remaining=step.t_left, direction="UP" if est.p_yes >= 0.5 else "DOWN", confidence=0, quality=0,
        grade="BTC", regime=step.regime.value, accel_state=f.momentum_state, underlying_state="BTC",
        stability=None, yes_ask=step.yes_ask, no_ask=step.no_ask, spread=None,
        features={k: v for k, v in feats.items() if v is not None}, outcome=step.outcome, source="btc",
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", default=str(ROOT / "data" / "replay_cache"))
    ap.add_argument("--avg-window", type=int, default=60)
    args = ap.parse_args()
    cache = Path(args.cache)
    if not cache.exists() or not any(cache.glob("KXBTC*.json.gz")):
        print(f"No downloaded BTC markets in {cache}.\nRun first:  python scripts/replay.py --days 30")
        return 1
    settings = Settings()
    repo = Repository(Database(settings.database_url))
    repo.init()
    known = {o.ticker for o in repo.observations(source="btc", settled_only=False)}
    print("Replaying downloaded BTC markets (only data available at each minute is used)...")
    ds = load_dataset(cache, ModelConfig(basis_bps=settings.btc_basis_bps), args.avg_window)
    rows: list[Observation] = []
    added = 0
    for ticker, steps in ds.markets.items():
        if ticker in known:
            continue
        obs = [o for o in (observation(s) for s in steps) if o is not None]
        if obs:
            rows.extend(obs)
            added += 1
    if rows:
        repo.add_observations(rows)
    rep = calibration_report(repo.observations(source="btc"), settings.hist_min_samples, settings.calibration_ece_tol)
    st = rep.state
    print(f"\nAdded {added} markets ({len(rows)} minutes). Skipped {len(known)} already recorded.")
    print(f"Odds check: {st.status} on {rep.markets} markets"
          + (f" · calibration error {st.ece:.3f}" if st.ece is not None else "")
          + (f" · Brier bot {st.brier_model:.4f} vs Kalshi {st.brier_market:.4f}"
             if st.brier_model is not None and st.brier_market is not None else ""))
    for b in rep.buckets:
        if b.markets:
            print(f"  bot said {b.label:>8}  happened {b.actual * 100:5.1f}%  ({b.markets} markets)")
    print("\nRestart the bot so it picks this up:  sudo docker compose restart")
    return 0


if __name__ == "__main__":
    sys.exit(main())
