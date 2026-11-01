"""Gold readiness check + pricing-model validation (research only, never trades).

    python scripts/gold_check.py                 # discover series, check rules, find the Pyth feed
    python scripts/gold_check.py --days 14       # also validate a Gold pricing model on settled markets

Step 1  Kalshi: list Gold series (from Kalshi's own /series), show each series' window, an open market's
        settlement rules, and whether ``validate_gold_market`` accepts it.
Step 2  Pyth: find the XAU/USD feed id (so you can set GOLD_PYTH_FEED_ID) and check the latest price is fresh.
Step 3  (--days) Model validation: download settled Gold markets (1-minute Kalshi candles) and Pyth 1-minute
        history, price every minute with a driftless log-normal model, and compare its accuracy (Brier score)
        with Kalshi's own prices. If Kalshi is at least as accurate, there is no model edge to trade.
Nothing is assumed: if Kalshi has no 15-minute Gold series, the output says so.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from app.config import Settings
from app.data.gold import HERMES, PythGoldProvider
from app.data.underlying import SpotPrice
from app.errors import KalshiError
from app.gold.markets import (
    discover_gold_series,
    frequency_minutes,
    validate_gold_market,
)
from app.kalshi.client import KalshiClient
from app.kalshi.market_data import parse_market_info
from app.research.fairvalue import brier, build_points, calibration_table
from app.research.replay import HistoryDownloader, _record_candles
from app.utils.time import parse_ts

ROOT = Path(__file__).resolve().parents[1]


async def find_pyth_feeds(http: httpx.AsyncClient) -> list[dict]:
    r = await http.get(f"{HERMES}/v2/price_feeds", params={"query": "XAU", "asset_type": "metal"})
    r.raise_for_status()
    out = []
    for f in r.json():
        attrs = f.get("attributes") or {}
        if "XAU" in str(attrs.get("symbol", "")).upper():
            out.append({"id": "0x" + str(f.get("id", "")).removeprefix("0x"), "symbol": attrs.get("symbol"),
                        "description": attrs.get("description")})
    return out


async def main_async(a: argparse.Namespace) -> int:
    s = Settings()
    client = KalshiClient(s)  # public market data needs no key
    http = httpx.AsyncClient(timeout=20)
    try:
        cats = [c.strip() for c in (a.categories or s.gold_categories).split(",") if c.strip()]
        print("STEP 1 - Kalshi Gold series")
        series, errors = await discover_gold_series(client, cats)
        for e in errors:
            print(f"   ⚠️ {e}")
        if not series:
            print("   ❌ No Gold series found on Kalshi. Gold stays disabled.")
            return 1
        for ser in series:
            mins = frequency_minutes(ser)
            src = ", ".join(str(x.get("name") or x) for x in ser.get("settlement_sources") or []) or "?"
            print(f"   {ser['ticker']:<14} window={mins or '?'} min  '{ser.get('title', '')}'  sources: {src}")
        fifteen = [x for x in series if frequency_minutes(x) == 15]
        print(f"   15-minute Gold series: {', '.join(x['ticker'] for x in fifteen) or 'NONE'}")

        print("\nSTEP 2 - Pyth XAU/USD feed")
        feed_id = s.gold_pyth_feed_id
        try:
            feeds = await find_pyth_feeds(http)
            for f in feeds[:6]:
                print(f"   {f['symbol']:<22} {f['id']}  {f.get('description') or ''}")
            spot_feed = next((f for f in feeds if str(f["symbol"]).upper().endswith("XAU/USD")), None)
            if not feed_id and spot_feed:
                feed_id = spot_feed["id"]
                print(f"   -> add to .env:  GOLD_PYTH_FEED_ID={feed_id}")
        except (httpx.HTTPError, ValueError) as exc:
            print(f"   ⚠️ Pyth lookup failed ({type(exc).__name__})")
        provider = PythGoldProvider(feed_id, stale_after_sec=s.gold_stale_sec) if feed_id else None
        if provider:
            spot = await provider.get_price("GOLD")
            print(f"   latest: {spot.price:.2f} at {spot.ts:%Y-%m-%d %H:%M:%S} UTC" if spot
                  else f"   latest: unavailable ({provider.last_error})")

        print("\nSTEP 3 - open market rules check")
        target = fifteen or series
        for ser in target[:3]:
            try:
                raws = await client.get_markets(series_ticker=ser["ticker"], status="open", limit=5, max_pages=1)
            except KalshiError as exc:
                print(f"   {ser['ticker']}: {exc}")
                continue
            if not raws:
                print(f"   {ser['ticker']}: no open market right now")
                continue
            info = parse_market_info(raws[0], asset="GOLD", series_ticker=ser["ticker"])
            chk = validate_gold_market(info, "pyth" if provider else None, datetime.now(UTC))
            print(f"   {info.ticker}: strike_type={info.strike_type} strike={info.floor_strike or info.cap_strike}")
            print(f"     rules: {info.rules_primary[:300]}")
            print(f"     {'✅ VERIFIED' if chk.verified else '❌ ' + '; '.join(chk.problems)}  (source: {chk.source})")

        if a.days and fifteen and provider:
            await validate_model(client, provider, fifteen[0]["ticker"], a.days)
        elif a.days:
            print("\nSTEP 4 skipped: needs a 15-minute Gold series AND a Pyth feed id.")
    finally:
        await client.close()
        await http.aclose()
    return 0


async def validate_model(client: KalshiClient, provider: PythGoldProvider, series: str, days: int) -> None:
    print(f"\nSTEP 4 - model validation on settled {series} markets (last {days} days)")
    cache = ROOT / "data" / "gold_cache"
    dl = HistoryDownloader(client, cache, trades=False)
    until = datetime.now(UTC)
    since = until - timedelta(days=days)
    markets = await dl.settled_markets(series, since, until)
    print(f"   settled markets: {len(markets)}")
    if not markets:
        return
    spot_file = cache / f"pyth_xau_{since:%Y%m%d}_{until:%Y%m%d}.json.gz"
    if spot_file.exists():
        rows = json.loads(gzip.decompress(spot_file.read_bytes()))
    else:
        bars = await provider.bars(since - timedelta(hours=2), until)
        rows = [[b.ts.timestamp(), b.close] for b in bars]
        spot_file.write_bytes(gzip.compress(json.dumps(rows).encode()))
    spot = [SpotPrice("GOLD", float(c), datetime.fromtimestamp(t, UTC), "pyth") for t, c in rows]
    print(f"   Pyth minutes: {len(spot):,}")
    points = []
    for m in markets:
        rec = await dl.market_data(series, m)
        if not rec:
            continue
        mk = rec["market"]
        close = parse_ts(mk.get("close_time"))
        strike = mk.get("floor_strike")
        if close is None or not isinstance(strike, (int, float)) or mk.get("result") not in ("yes", "no"):
            continue
        quotes = [(c.ts, c.yes_bid, c.yes_ask) for c in _record_candles(rec)]
        points += build_points(mk["ticker"], "GOLD", float(strike), close, mk["result"], quotes, spot)
    b_model, b_mid, n = brier(points)
    if not n:
        print("   no comparable minutes (missing Pyth history or quotes)")
        return
    print(f"   Brier: model {b_model:.4f} vs Kalshi mid {b_mid:.4f} over {n:,} minutes "
          f"({len({p.ticker for p in points})} markets)")
    for bucket, npts, nm, fair, act, mid in calibration_table(points):
        print(f"     {bucket:>8} {npts:7,} min {nm:5} mkts  model {fair:5.1%}  actual {act:5.1%}  "
              f"kalshi {mid if mid is None else f'{mid:5.1%}'}")
    if b_model >= b_mid:
        print("   ❌ Kalshi's own prices are at least as accurate: no model edge. Gold stays research-only.")
    else:
        print("   🟡 Model more accurate than Kalshi here. Next: a walk-forward EV test after fees "
              "(scripts/model_compare.py --asset GOLD). Still research-only.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=0)
    ap.add_argument("--categories", default=None, help="comma list of Kalshi categories to search")
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
