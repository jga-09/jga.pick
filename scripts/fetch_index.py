"""Add Kalshi's settlement index (CF Benchmarks) to the BTC markets already downloaded.

    python scripts/fetch_index.py              # resumable; skips markets that already have it
    python scripts/fetch_index.py --rps 2      # slower if Kalshi answers "429 too many requests"

For every cached KXBTC15M market it calls GET /live_data/events/{event_ticker} and stores the
1-minute index closes in the cached file (key "index"). Nothing else is changed.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.data.settlement_index import compact, parse_index_bars  # noqa: E402
from app.errors import KalshiError  # noqa: E402
from app.kalshi.client import KalshiClient  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


async def run(cache: Path, rps: float, limit: int | None) -> int:
    settings = Settings().model_copy(update={"kalshi_read_rps": rps})
    client = KalshiClient(settings, None)
    files = sorted(cache.glob("KXBTC*.json.gz"))
    done = have = empty = failed = 0
    try:
        for i, f in enumerate(files, 1):
            if limit is not None and done >= limit:
                break
            try:
                rec = json.loads(gzip.decompress(f.read_bytes()))
            except (OSError, ValueError):
                continue
            if rec.get("index"):
                have += 1
                continue
            ev = (rec.get("market") or {}).get("event_ticker")
            if not ev:
                continue
            try:
                bars = parse_index_bars(await client.get_event_live_data(ev))
            except KalshiError as exc:
                failed += 1
                if failed <= 3:
                    print(f"  {ev}: {exc}")
                continue
            done += 1
            if not bars:
                empty += 1
                continue
            rec["index"] = compact(bars)
            f.write_bytes(gzip.compress(json.dumps(rec, separators=(",", ":")).encode()))
            if done % 50 == 0:
                print(f"  {i}/{len(files)} files · {done} fetched ({empty} without index data) · {failed} failed",
                      flush=True)
    finally:
        await client.close()
    print(f"\nDone: {done} fetched ({done - empty} with index data, {empty} empty), {have} already had it, "
          f"{failed} failed. Re-run to retry failures.")
    if done and empty == done:
        print("Kalshi returned no index candles for past events - the comparison test cannot run on history.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", default=str(ROOT / "data" / "replay_cache"))
    ap.add_argument("--rps", type=float, default=3.0, help="requests per second (lower if you get 429s)")
    ap.add_argument("--limit", type=int, default=None, help="fetch at most this many markets (testing)")
    args = ap.parse_args()
    cache = Path(args.cache)
    if not cache.exists() or not any(cache.glob("KXBTC*.json.gz")):
        print(f"No BTC market history in {cache}.\nRun first:  python scripts/replay.py --days 30")
        return 1
    return asyncio.run(run(cache, args.rps, args.limit))


if __name__ == "__main__":
    sys.exit(main())
