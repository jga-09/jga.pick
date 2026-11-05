"""Test the "15-minute strategy guide" approaches on real Kalshi history (data/replay_cache).

    python scripts/guide_test.py                         # BTC, ETH, SOL, XRP, DOGE, HYPE
    python scripts/guide_test.py --assets BTC --slippage 2
    python scripts/guide_test.py --assets HYPE,DOGE --before 2026-10-05   # fresh test on OLDER, unseen markets

Approaches (rules fixed BEFORE looking at results; one trade per market, paying the ask + Kalshi's rounded-up
taker fee + slippage, held to settlement):

  A  Fair value / distance-to-target: P(finish >= strike) from the spot price's distance to the strike, the time
     left (minus the 60-second settlement average) and the last 60 minutes' realized volatility. Buy the first side
     whose edge after fees >= EDGE cents.  Variants: any time (1.5-14 min left) vs MID-WINDOW only (5-11 min left
     = minutes 4-10), EDGE 3c vs 6c.
  (B late favourite and D mean reversion were tested on 22,000 markets, lost after fees, and were dropped.)
  (C, final-minute locked average, needs second-by-second data and speed - not testable on 1-minute candles.)

Each line shows trades, win rate, average P&L per trade (cents, after fees) with a 95% bootstrap range, on the
earlier days AND on the untouched last 7 days. A rule only counts if BOTH are positive and the ranges stay above 0.
Spot = Coinbase 1-minute closes (Kalshi settles on CF Benchmarks, so expect small differences). Read-only.
"""

from __future__ import annotations

import argparse
import math
import random
import sys
from bisect import bisect_right
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import NormalDist

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.research.model_data import load_crypto_bars, load_markets
from app.trading.fees import estimate_fee

ROOT = Path(__file__).resolve().parents[1]
ND = NormalDist()
AVG_WINDOW = 60  # Kalshi settles on a 60-second average


def fee_c(price: float) -> float:
    return estimate_fee(1, price, 0.07) * 100


def boot(xs: list[float], n: int = 2000) -> tuple[float, float] | None:
    if len(xs) < 10:
        return None
    rng = random.Random(7)
    ms = sorted(sum(rng.choices(xs, k=len(xs))) / len(xs) for _ in range(n))
    return ms[int(0.025 * n)], ms[int(0.975 * n) - 1]


class Spot:
    """Coinbase 1-minute closes with O(1) 60-minute realized volatility."""

    def __init__(self, bars: list) -> None:
        self.ends = [b.end for b in bars]
        self.close = [b.close for b in bars]
        r2 = [0.0]
        for i in range(1, len(bars)):
            a, b = self.close[i - 1], self.close[i]
            r = math.log(b / a) if a > 0 and b > 0 else 0.0
            r2.append(r2[-1] + r * r)
        self.r2 = r2

    def at(self, t: datetime) -> int | None:
        i = bisect_right(self.ends, t.timestamp()) - 1
        return i if i >= 60 and t.timestamp() - self.ends[i] <= 120 else None

    def sigma_per_sec(self, i: int) -> float:
        return math.sqrt((self.r2[i] - self.r2[i - 60]) / 60) / math.sqrt(60)

    def ret(self, i: int, n: int) -> float:
        return math.log(self.close[i] / self.close[i - n])


def p_yes(spot: float, strike: float, sigma: float, t_left: float) -> float | None:
    if t_left < AVG_WINDOW or sigma <= 0 or spot <= 0 or strike <= 0:
        return None
    h = (t_left - AVG_WINDOW) + AVG_WINDOW / 3
    return min(0.999, max(0.001, ND.cdf(math.log(spot / strike) / (sigma * math.sqrt(h)))))


def quotes(m: dict) -> dict[datetime, tuple[float, float]]:
    out = {}
    for c in m["candles"]:
        if c[1] is not None and c[2] is not None and 0 < c[1] < c[2] < 100 and c[2] - c[1] <= 5:
            out[c[0]] = (c[1], c[2])
    return out


def pnl(side: str, yes_won: bool, price: float, slip: float) -> float:
    won = yes_won if side == "yes" else not yes_won
    return (100 if won else 0) - price - fee_c(price) - slip


def rule_a(m, q, sp, lo_min, hi_min, edge, slip):
    for k in range(hi_min, lo_min - 1, -1):  # earliest allowed minute first
        at = m["close"] - timedelta(minutes=k)
        if at not in q or (i := sp.at(at)) is None:
            continue
        p = p_yes(sp.close[i], float(m["strike"]), sp.sigma_per_sec(i), k * 60)
        if p is None:
            continue
        bid, ask = q[at]
        for side, price, pw in (("yes", ask, p), ("no", 100 - bid, 1 - p)):
            if 5 <= price <= 95 and pw * 100 - price - fee_c(price) - slip >= edge:
                return side, price
    return None


CHOSEN = "A fair value, MID-window, edge>=6c"  # the rule the bot forward-tests (FAIR_VALUE_PURE)


PRICE_MOVES: dict[str, dict[str, float]] = {}  # asset -> month -> % change of the coin (Coinbase)


