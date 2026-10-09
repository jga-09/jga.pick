"""Replay past Kalshi 15-minute markets through the real signal engine.

    python scripts/replay.py                 # last 14 days, BTC/ETH/SOL
    python scripts/replay.py --days 30
    python scripts/replay.py --report-only   # re-analyse what was already downloaded

Downloads settled markets (1-minute candles + trades) and Coinbase 1-minute spot
prices, caches them under data/replay_cache/, replays every market minute by
minute through the SAME engine the bot uses, stores the observations in
data/replay.db (never mixed with the bot's live statistics) and prints:
  * the full research report (calibration, regimes, buckets, features,
    walk-forward, filter ablation), and
  * a stress test of V4L = V4 without the order book.

LIMITS: Kalshi keeps no order-book history (no order-book component; V3/V4
cannot be replayed) and quotes are 1-minute closes, not 5-second snapshots.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Settings  # noqa: E402
from app.database.db import Database  # noqa: E402
from app.database.repository import Repository  # noqa: E402
from app.errors import KalshiAuthError  # noqa: E402
from app.kalshi.auth import KalshiSigner  # noqa: E402
from app.kalshi.client import KalshiClient  # noqa: E402
from app.kalshi.markets import MarketDiscovery, detect_asset  # noqa: E402
from app.research.backtest import BacktestConfig  # noqa: E402
from app.research.replay import CoinbaseHistory, HistoryDownloader, ReplayStats, replay_engine, replay_market  # noqa: E402
from app.research.reporting import research_report, stress_report  # noqa: E402
from app.utils.logging import setup_logging  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


async def download_and_replay(s: Settings, days: int, assets: list[str], db: Repository) -> ReplayStats:
    signer = None
    if s.has_kalshi_credentials:
        try:
            signer = KalshiSigner.from_file(s.kalshi_api_key_id or "", s.kalshi_private_key_path or "")
        except KalshiAuthError:
            signer = None  # public endpoints work without signing
    client = KalshiClient(s, signer)
    dl = HistoryDownloader(client, ROOT / "data" / "replay_cache")
    spot = CoinbaseHistory(ROOT / "data" / "replay_cache")
    stats = ReplayStats()
    summary: list[str] = []
    until = datetime.now(UTC)
    since = until - timedelta(days=days)
    try:
        series = await MarketDiscovery(client, s).resolve_series()
        series = {k: v for k, v in series.items() if v in assets}
        print(f"Series: {', '.join(series) or 'none found'}")
        for ser, asset in series.items():
            markets = await dl.settled_markets(ser, since, until)
            print(f"\n{ser} ({asset}): {len(markets)} settled markets in the last {days} days")
            if not markets:
                summary.append(f"{ser} ({asset}): NO settled markets found")
                continue
            ok = failed = n_obs = no_spot = 0
            spot_by_day: dict[str, list] = {}
            t0, done = time.time(), 0
            batch: list = []
            for i in range(0, len(markets), 50):
                chunk = markets[i:i + 50]
                records = await asyncio.gather(*(dl.market_data(ser, m) for m in chunk))
                for rec in records:
                    done += 1
                    if rec is None:
                        stats.skip("download failed")
                        failed += 1
                        continue
                    ok += 1
                    close = datetime.fromisoformat(rec["market"]["close_time"].replace("Z", "+00:00"))
                    day_key = close.strftime("%Y%m%d")
                    if day_key not in spot_by_day:
                        spot_by_day[day_key] = await spot.day(asset, close)
                    prices = spot_by_day[day_key]
                    if close.hour == 0 and close.minute <= 20:  # window may start on the previous day
                        prev = (close - timedelta(days=1)).strftime("%Y%m%d")
                        if prev not in spot_by_day:
                            spot_by_day[prev] = await spot.day(asset, close - timedelta(days=1))
                        prices = spot_by_day[prev] + prices
                    obs = replay_market(rec, asset, prices, replay_engine)
                    n_obs += len(obs)
                    no_spot += 0 if prices else 1
                    if not obs:
                        stats.skip("no valid setups")
                    stats.markets += 1
                    stats.observations += len(obs)
                    batch.extend(obs)
                rate = done / max(1e-6, time.time() - t0)
                print(f"  {done}/{len(markets)} markets  ({rate:.1f}/s)", end="\r", flush=True)
                if batch:
                    db.add_observations(batch)
                    batch = []
            print()
            summary.append(f"{ser} ({asset}): {len(markets)} found | {ok} downloaded | {failed} failed | "
                           f"{n_obs:,} observations | {no_spot} without spot prices")
        print("\nPER-SERIES SUMMARY")
        for line in summary:
            print("  " + line)
        missing = [a for a in assets if a not in series.values()]
        if missing:
            print(f"  ⚠️ no 15-minute series found for: {', '.join(missing)}")
        if not spot.available:
            print("\n⚠️ Coinbase spot history was unavailable - underlying components are missing, "
                  "so V4L cannot trigger. Other strategies are still reported.")
    finally:
        await spot.close()
        await client.close()
    return stats


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--assets", default=None, help="comma list, default = ASSETS from .env")
    ap.add_argument("--db", default=str(ROOT / "data" / "replay.db"))
    ap.add_argument("--report-only", action="store_true", help="skip downloading; analyse stored replay data")
    args = ap.parse_args()
    setup_logging("WARNING")
    s = Settings()
    assets = [a.strip().upper() for a in (args.assets or ",".join(s.asset_list)).split(",") if a.strip()]
    repo = Repository(Database(f"sqlite:///{Path(args.db).resolve()}"))
    repo.init()
    if not args.report_only:
        # rebuild the replay DB from the cache each time so re-runs never double count
        repo.db.engine.dispose()
        Path(args.db).unlink(missing_ok=True)
        repo = Repository(Database(f"sqlite:///{Path(args.db).resolve()}"))
        repo.init()
        stats = asyncio.run(download_and_replay(s, args.days, assets, repo))
        print(f"\nReplayed {stats.markets} markets -> {stats.observations:,} observations. "
              f"Skipped: {dict(stats.skipped) or 'none'}")
    obs = repo.observations(source="replay")
    cfg = BacktestConfig(fee_rate=s.fee_rate, slippage=s.paper_slippage_cents, min_samples=s.hist_min_samples,
                         prior_strength=s.calibration_prior_strength)
    print("\n" + "=" * 90)
    print("HISTORICAL REPLAY REPORT (real past Kalshi markets; no order book; 1-minute quotes)")
    print("=" * 90)
    print(research_report(obs, cfg, "days", 2))
    print("\n" + "=" * 90)
    print(stress_report(obs, "V4L", cfg, picked_on_this_data=False))
    by_asset = defaultdict(int)
    for o in obs:
        by_asset[o.asset] += 1
    print(f"\nObservations by asset: {dict(by_asset)}")
    _ = detect_asset


if __name__ == "__main__":
    main()
