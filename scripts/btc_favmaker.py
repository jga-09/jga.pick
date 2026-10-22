"""Test the 'favourite maker' idea (post a bid on the favourite side) on REAL downloaded history.

    python scripts/btc_favmaker.py
    python scripts/btc_favmaker.py --holdout-days 7

Uses the cached markets AND their real trade tapes from scripts/replay.py. Pre-registered fixed
versions, conservative fill rule decides, final holdout used once. Changes nothing in the bot.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.research.btc_backtest import load_dataset  # noqa: E402
from app.research.btc_favmaker import compute_fills, format_favmaker, run_favmaker  # noqa: E402
from app.strategy.btc_model import ModelConfig  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", default=str(ROOT / "data" / "replay_cache"))
    ap.add_argument("--holdout-days", type=int, default=7)
    args = ap.parse_args()
    cache = Path(args.cache)
    if not cache.exists() or not any(cache.glob("KXBTC*.json.gz")):
        print(f"No BTC market history in {cache}.\nRun first:  python scripts/replay.py --days 30")
        return 1
    print("Loading cached BTC markets...", flush=True)
    ds = load_dataset(cache, ModelConfig())
    print(f"Checking fills against the real trade tape, one market at a time ({len(ds.markets)} markets)...",
          flush=True)
    fills, with_tape = compute_fills(cache, ds)
    try:
        rep = run_favmaker(ds, fills, args.holdout_days, with_tape)
    except ValueError as exc:
        print(f"Not enough data: {exc}")
        return 1
    print()
    print(format_favmaker(rep))
    out = ROOT / "data" / "favmaker_report.json"
    out.write_text(json.dumps(rep, indent=2, default=str))
    print(f"\nSaved: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
