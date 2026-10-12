"""SYNTHETIC sanity check of the BTC backtest machinery (NOT market evidence).

    python scripts/btc_simulate.py

Runs the BTC walk-forward backtest twice on generated data:
  1. efficient quotes (exact fair prices)  -> a correct pipeline must NOT find an edge;
  2. lagged quotes (planted mispricing)    -> a correct pipeline SHOULD find it.
If either check fails, the backtest itself is broken and its real-data results cannot be trusted.
Data is written to a temporary folder and never mixed with real replay data.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.research.btc_backtest import format_report, load_dataset, run  # noqa: E402
from app.research.btc_synthetic import generate  # noqa: E402


def main() -> int:
    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        for mode in ("efficient", "lagged"):
            cache = Path(tmp) / mode
            generate(cache, days=8, mode=mode)
            report = run(load_dataset(cache))
            full = report["variants"]["full strategy"]["oos"]
            print(f"\n================ SYNTHETIC ({mode}) ================")
            print(format_report(report, full=False))
            if mode == "efficient":
                passed = full["trades"] == 0 or (full["avg_profit_per_trade"] or 0) <= 0.005
                print(f"\nCHECK no false edge on efficient prices: {'PASS' if passed else 'FAIL'}")
            else:
                passed = full["trades"] >= 30 and (full["avg_profit_per_trade"] or 0) > 0
                print(f"\nCHECK finds the planted mispricing: {'PASS' if passed else 'FAIL'}")
            ok &= passed
    print("\nAll machinery checks passed." if ok else "\nA machinery check FAILED - do not trust backtests.")
    print("(Synthetic data only - this says nothing about real Kalshi markets.)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