def run(asset: str, cache: Path, slip: float, holdout_days: int, totals: dict, before: datetime | None = None,
        chosen: dict | None = None) -> None:
    bars = load_crypto_bars(cache, asset)
    first_last: dict[str, list[float]] = {}
    for b in bars:
        if before is None or b.end < before.timestamp():
            fl = first_last.setdefault(f"{datetime.fromtimestamp(b.end, UTC):%Y-%m}", [b.close, b.close])
            fl[1] = b.close
    PRICE_MOVES[asset] = {k: v[1] / v[0] - 1 for k, v in first_last.items() if v[0] > 0}
    markets = [m for m in load_markets(cache, f"KX{asset}15M-") if m["result"] in ("yes", "no") and m["strike"]]
    if before is not None:
        markets = [m for m in markets if m["close"] < before]  # only markets the rule was NOT chosen on
    if not bars or not markets:
        hint = "--days 120" if before else "--days 30"
        print(f"\n{asset}: no data (run: python scripts/replay.py {hint} --no-trades --assets {asset})")
        return
    sp = Spot(bars)
    hold_from = max(m["close"] for m in markets) - timedelta(days=holdout_days)
    rules = {
        "A fair value, any time, edge>=3c": lambda m, q: rule_a(m, q, sp, 2, 14, 3, slip),
        "A fair value, any time, edge>=6c": lambda m, q: rule_a(m, q, sp, 2, 14, 6, slip),
        "A fair value, MID-window, edge>=3c": lambda m, q: rule_a(m, q, sp, 5, 11, 3, slip),
        "A fair value, MID-window, edge>=6c": lambda m, q: rule_a(m, q, sp, 5, 11, 6, slip),
    }
    span = f"{min(m['close'] for m in markets):%Y-%m-%d} .. {max(m['close'] for m in markets):%Y-%m-%d}"
    print(f"\n=== {asset} · {len(markets)} settled markets {span} · slippage {slip:g}c ===")
    for name, rule in rules.items():
        dev, hold = [], []
        for m in markets:
            q = quotes(m)
            pick = rule(m, q)
            if pick is None:
                continue
            side, price = pick
            r = pnl(side, m["result"] == "yes", price, slip)
            (hold if m["close"] >= hold_from else dev).append((r, r > 0))
            totals.setdefault(name, ([], []))[0 if m["close"] < hold_from else 1].append(r)
            if name == CHOSEN and chosen is not None:
                chosen.setdefault(asset, []).append((m["close"], r, side))
        print(f"  {name}")
        for label, xs in (("earlier", dev), ("last 7d", hold)):
            print("    " + line(label, [x[0] for x in xs]))
        if before is not None:
            print("    " + line("all", [x[0] for x in dev + hold]))


def line(label: str, ps: list[float]) -> str:
    if not ps:
        return f"{label:<8} 0 trades"
    ci = boot(ps)
    rng = f" ({ci[0]:+.1f}..{ci[1]:+.1f}c)" if ci else ""
    wr = sum(p > 0 for p in ps) / len(ps)
    return f"{label:<8} {len(ps):5} trades  win {wr:5.1%}  avg {sum(ps) / len(ps):+5.1f}c/trade{rng}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--assets", default="BTC,ETH,SOL,XRP,DOGE,HYPE")
    ap.add_argument("--cache", default=str(ROOT / "data" / "replay_cache"))
    ap.add_argument("--slippage", type=float, default=1.0)
    ap.add_argument("--holdout-days", type=int, default=7)
    ap.add_argument("--before", help="YYYY-MM-DD: only markets closing before this date - the period the rule was "
                                     "NOT chosen on (fresh out-of-sample test; HYPE/DOGE were chosen on 2026-10-05..)")
    a = ap.parse_args()
    before = datetime.fromisoformat(a.before).replace(tzinfo=UTC) if a.before else None
    totals: dict[str, tuple[list[float], list[float]]] = {}
    chosen: dict[str, list[tuple[datetime, float]]] = {}
    for asset in [x.strip().upper() for x in a.assets.split(",") if x.strip()]:
        run(asset, Path(a.cache), a.slippage, a.holdout_days, totals, before, chosen)
    if before is not None and chosen:
        print(f"\n=== FRESH-PERIOD VERDICT for the bot's rule ({CHOSEN}), markets before {a.before} ===")
        print("    same rules as scripts/forward_report.py: PASS = 150+ trades and the whole 95% range above 0")
        for asset, rs in chosen.items():
            ps = [r for _, r, _ in rs]
            ci = boot(ps)
            ok = len(ps) >= 150 and ci is not None and ci[0] > 0
            bad = ci is not None and ci[1] < 0
            word = "✅ PASS" if ok else "❌ FAIL" if bad else "⏳ INCONCLUSIVE"
            print(f"  {asset:<5} {line('all', ps)}  {word}")
            months: dict[str, list[float]] = {}
            for when, r, _ in rs:
                months.setdefault(f"{when:%Y-%m}", []).append(r)
            print("        by month: " + " · ".join(f"{k} {sum(v) / len(v):+.1f}c (n={len(v)})"
                                                    for k, v in sorted(months.items())))
            for side in ("yes", "no"):
                sp_ = [r for _, r, sd in rs if sd == side]
                print(f"        {'YES (UP) ' if side == 'yes' else 'NO (DOWN)'} {line('', sp_)}")
                bym: dict[str, list[float]] = {}
                for when, r, sd in rs:
                    if sd == side:
                        bym.setdefault(f"{when:%Y-%m}", []).append(r)
                print("          by month: " + " · ".join(f"{k} {sum(v) / len(v):+.1f}c (n={len(v)})"
                                                      for k, v in sorted(bym.items())))
            moves = PRICE_MOVES.get(asset, {})
            if moves:
                print("        coin price change by month: " + " · ".join(
                    f"{k} {v:+.0%}" for k, v in sorted(moves.items())))
                print("        (if NO wins only in falling months and YES only in rising ones, it is trend, not an edge)")
    if totals:
        print("\n=== ALL COINS ===")
        for name, (dev, hold) in totals.items():
            print(f"  {name}\n    {line('earlier', dev)}\n    {line('last 7d', hold)}")
    print("\nA rule is worth paper-testing in the bot only if 'earlier' AND 'last 7d' are both positive with ranges\n"
          f"above zero. (generated {datetime.now(UTC):%Y-%m-%d %H:%M} UTC · research only, nothing traded)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
