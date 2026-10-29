"""TradingView-style VWAP + volume test on downloaded Kalshi BTC 15-minute history.

Recreates the chart setup:
  * Volume bars (Coinbase 1-minute volume),
  * VWAP anchored to the session (resets 00:00 UTC, as TradingView does for crypto),
    source = (high + low + close) / 3, bands at 0.5 and 1 standard deviation,
  * the 15-minute open line = Kalshi's strike (floor_strike).

and tests simple rules on real settled markets, buying at Kalshi's ask with the fee.
Research only: reads files already downloaded by scripts/replay.py, never places orders,
needs no network. Standard library only.

    python3 scripts/vwap_test.py
    python3 scripts/vwap_test.py --asset BTC --holdout-days 7

RULES (fixed before seeing any results). At the decision minute (3, 6 or 10 min before
close), using only data up to that minute:
  BASE    buy the side the price is on (above strike -> YES, below -> NO)
  VWAP    BASE, only if price is on the same side of VWAP
  BAND1   VWAP, and price beyond the 0.5-sd band in that direction
  VOL     VWAP, and last-5-minute volume >= 1.5x the hour's average 5-minute volume
  FADE    price beyond the 1-sd band: buy the side AGAINST the stretch (mean reversion)
Entry at the ask (YES ask, or NO ask = 100 - YES bid); skipped if the bid is 0/missing,
the spread is over 5c, or the price is outside 3-97c. One trade per market per rule.
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import random
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MINUTES = (3, 6, 10)
MAX_SPREAD = 5.0
RULES = ("BASE", "VWAP", "BAND1", "VOL", "FADE")
MIN_TRADES = 30


# ------------------------------------------------------------------ data
@dataclass(frozen=True)
class Bar:
    end: int  # unix seconds at the END of the minute (only known from then on)
    high: float
    low: float
    close: float
    volume: float


def load_spot(cache: Path, asset: str) -> list[Bar]:
    by_end: dict[int, Bar] = {}
    for f in sorted(cache.glob(f"spot_{asset}_*.json.gz")):
        try:
            rows = json.loads(gzip.decompress(f.read_bytes()))
        except (OSError, ValueError):
            continue
        for r in rows:  # Coinbase: [start time, low, high, open, close, volume]
            if isinstance(r, list) and len(r) >= 6:
                by_end[int(r[0]) + 60] = Bar(int(r[0]) + 60, float(r[2]), float(r[1]), float(r[4]), float(r[5]))
    return [by_end[k] for k in sorted(by_end)]


def parse_time(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC)


def load_markets(cache: Path, asset: str) -> list[dict]:
    out = []
    for f in sorted(cache.glob(f"KX{asset}15M-*.json.gz")):
        try:
            rec = json.loads(gzip.decompress(f.read_bytes()))
        except (OSError, ValueError):
            continue
        m = rec.get("market") or {}
        if rec.get("v") != 2 or m.get("result") not in ("yes", "no") or not m.get("close_time"):
            continue
        out.append({"ticker": m["ticker"], "close": int(parse_time(m["close_time"]).timestamp()),
                    "strike": m.get("floor_strike"), "result": m["result"],
                    "candles": {int(c[0]): (c[1], c[2]) for c in rec.get("candles") or []}})
    return sorted(out, key=lambda m: m["close"])


# ------------------------------------------------------------------ indicators
@dataclass(frozen=True)
class Ind:
    price: float
    vwap: float
    sd: float
    vol5: float
    vol5_avg: float


def indicators(bars: list[Bar], ends: list[int], at: int) -> Ind | None:
    """VWAP/bands since 00:00 UTC and volume, from bars that ENDED at or before ``at``."""
    i = bisect_right(ends, at)
    if i == 0 or at - bars[i - 1].end > 120:
        return None  # no fresh price
    day0 = at - at % 86400
    pv = pv2 = v = 0.0
    j = i - 1
    while j >= 0 and bars[j].end > day0:
        b = bars[j]
        tp = (b.high + b.low + b.close) / 3
        pv += tp * b.volume
        pv2 += tp * tp * b.volume
        v += b.volume
        j -= 1
    if v <= 0 or i - 1 - j < 15:
        return None  # session too young for a meaningful VWAP
    vwap = pv / v
    sd = math.sqrt(max(0.0, pv2 / v - vwap * vwap))
    recent = [b.volume for b in bars[max(0, i - 60):i] if b.end > at - 3600]
    vol5 = sum(b.volume for b in bars[max(0, i - 5):i] if b.end > at - 300)
    vol5_avg = sum(recent) / len(recent) * 5 if recent else 0.0
    return Ind(bars[i - 1].close, vwap, sd, vol5, vol5_avg)


def decide(rule: str, ind: Ind, strike: float) -> str | None:
    """Side to buy ('yes' = finishes above the strike) or None."""
    if rule == "FADE":
        if ind.sd <= 0:
            return None
        z = (ind.price - ind.vwap) / ind.sd
        return "no" if z >= 1 else "yes" if z <= -1 else None
    if ind.price == strike:
        return None
    up = ind.price > strike
    if rule == "BASE":
        return "yes" if up else "no"
    if (ind.price > ind.vwap) != up:
        return None
    if rule == "VWAP":
        return "yes" if up else "no"
    if rule == "BAND1":
        beyond = ind.price >= ind.vwap + 0.5 * ind.sd if up else ind.price <= ind.vwap - 0.5 * ind.sd
        return ("yes" if up else "no") if beyond else None
    if rule == "VOL":
        return ("yes" if up else "no") if ind.vol5_avg > 0 and ind.vol5 >= 1.5 * ind.vol5_avg else None
    raise ValueError(rule)


def fee_cents(p: float) -> int:
    return math.ceil(round(0.07 * p * (100 - p) / 100, 9))


# ------------------------------------------------------------------ simulation
def simulate(markets, bars, minutes_left: int):
    ends = [b.end for b in bars]
    trades = {r: [] for r in RULES}
    skipped = Counter()
    for m in markets:
        at = m["close"] - minutes_left * 60
        if not isinstance(m["strike"], (int, float)):
            skipped["no strike"] += 1
            continue
        q = m["candles"].get(at)
        if q is None:
            skipped["no Kalshi candle at the decision minute"] += 1
            continue
        bid, ask = q
        if not bid or ask is None or ask >= 100 or ask - bid > MAX_SPREAD:
            skipped["empty or wide Kalshi book"] += 1
            continue
        ind = indicators(bars, ends, at)
        if ind is None:
            skipped["no Coinbase price/volume at the decision minute"] += 1
            continue
        for r in RULES:
            side = decide(r, ind, float(m["strike"]))
            if side is None:
                continue
            price = ask if side == "yes" else 100 - bid
            if not 3 <= price <= 97:
                continue
            fee = fee_cents(price)
            won = m["result"] == side
            trades[r].append({"close": m["close"], "ticker": m["ticker"], "side": side, "price": price, "fee": fee,
                              "won": won, "profit": (100 - price - fee) if won else (-price - fee)})
    return trades, skipped


def boot(xs, n=3000, seed=7):
    if len(xs) < 10:
        return None
    rng = random.Random(seed)
    ms = sorted(sum(rng.choices(xs, k=len(xs))) / len(xs) for _ in range(n))
    return ms[int(0.025 * n)], ms[int(0.975 * n) - 1]


def line(ts) -> str:
    if not ts:
        return "   0 trades"
    p = [t["profit"] for t in ts]
    wr = sum(t["won"] for t in ts) / len(ts)
    price = sum(t["price"] for t in ts) / len(ts)
    be = (price + sum(t["fee"] for t in ts) / len(ts)) / 100
    ci = boot(p)
    ci_s = f"  95% {ci[0]:+.1f}..{ci[1]:+.1f}c" if ci else ""
    return (f"{len(ts):5d} trades  WR {wr:5.1%} (break-even {be:5.1%})  price {price:4.1f}c  "
            f"avg {sum(p) / len(p):+6.2f}c{ci_s}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--asset", default="BTC")
    ap.add_argument("--cache", default=str(ROOT / "data" / "replay_cache"))
    ap.add_argument("--holdout-days", type=int, default=7)
    a = ap.parse_args()
    cache = Path(a.cache)
    markets, bars = load_markets(cache, a.asset), load_spot(cache, a.asset)
    if not markets or not bars:
        print(f"Need downloaded history in {cache} (markets: {len(markets)}, Coinbase minutes: {len(bars)}).\n"
              f"Run first:  python scripts/replay.py --days 14 --assets {a.asset}")
        return 1
    first, last = datetime.fromtimestamp(markets[0]["close"], UTC), datetime.fromtimestamp(markets[-1]["close"], UTC)
    hold_from = int((last - timedelta(days=a.holdout_days)).timestamp())
    print(f"VWAP + VOLUME TEST · {a.asset} · {len(markets):,} settled markets {first:%Y-%m-%d} .. {last:%Y-%m-%d} · "
          f"{len(bars):,} Coinbase minutes · research only, no orders")
    print(f"Holdout (judged, never looked at while writing the rules): last {a.holdout_days} days, "
          f"from {datetime.fromtimestamp(hold_from, UTC):%Y-%m-%d %H:%M} UTC")
    verdicts = []
    for ml in MINUTES:
        trades, skipped = simulate(markets, bars, ml)
        print(f"\n=== {ml} minutes before close ===")
        for why, n in skipped.most_common():
            print(f"   skipped {n:5d}: {why}")
        for r in RULES:
            ts = trades[r]
            dev = [t for t in ts if t["close"] < hold_from]
            hold = [t for t in ts if t["close"] >= hold_from]
            print(f"  {r:5}  earlier: {line(dev)}")
            print(f"         holdout: {line(hold)}")
            hp = [t["profit"] for t in hold]
            dp = [t["profit"] for t in dev]
            ci = boot(hp)
            ok = (len(hold) >= MIN_TRADES and len(dev) >= MIN_TRADES and sum(dp) > 0 and sum(hp) > 0
                  and ci is not None and ci[0] > 0)
            verdicts.append((ml, r, ok, len(hold), sum(hp) / len(hp) if hp else None))
    print("\nWHAT TO READ: compare each rule's win rate with its break-even. BASE is the plain")
    print("'which side of the strike' signal Kalshi's price already reflects; VWAP/BAND1/VOL only add")
    print("value if they beat BASE AND their own break-even, on BOTH the earlier days and the holdout.")
    print(f"({len(RULES) * len(MINUTES)} rule/time combinations are tested, so one may look good by luck.)")
    passed = [v for v in verdicts if v[2]]
    print("\nVERDICT")
    if passed:
        base = {ml: avg for ml, r, _, _, avg in verdicts if r == "BASE"}
        for ml, r, _, n, avg in passed:
            note = ""
            if r in ("VWAP", "BAND1", "VOL") and base.get(ml) is not None and avg <= base[ml]:
                note = " (no better than BASE: the edge is price-vs-strike, VWAP/volume add nothing)"
            print(f"   🟡 {r} at {ml} min: positive on earlier AND holdout days, holdout 95% range above zero "
                  f"({n} trades, {avg:+.2f}c){note}. Worth a live PAPER test - not proof yet.")
    else:
        print("   ❌ No rule made money after the fee on both the earlier days and the untouched holdout.")
        print("   The VWAP/volume information is already in Kalshi's prices (or too weak to beat the fee).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
