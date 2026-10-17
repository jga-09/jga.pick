"""Backtest "post your own bid" (maker) entries on REAL history downloaded by scripts/replay.py.

    python scripts/btc_maker_backtest.py
    python scripts/btc_maker_backtest.py --offsets 1,2 --wait 3 --maker-fee 0

A bid only counts as filled if a later 1-minute quote shows the ask at or below it. Unfilled
orders are missed trades. Everything is out-of-sample (walk-forward), labelled BACKTEST.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.research.btc_backtest import SimCosts, load_dataset  # noqa: E402
from app.research.btc_maker import format_maker_report, run_maker  # noqa: E402
from app.strategy.btc_model import ModelConfig  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", default=str(ROOT / "data" / "replay_cache"))
    ap.add_argument("--out", default=str(ROOT / "data" / "btc_maker_report.json"))
    ap.add_argument("--offsets", default="1,2,3", help="cents below the ask to bid, comma separated")
    ap.add_argument("--wait", type=float, default=5.0, help="minutes before an unfilled bid is cancelled")
    ap.add_argument("--maker-fee", type=float, default=0.0175, help="maker fee rate (0 if none)")
    ap.add_argument("--fee-rate", type=float, default=0.07, help="taker fee rate for the comparison row")
    ap.add_argument("--slippage", type=float, default=1.0)
    ap.add_argument("--train-days", type=int, default=3)
    ap.add_argument("--val-days", type=int, default=1)
    args = ap.parse_args()
    cache = Path(args.cache)
    if not cache.exists() or not any(cache.glob("KXBTC*.json.gz")):
        print(f"No BTC market history in {cache}.\nRun first:  python scripts/replay.py --days 30")
        return 1
    print("Loading and replaying cached BTC markets (only data available at each minute is used)...")
    ds = load_dataset(cache, ModelConfig())
    if len(ds.days) <= args.train_days + args.val_days:
        print(f"Only {len(ds.days)} days of data - download more.")
        return 1
    offsets = tuple(float(x) for x in args.offsets.split(",") if x.strip())
    report = run_maker(ds, SimCosts(args.fee_rate, args.slippage), args.train_days, args.val_days, offsets,
                       args.wait, args.maker_fee)
    print()
    print(format_maker_report(report))
    Path(args.out).write_text(json.dumps(report, indent=2, default=str))
    print(f"\nSaved: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
