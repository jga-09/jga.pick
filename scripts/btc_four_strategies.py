"""Test Hammer / Drift / Surge / Fade on REAL downloaded BTC 15-minute history.

    python scripts/btc_four_strategies.py

Rules ported unchanged from the Kalshi AI project; development days then an untouched holdout;
Drift fills come from the real trade tape. Changes nothing in the bot.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.research.btc_backtest import SimCosts, load_dataset  # noqa: E402
from app.research.btc_four import format_four, run_four  # noqa: E402
from app.strategy.btc_model import ModelConfig  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", default=str(ROOT / "data" / "replay_cache"))
    ap.add_argument("--holdout-days", type=int, default=7)
    ap.add_argument("--slippage", type=float, default=1.0)
    args = ap.parse_args()
    cache = Path(args.cache)
    if not cache.exists() or not any(cache.glob("KXBTC*.json.gz")):
        print(f"No BTC market history in {cache}.\nRun first:  python scripts/replay.py --days 30")
        return 1
    print("Loading cached BTC markets...", flush=True)
    ds = load_dataset(cache, ModelConfig())
    print(f"Running the four strategies market by market ({len(ds.markets)} markets)...", flush=True)
    try:
        rep = run_four(cache, ds, args.holdout_days, SimCosts(slippage_cents=args.slippage))
    except ValueError as exc:
        print(f"Not enough data: {exc}")
        return 1
    print()
    print(format_four(rep))
    out = ROOT / "data" / "four_strategies_report.json"
    out.write_text(json.dumps(rep, indent=2, default=str))
    print(f"\nSaved: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
