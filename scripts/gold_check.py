"""Gold readiness check + pricing-model validation (research only, never trades).

    python scripts/gold_check.py                 # discover series, check rules, find the Pyth feed
    python scripts/gold_check.py --days 14       # also validate a Gold pricing model on settled markets

Step 1  Kalshi: list Gold series (from Kalshi's own /series), show each series' window, an open market's
        settlement rules, and whether ``validate_gold_market`` accepts it.
Step 2  Price: Kalshi's own copy of the Pyth gold candles (GET /live_data/events/{event}) for the open market.
        (Pyth's public Hermes API now answers 401 without a key, so it is no longer used.)
Step 4  (--days) Model validation: download settled Gold markets (1-minute Kalshi candles) and, per event, the
        1-minute Pyth settlement candles from Kalshi, price every minute with a driftless log-normal model, and compare its accuracy (Brier score)
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


from app.config import Settings
from app.data.gold import KalshiGoldFeed
from app.errors import KalshiError
from app.gold.markets import (
    discover_gold_series,
    frequency_minutes,
    validate_gold_market,
)
from app.kalshi.client import KalshiClient
from app.kalshi.market_data import parse_market_info
from app.research.fairvalue import brier, build_points, calibration_table
from app.research.replay import CoinbaseHistory, HistoryDownloader, _record_candles
from app.utils.time import parse_ts

ROOT = Path(__file__).resolve().parents[1]


async def main_async(a: argparse.Namespace) -> int:
    s = Settings()
    client = KalshiClient(s)  # public market data needs no key
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

        provider = KalshiGoldFeed(stale_after_sec=s.gold_stale_sec)
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
            srcs = [str(x.get("name") or x) for x in ser.get("settlement_sources") or []]
            chk = validate_gold_market(info, provider.source, datetime.now(UTC), srcs)
            print(f"   {info.ticker}: strike_type={info.strike_type} strike={info.floor_strike or info.cap_strike}")
            print(f"     rules: {info.rules_primary[:300]}")
            print(f"     {'✅ VERIFIED' if chk.verified else '❌ ' + '; '.join(chk.problems)}  (source: {chk.source})")
            spot = await provider.latest(client, info.event_ticker)
            print("     price (Kalshi settlement candles): "
                  + (f"{spot.price:,.2f} at {spot.ts:%H:%M} UTC" if spot else f"unavailable ({provider.last_error})"))

        if a.days and fifteen:
            await validate_model(client, provider, fifteen[0]["ticker"], a.days)
        elif a.days:
            print("\nSTEP 4 skipped: no 15-minute Gold series.")
    finally:
        await client.close()
    return 0


async def validate_model(client: KalshiClient, provider: KalshiGoldFeed, series: str, days: int) -> None:
    print(f"\nSTEP 4 - model validation on settled {series} markets (last {days} days)")
    cache = ROOT / "data" / "gold_cache"
    dl = HistoryDownloader(client, cache, trades=False)
    until = datetime.now(UTC)
    since = until - timedelta(days=days)
    markets = await dl.settled_markets(series, since, until)
    print(f"   settled markets: {len(markets)}")
    if not markets:
        return
    # Kalshi does not publish past Gold settlement candles (/live_data is empty for Gold events), and Pyth's
    # public API now needs a key. Proxy: PAXG (Paxos Gold, 1 token = 1 fine troy oz) 1-minute candles from
    # Coinbase - real and timestamped, but NOT the settlement source, so its basis is measured below.
    hist = CoinbaseHistory(cache)
    days_list = sorted({(parse_ts(m["close_time"]) or until).date() for m in markets})
    spot = []
    try:
        for d in days_list:
            spot += await hist.day("PAXG", datetime(d.year, d.month, d.day, tzinfo=UTC))
    finally:
        await hist.close()
    spot = sorted({p.ts: p for p in spot}.values(), key=lambda p: p.ts)
    print(f"   PAXG (gold proxy) minutes from Coinbase: {len(spot):,}")
    if not spot:
        print("   ❌ No PAXG history (Coinbase PAXG-USD unavailable) - cannot validate a model.")
        return
    # basis check: Kalshi's strike IS the Pyth gold price at the window start -> compare PAXG there
    from app.research.fairvalue import spot_at

    diffs, agree, total = [], 0, 0
    recs = []
    for m in markets:
        rec = await dl.market_data(series, m)
        if rec:
            recs.append(rec)
    for rec in recs:
        mk = rec["market"]
        close, strike = parse_ts(mk.get("close_time")), mk.get("floor_strike")
        opened = parse_ts(mk.get("open_time"))
        if close is None or opened is None or not isinstance(strike, (int, float)):
            continue
        a, b = spot_at(spot, opened), spot_at(spot, close)
        if a is None or b is None:
            continue
        diffs.append((a.price / strike - 1) * 1e4)
        if mk.get("result") in ("yes", "no"):
            total += 1
            agree += int((b.price >= a.price) == (mk["result"] == "yes"))
    if diffs:
        ds = sorted(diffs)
        med = ds[len(ds) // 2]
        mad = sorted(abs(x - med) for x in ds)[len(ds) // 2]
        print(f"   basis PAXG vs Pyth (Kalshi strike) at window start: median {med:+.1f} bps, "
              f"typical deviation {mad:.1f} bps ({len(ds)} markets)")
    if total:
        print(f"   PAXG direction over the window agreed with Kalshi's result in {agree}/{total} "
              f"({agree / total:.1%}) markets")
        if agree / total < 0.95:
            print("   ⚠️ The proxy disagrees with settlement too often: model results below are unreliable.")
    rows = [[p.ts.timestamp(), p.price] for p in spot]
    (cache / "pyth_xau_proxy_paxg.json.gz").write_bytes(gzip.compress(json.dumps(rows).encode()))
    points = []
    for rec in recs:
        mk = rec["market"]
        close = parse_ts(mk.get("close_time"))
        strike = mk.get("floor_strike")
        if close is None or not isinstance(strike, (int, float)) or mk.get("result") not in ("yes", "no"):
            continue
        quotes = [(c.ts, c.yes_bid, c.yes_ask) for c in _record_candles(rec)]
        points += build_points(mk["ticker"], "GOLD", float(strike), close, mk["result"], quotes, spot)
    b_model, b_mid, n = brier(points)
    if not n:
        print("   no comparable minutes (missing PAXG history or quotes)")
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
