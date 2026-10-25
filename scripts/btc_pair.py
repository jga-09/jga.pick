"""Test 'pair completion' (bids on both YES and NO) on REAL downloaded BTC 15-minute history.

    python scripts/btc_pair.py

Fills from the real trade tape (conservative rule decides); development days then an untouched holdout.
Changes nothing in the bot.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.research.btc_backtest import SimCosts, load_dataset  # noqa: E402
from app.research.btc_pair import format_pair, run_pair  # noqa: E402
from app.strategy.btc_model import ModelConfig  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", default=str(ROOT / "data" / "replay_cache"))
    ap.add_argument("--holdout-days", type=int, default=7)
    args = ap.parse_args()
    cache = Path(args.cache)
    if not cache.exists() or not any(cache.glob("KXBTC*.json.gz")):
        print(f"No BTC market history in {cache}.\nRun first:  python scripts/replay.py --days 45")
        return 1
    print("Loading cached BTC markets...", flush=True)
    ds = load_dataset(cache, ModelConfig())
    print(f"Checking both legs against the real trade tape, one market at a time ({len(ds.markets)} markets)...",
          flush=True)
    try:
        rep = run_pair(cache, ds, args.holdout_days, SimCosts())
    except ValueError as exc:
        print(f"Not enough data: {exc}")
        return 1
    print()
    print(format_pair(rep))
    out = ROOT / "data" / "pair_report.json"
    out.write_text(json.dumps(rep, indent=2, default=str))
    print(f"\nSaved: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
