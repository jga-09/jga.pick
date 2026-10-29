"""BUY THE FAVOURITE - research backtest on real settled Kalshi markets.

Rule: in each 15-minute market, MINUTES_LEFT minutes before close, the favourite is the
side whose ask is higher. If that ask is within [ASK_LO, ASK_HI] cents, buy one contract
at the ask and hold it to settlement. At most one trade per market.

Research only: this script only READS public market data. It never places an order and
needs no Kalshi account or API key. Standard library + requests.

    pip install requests
    python3 scripts/favourite_test.py

Responses are cached in ./kalshi_cache so reruns are fast and do not refetch.
"""

from __future__ import annotations

import hashlib
import json
import math
import sys
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import requests

# ------------------------------------------------------------------ SETTINGS
SERIES = "KXBTC15M"
DAYS_BACK = 30
MINUTES_LEFT = 3
ASK_LO = 85
ASK_HI = 95
MAX_SPREAD = 5  # cents; a wider book is not a price you could have traded
ROBUSTNESS_MINUTES = (1, 3, 6, 10)
BUCKETS = ((85, 88, "85-87"), (88, 91, "88-90"), (91, 94, "91-93"), (94, 95.0001, "94-95"))

BASE = "https://api.elections.kalshi.com/trade-api/v2"
CACHE = Path("kalshi_cache")
LIST_CACHE_HOURS = 6  # market lists change over time; candles and settled data never do
SLEEP = 0.15


# ------------------------------------------------------------------ HTTP
class NotFound(Exception):
    pass


_session = requests.Session()
_session.headers["User-Agent"] = "favourite-backtest/1.0 (research, read-only)"


def get(path: str, params: dict | None = None, ttl_hours: float | None = None) -> dict:
    """GET with a disk cache, polite pacing and retries on 429/5xx. Read-only by construction."""
    params = {k: v for k, v in (params or {}).items() if v is not None}
    key = hashlib.sha256(json.dumps([path, sorted(params.items())]).encode()).hexdigest()[:32]
    f = CACHE / f"{key}.json"
    if f.exists() and (ttl_hours is None or time.time() - f.stat().st_mtime < ttl_hours * 3600):
        data = json.loads(f.read_text())
        if data.get("__not_found__"):
            raise NotFound(path)
        return data
    for attempt in range(6):
        time.sleep(SLEEP)
        try:
            r = _session.get(BASE + path, params=params, timeout=30)
        except requests.RequestException as exc:
            if attempt == 5:
                raise
            print(f"  network error ({type(exc).__name__}), retrying...", file=sys.stderr)
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 429 or r.status_code >= 500:
            wait = float(r.headers.get("Retry-After") or 2 ** attempt)
            print(f"  HTTP {r.status_code} on {path}, retrying in {wait:.0f}s", file=sys.stderr)
            time.sleep(wait)
            continue
        CACHE.mkdir(exist_ok=True)
        if r.status_code in (400, 404):
            f.write_text(json.dumps({"__not_found__": True}))
            raise NotFound(path)
        r.raise_for_status()
        data = r.json()
        f.write_text(json.dumps(data))
        return data
    raise RuntimeError(f"giving up on {path} after repeated 429/5xx errors")


def cents(v) -> float | None:
    """'0.9450' -> 94.5 (dollars string, tenths of a cent allowed)."""
    if v is None or v == "":
        return None
    try:
        return round(float(v) * 100, 4)
    except (TypeError, ValueError):
        return None


def parse_time(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC)


# ------------------------------------------------------------------ data
def fee_info() -> tuple[str, float]:
    s = get(f"/series/{SERIES}", ttl_hours=LIST_CACHE_HOURS).get("series") or {}
    return str(s.get("fee_type")), float(s.get("fee_multiplier") or 1)


