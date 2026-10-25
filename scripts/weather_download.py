"""Download Kalshi daily-high weather markets, archived forecasts and official highs (resumable).

    python scripts/weather_download.py                # last 150 days, all 7 cities
    python scripts/weather_download.py --days 240 --rps 2
    python scripts/weather_download.py --cities ny chi

No Kalshi key is needed (public market data). Saved in data/weather_cache. Re-run to fill gaps.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.kalshi.client import KalshiClient  # noqa: E402
from app.research.weather import CITIES  # noqa: E402
from app.research.weather_download import download_all  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


async def run(args: argparse.Namespace) -> int:
    client = KalshiClient(Settings().model_copy(update={"kalshi_read_rps": args.rps}), None)
    try:
        await download_all(client, Path(args.cache), args.days, args.cities,
                           say=lambda s: print(s, flush=True))
    finally:
        await client.close()
    print("\nDone. Next:  python scripts/weather_test.py")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", default=str(ROOT / "data" / "weather_cache"))
    ap.add_argument("--days", type=int, default=150)
    ap.add_argument("--rps", type=float, default=3.0, help="Kalshi requests per second (lower if you get 429s)")
    ap.add_argument("--cities", nargs="*", choices=[c.key for c in CITIES], default=None)
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
