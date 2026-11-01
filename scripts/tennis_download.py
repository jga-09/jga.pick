"""Download Kalshi tennis match markets and tennis-data.co.uk Pinnacle odds (resumable).

    python scripts/tennis_download.py             # last 150 days
    python scripts/tennis_download.py --days 240 --rps 2

No Kalshi key is needed. Saved in data/tennis_cache. Re-run to fill gaps.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.kalshi.client import KalshiClient  # noqa: E402
from app.research.tennis_download import kalshi_tennis, odds_files  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


async def run(args: argparse.Namespace) -> int:
    cache = Path(args.cache)
    client = KalshiClient(Settings().model_copy(update={"kalshi_read_rps": args.rps}), None)
    say = lambda s: print(s, flush=True)  # noqa: E731
    try:
        say("Kalshi tennis markets + hourly quotes...")
        await kalshi_tennis(client, cache, args.days, say)
    finally:
        await client.close()
    say("Pinnacle closing odds (tennis-data.co.uk)...")
    await odds_files(cache, args.days, say)
    say("\nDone. Next:  python scripts/tennis_test.py")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", default=str(ROOT / "data" / "tennis_cache"))
    ap.add_argument("--days", type=int, default=150)
    ap.add_argument("--rps", type=float, default=3.0, help="Kalshi requests per second (lower if you get 429s)")
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