def settled_markets(since: datetime) -> tuple[list[dict], set[str]]:
    """Settled markets closing after ``since`` from the live and historical endpoints."""
    found: dict[str, dict] = {}
    historical: set[str] = set()
    day_start = int(since.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    cursor = None
    while True:
        d = get("/markets", {"series_ticker": SERIES, "status": "settled", "limit": 1000,
                             "min_close_ts": day_start, "cursor": cursor}, ttl_hours=LIST_CACHE_HOURS)
        for m in d.get("markets") or []:
            found[m["ticker"]] = m
        cursor = d.get("cursor") or None
        if not cursor:
            break
    cursor = None
    while True:
        try:
            d = get("/historical/markets", {"series_ticker": SERIES, "limit": 1000, "cursor": cursor},
                    ttl_hours=LIST_CACHE_HOURS)
        except NotFound:
            break
        page = d.get("markets") or []
        for m in page:
            if m["ticker"] not in found:
                found[m["ticker"]] = m
                historical.add(m["ticker"])
        cursor = d.get("cursor") or None
        closes = [parse_time(m["close_time"]) for m in page if m.get("close_time")]
        if not cursor or not page or (closes and max(closes) < since):
            break
    out = [m for m in found.values()
           if m.get("close_time") and parse_time(m["close_time"]) >= since and m.get("result") in ("yes", "no")]
    return sorted(out, key=lambda m: m["close_time"]), historical


def candles(m: dict, historical: bool) -> list[dict]:
    close = int(parse_time(m["close_time"]).timestamp())
    params = {"start_ts": close - 900, "end_ts": close, "period_interval": 1}
    paths = [f"/series/{SERIES}/markets/{m['ticker']}/candlesticks", f"/historical/markets/{m['ticker']}/candlesticks"]
    if historical:
        paths.reverse()
    for p in paths:
        try:
            c = get(p, params).get("candlesticks") or []
        except NotFound:
            continue
        if c:
            return c
    return []


def close_field(c: dict, side: str) -> float | None:
    d = c.get(side)
    if not isinstance(d, dict):
        return None
    return cents(d.get("close_dollars", d.get("close")))  # live: close_dollars, historical: close


# ------------------------------------------------------------------ simulation
def fee_cents(p: float, mult: float) -> int:
    return math.ceil(round(mult * 0.07 * p * (100 - p) / 100, 9))


def simulate(markets: list[dict], cdl: dict[str, list[dict]], minutes_left: int, mult: float):
    trades, skipped = [], Counter()
    for m in markets:
        close = parse_time(m["close_time"])
        target = int(close.timestamp()) - minutes_left * 60
        # ONLY the candle ending exactly at the decision minute: nothing after it is ever read.
        c = next((x for x in cdl.get(m["ticker"], []) if int(x.get("end_period_ts", -1)) == target), None)
        if c is None:
            skipped["no candle at the decision minute"] += 1
            continue
        bid, ask = close_field(c, "yes_bid"), close_field(c, "yes_ask")
        if not bid:
            skipped["yes bid 0 or missing (empty book)"] += 1
            continue
        if ask is None or ask >= 100:
            skipped["yes ask missing"] += 1
            continue
        if ask - bid > MAX_SPREAD:
            skipped[f"spread wider than {MAX_SPREAD}c"] += 1
            continue
        no_ask = 100 - bid
        if ask == no_ask:
            skipped["no favourite (asks equal)"] += 1
            continue
        side, price = ("yes", ask) if ask > no_ask else ("no", no_ask)
        if not (ASK_LO <= price <= ASK_HI):
            skipped[f"favourite ask outside {ASK_LO}-{ASK_HI}c"] += 1
            continue
        fee = fee_cents(price, mult)  # fill at the ask, never better
        won = m["result"] == side
        profit = (100 - price - fee) if won else (-price - fee)
        trades.append({"ticker": m["ticker"], "close": close, "decision": datetime.fromtimestamp(target, UTC),
                       "yes_bid": bid, "yes_ask": ask, "side": side, "price": price, "fee": fee,
                       "result": m["result"], "won": won, "profit": profit})
    return trades, skipped


# ------------------------------------------------------------------ report
def avg(xs):
    return sum(xs) / len(xs) if xs else float("nan")


def report(trades, skipped, minutes_left: int, since: datetime, until: datetime, n_markets: int, full: bool):
    print(f"\n=== MINUTES_LEFT = {minutes_left}  (favourite ask {ASK_LO}-{ASK_HI}c) ===")
    print(f"Markets: {n_markets} · trades: {len(trades)} · skipped: {sum(skipped.values())}")
    for why, n in skipped.most_common():
        print(f"   skipped {n:5d}: {why}")
    if not trades:
        print("No trades.")
        return "not enough data"
    profits = [t["profit"] for t in trades]
    wr = avg([t["won"] for t in trades])
    print(f"1) Trades {len(trades)} · win rate {wr:.1%} · avg price {avg([t['price'] for t in trades]):.2f}c · "
          f"avg profit {avg(profits):+.2f}c/contract after fee · total ${sum(profits) / 100:+.2f}")

    if full:
        print("\n2) By price bucket")
        print("   bucket  trades  win rate  break-even WR  avg profit")
        for lo, hi, label in BUCKETS:
            b = [t for t in trades if lo <= t["price"] < hi]
            if not b:
                print(f"   {label:>6}  {0:6d}         -              -           -")
                continue
            be = (avg([t["price"] for t in b]) + avg([t["fee"] for t in b])) / 100
            print(f"   {label:>6}  {len(b):6d}  {avg([t['won'] for t in b]):8.1%}  {be:13.1%}  {avg([t['profit'] for t in b]):+9.2f}c")

    mid = since + (until - since) / 2
    first = [t["profit"] for t in trades if t["close"] < mid]
    second = [t["profit"] for t in trades if t["close"] >= mid]
    by_day = defaultdict(float)
    for t in trades:
        by_day[t["close"].date()] += t["profit"]
    total = sum(profits)
    best_day, best = max(by_day.items(), key=lambda kv: kv[1])
    share = best / total if total > 0 else None
    print(f"\n3) First half (before {mid:%Y-%m-%d %H:%M} UTC): {len(first)} trades, avg {avg(first):+.2f}c · "
          f"second half: {len(second)} trades, avg {avg(second):+.2f}c")
    print(f"   Best day {best_day}: {best / 100:+.2f}$ = "
          + (f"{share:.0%} of total profit" if share is not None else "n/a (total profit is not positive)"))

    if len(trades) < 300:
        verdict = "not enough data"
    elif first and second and avg(first) > 0 and avg(second) > 0 and share is not None and share <= 0.40:
        verdict = "holds up so far"
    else:
        verdict = "not yet"
    print(f"\n4) VERDICT: {verdict}")
    if verdict == "not enough data":
        print(f"   ({len(trades)} trades; 300 needed.)")
    elif verdict == "not yet":
        print("   (needs a positive average after the fee in BOTH halves and no day above 40% of the profit)")

    if full:
        print("\n5) Example trades to check by hand on Kalshi")
        step = max(1, len(trades) // 3)
        for t in trades[::step][:3]:
            print(f"   {t['ticker']}: at {t['decision']:%Y-%m-%d %H:%M} UTC ({minutes_left} min before close) "
                  f"yes bid {t['yes_bid']:.1f}c / yes ask {t['yes_ask']:.1f}c -> bought {t['side'].upper()} at "
                  f"{t['price']:.1f}c + fee {t['fee']}c; result {t['result'].upper()} -> "
                  f"{'WON' if t['won'] else 'LOST'} {t['profit']:+.1f}c")
    return verdict


def main() -> int:
    until = datetime.now(UTC)
    since = until - timedelta(days=DAYS_BACK)
    print(f"BUY THE FAVOURITE · {SERIES} · {since:%Y-%m-%d} .. {until:%Y-%m-%d} · research only, no orders")
    fee_type, mult = fee_info()
    print(f"Fee schedule from /series: fee_type={fee_type}, fee_multiplier={mult} "
          f"-> fee on a 90c contract = {fee_cents(90, mult)}c")
    if fee_type not in ("quadratic", "None", ""):
        print(f"   ⚠️ fee_type '{fee_type}' is not the quadratic schedule this formula assumes - check it.")
    markets, historical = settled_markets(since)
    print(f"Settled markets found: {len(markets)} ({len(historical)} from the historical archive)")
    cdl: dict[str, list[dict]] = {}
    for i, m in enumerate(markets, 1):
        cdl[m["ticker"]] = candles(m, m["ticker"] in historical)
        if i % 100 == 0 or i == len(markets):
            print(f"  candles {i}/{len(markets)}", end="\r", flush=True)
    print()
    no_candles = sum(1 for v in cdl.values() if not v)
    if no_candles:
        print(f"Markets with no candles at all: {no_candles}")
    trades, skipped = simulate(markets, cdl, MINUTES_LEFT, mult)
    report(trades, skipped, MINUTES_LEFT, since, until, len(markets), full=True)
    print("\n" + "=" * 70 + "\nROBUSTNESS: same report at other decision times")
    summary = []
    for ml in ROBUSTNESS_MINUTES:
        t, s = simulate(markets, cdl, ml, mult)
        v = report(t, s, ml, since, until, len(markets), full=True)
        summary.append((ml, len(t), avg([x["profit"] for x in t]) if t else float("nan"), v))
    print("\nSUMMARY")
    for ml, n, a, v in summary:
        a_txt = f"{a:+.2f}c/contract" if n else "-"
        print(f"   {ml:2d} min before close: {n:5d} trades, avg {a_txt} -> {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
