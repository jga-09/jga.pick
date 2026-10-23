"""Pre-registered late-entry test on REAL downloaded BTC 15-minute history.

    python scripts/replay.py --days 45      # make sure markets after 2026-10-12 are downloaded
    python scripts/btc_late_entry.py

Only markets after 2026-10-12 (never examined when the hypothesis was formed) decide the result.
Changes nothing in the bot.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.research.btc_backtest import SimCosts, load_dataset  # noqa: E402
from app.research.btc_late import FRESH_AFTER, format_late, run_late  # noqa: E402
from app.strategy.btc_model import ModelConfig  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", default=str(ROOT / "data" / "replay_cache"))
    args = ap.parse_args()
    cache = Path(args.cache)
    if not cache.exists() or not any(cache.glob("KXBTC*.json.gz")):
        print(f"No BTC market history in {cache}.\nRun first:  python scripts/replay.py --days 45")
        return 1
    print("Loading cached BTC markets...", flush=True)
    ds = load_dataset(cache, ModelConfig())
    rep = run_late(ds, SimCosts(), FRESH_AFTER)
    print()
    print(format_late(rep))
    out = ROOT / "data" / "late_entry_report.json"
    out.write_text(json.dumps(rep, indent=2, default=str))
    print(f"\nSaved: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
