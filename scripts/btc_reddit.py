"""Test AstroTick's 'reddit_time_delay' strategy on REAL downloaded BTC 15-minute history.

    python scripts/btc_reddit.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.research.btc_backtest import load_dataset  # noqa: E402
from app.research.btc_reddit import format_reddit, run_reddit  # noqa: E402
from app.strategy.btc_model import ModelConfig  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache", default=str(ROOT / "data" / "replay_cache"))
    ap.add_argument("--holdout-days", type=int, default=7)
    args = ap.parse_args()
    cache = Path(args.cache)
    if not cache.exists() or not any(cache.glob("KXBTC*.json.gz")):
        print(f"No BTC market history in {cache}.\nRun first:  python scripts/replay.py --days 30")
        return 1
    print("Loading cached BTC markets...", flush=True)
    rep = run_reddit(load_dataset(cache, ModelConfig()), args.holdout_days)
    print()
    print(format_reddit(rep))
    out = ROOT / "data" / "reddit_report.json"
    out.write_text(json.dumps(rep, indent=2, default=str))
    print(f"\nSaved: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
