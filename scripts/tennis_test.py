"""Kalshi tennis pre-match prices vs Pinnacle's closing line (upper-bound test) + honest baseline.

    python scripts/tennis_download.py   # first
    python scripts/tennis_test.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.research.tennis import format_tennis, load_kalshi, load_odds, run_tennis  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache", default=str(ROOT / "data" / "tennis_cache"))
    cache = Path(ap.parse_args().cache)
    if not (cache / "markets").exists():
        print(f"No tennis data in {cache}.\nRun first:  python scripts/tennis_download.py")
        return 1
    print("Loading Kalshi tennis markets and Pinnacle odds...", flush=True)
    matches, odds = load_kalshi(cache / "markets"), load_odds(cache / "td")
    print(f"  {len(matches)} two-player Kalshi matches · {len(odds)} tour matches in the odds files")
    try:
        rep = run_tennis(matches, odds)
    except ValueError as exc:
        print(f"Not enough data: {exc}.")
        return 1
    print()
    print(format_tennis(rep))
    out = ROOT / "data" / "tennis_report.json"
    out.write_text(json.dumps(rep, indent=2, default=str))
    print(f"\nSaved: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
