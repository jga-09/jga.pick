"""Pre-registered test of a station-corrected forecast edge on Kalshi daily-high weather markets.

    python scripts/weather_download.py   # first
    python scripts/weather_test.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.research.weather import format_weather, load_weather, run_weather  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache", default=str(ROOT / "data" / "weather_cache"))
    args = ap.parse_args()
    cache = Path(args.cache)
    if not (cache / "markets").exists() or not any((cache / "markets").glob("*.json.gz")):
        print(f"No weather markets in {cache}.\nRun first:  python scripts/weather_download.py")
        return 1
    print("Loading weather markets, forecasts and observations...", flush=True)
    try:
        rep = run_weather(load_weather(cache))
    except ValueError as exc:
        print(f"Not enough data: {exc}. Download more:  python scripts/weather_download.py --days 240")
        return 1
    print()
    print(format_weather(rep))
    out = ROOT / "data" / "weather_report.json"
    out.write_text(json.dumps(rep, indent=2, default=str))
    print(f"\nSaved: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
