"""Backtest the BTC 15-minute engine on REAL history downloaded by scripts/replay.py.

    python scripts/replay.py --days 30          # 1) download settled BTC markets + Coinbase prices (cached)
    python scripts/btc_backtest.py              # 2) walk-forward backtest, report + data/btc_backtest_report.json
    python scripts/btc_backtest.py --risk low --slippage 2 --train-days 5 --val-days 2

Every minute of every settled market is replayed through the SAME feature, probability-model and
no-trade code the bot runs live, using only information available at that minute. Parameters are
chosen on earlier days and tested on later, unseen days. The report labels everything as BACKTEST.
The Telegram 🧪 Backtest screen shows the last saved report.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.research.btc_backtest import SimCosts, format_report, load_dataset, run, save_report  # noqa: E402
from app.risk.profiles import RiskLevel  # noqa: E402
from app.strategy.btc_model import ModelConfig  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", default=str(ROOT / "data" / "replay_cache"))
    ap.add_argument("--out", default=str(ROOT / "data" / "btc_backtest_report.json"))
    ap.add_argument("--risk", default="medium", choices=[r.value for r in RiskLevel],
                    help="risk profile whose filters the main 'full strategy' uses (all three are reported)")
    ap.add_argument("--fee-rate", type=float, default=0.07)
    ap.add_argument("--slippage", type=float, default=1.0, help="cents per contract on top of the ask")
    ap.add_argument("--capital", type=float, default=100.0, help="starting capital for return/sizing (USD)")
    ap.add_argument("--train-days", type=int, default=3)
    ap.add_argument("--val-days", type=int, default=1)
    ap.add_argument("--avg-window", type=int, default=60,
                    help="settlement averaging window (s) assumed for cached markets without rules text")
    ap.add_argument("--basis-bps", type=float, default=3.0)
    args = ap.parse_args()

    cache = Path(args.cache)
    if not cache.exists() or not any(cache.glob("KXBTC*.json.gz")):
        print(f"No BTC market history in {cache}.\nRun first:  python scripts/replay.py --days 30")
        return 1
    print("Loading and replaying cached BTC markets (only data available at each minute is used)...")
    ds = load_dataset(cache, ModelConfig(basis_bps=args.basis_bps), args.avg_window)
    if not ds.markets:
        print("No usable BTC markets. Skipped:", ds.skipped)
        return 1
    if len(ds.days) <= args.train_days + args.val_days:
        print(f"Only {len(ds.days)} days of data: need more than train+validation "
              f"({args.train_days}+{args.val_days}) days for an out-of-sample test. Download more days.")
        return 1
    report = run(ds, SimCosts(args.fee_rate, args.slippage), args.capital, args.train_days, args.val_days,
                 RiskLevel(args.risk))
    print()
    print(format_report(report, full=True))
    if ds.skipped:
        print("\nSkipped markets:", ", ".join(f"{v} {k}" for k, v in ds.skipped.items()))
    save_report(report, Path(args.out))
    print(f"\nSaved: {args.out}  (shown in Telegram under 🧪 Backtest)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
