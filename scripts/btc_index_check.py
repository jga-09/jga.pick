"""Does Kalshi's settlement index make the BTC model more accurate than Coinbase prices?

    python scripts/fetch_index.py      # first: add the index to the downloaded markets
    python scripts/btc_index_check.py

Pre-registered comparison on the SAME markets: Brier score of the model using Coinbase spot vs using
the settlement-index spot vs Kalshi's own price (lower = more accurate). Then the full profitability
audit is re-run with the index spot (development days + untouched holdout). Changes nothing in the bot.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.research.btc_audit import calibration, format_audit, run_audit  # noqa: E402
from app.research.btc_backtest import load_dataset  # noqa: E402
from app.strategy.btc_model import ModelConfig  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def tickers_with_index(cache: Path) -> set[str]:
    out = set()
    for f in cache.glob("KXBTC*.json.gz"):
        try:
            rec = json.loads(gzip.decompress(f.read_bytes()))
        except (OSError, ValueError):
            continue
        if rec.get("index"):
            out.add((rec.get("market") or {}).get("ticker"))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", default=str(ROOT / "data" / "replay_cache"))
    ap.add_argument("--holdout-days", type=int, default=7)
    args = ap.parse_args()
    cache = Path(args.cache)
    only = tickers_with_index(cache)
    if len(only) < 100:
        print(f"Only {len(only)} cached markets have settlement-index data. Run scripts/fetch_index.py first.")
        return 1
    print(f"Comparing on {len(only)} markets that have settlement-index data...", flush=True)
    cb = load_dataset(cache, ModelConfig(), only=only)
    ix = load_dataset(cache, ModelConfig(), spot_source="index", only=only)
    common = set(cb.markets) & set(ix.markets)
    cb.markets = {k: v for k, v in cb.markets.items() if k in common}
    ix.markets = {k: v for k, v in ix.markets.items() if k in common}
    c_cb, c_ix = calibration(cb.markets), calibration(ix.markets)
    print(f"\nACCURACY ON THE SAME {len(common)} MARKETS (Brier score, lower = better)")
    print(f"  model with Coinbase spot   : {c_cb['brier_model']:.4f}   ECE {c_cb['ece']:.3f}")
    print(f"  model with settlement index: {c_ix['brier_model']:.4f}   ECE {c_ix['ece']:.3f}")
    print(f"  Kalshi's own price         : {c_ix['brier_market']:.4f}")
    for k in c_ix["by_minutes_left"]:
        a, b = c_cb["by_minutes_left"].get(k, {}), c_ix["by_minutes_left"][k]
        print(f"   {k:>6} left: Coinbase {a.get('brier_model') or 0:.4f} | index {b['brier_model']:.4f} | "
              f"Kalshi {b['brier_market']:.4f}")
    helps = c_ix["brier_model"] < c_cb["brier_model"]
    beats = c_ix["brier_model"] < c_ix["brier_market"]
    print("\nIndex improves the model: " + ("YES" if helps else "NO")
          + " · Model beats Kalshi's price with the index: " + ("YES" if beats else "NO"))
    print("\nRe-running the profitability audit with the settlement-index spot...", flush=True)
    try:
        rep = run_audit(ix, holdout_days=args.holdout_days)
    except ValueError as exc:
        print(f"Audit skipped: {exc}")
        return 0
    print(format_audit(rep))
    out = ROOT / "data" / "index_check_report.json"
    out.write_text(json.dumps({"coinbase": c_cb, "index": c_ix, "audit": rep}, indent=2, default=str))
    print(f"\nSaved: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
