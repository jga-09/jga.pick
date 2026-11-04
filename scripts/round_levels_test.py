"""Do round-number "key levels" (BTC 75k/80k/85k/90k/100k...) give an edge on Kalshi 15-minute markets?

    python scripts/round_levels_test.py                    # BTC, ETH, SOL from data/replay_cache
    python scripts/round_levels_test.py --assets BTC --near-bps 20

Idea being tested (fixed before looking at results):
  * price just BELOW a key level  -> the level acts as resistance -> UP is less likely than Kalshi prices
                                     -> buy NO
  * price just ABOVE a key level  -> support -> DOWN is less likely  -> buy YES
For each market, at a fixed decision time (minutes before close), using only the Coinbase price known then.
Reports, per condition: how often YES won vs what Kalshi's price implied (the "excess", with a 95% range),
and the paper P&L of the rule paying the ask + Kalshi's rounded-up fee + 1c slippage, on the earlier days
AND on the untouched last 7 days. Read-only; never trades.
"""

from __future__ import annotations

import argparse
import math
import sys
from bisect import bisect_right
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.research.model_data import load_crypto_bars, load_markets
from app.research.stats import wilson
from app.trading.fees import estimate_fee

ROOT = Path(__file__).resolve().parents[1]
# (minor step, major step) in dollars: "key levels" = multiples of these
LEVELS = {"BTC": (1000, 5000), "ETH": (50, 250), "SOL": (5, 25), "XRP": (0.05, 0.25), "DOGE": (0.01, 0.05),
          "HYPE": (1, 5)}


def nearest(price: float, step: float) -> tuple[float, float]:
    below = math.floor(price / step) * step
    return below, below + step


def classify(price: float, step: float, near_bps: float) -> str | None:
    below, above = nearest(price, step)
    if (above - price) / price * 1e4 <= near_bps:
        return "below_level"  # resistance overhead
    if (price - below) / price * 1e4 <= near_bps:
        return "above_level"  # support underneath
    return None


def fee_c(price: float) -> float:
    return estimate_fee(1, price, 0.07) * 100


def boot(xs: list[float], n: int = 2000) -> tuple[float, float] | None:
    import random

    if len(xs) < 10:
        return None
    rng = random.Random(3)
    ms = sorted(sum(rng.choices(xs, k=len(xs))) / len(xs) for _ in range(n))
    return ms[int(0.025 * n)], ms[int(0.975 * n) - 1]


def run(asset: str, cache: Path, minutes: int, near_bps: float, holdout_days: int) -> None:
    bars = load_crypto_bars(cache, asset)
    markets = load_markets(cache, f"KX{asset}15M-")
    if not bars or not markets or asset not in LEVELS:
        print(f"\n{asset}: no data (run scripts/replay.py --days 30 --no-trades --assets {asset})")
        return
    ends = [b.end for b in bars]
    last = max(m["close"] for m in markets)
    hold_from = last - timedelta(days=holdout_days)
    print(f"\n=== {asset} · decision {minutes} min before close · 'near' = within {near_bps:g} bps of a level ===")
    for kind, step in zip(("minor", "major"), LEVELS[asset], strict=True):
        groups: dict[str, list[tuple[datetime, int, float, float, float]]] = {"below_level": [], "above_level": [],
                                                                            "not_near": []}
        for m in markets:
            if m["result"] not in ("yes", "no"):
                continue
            at = m["close"] - timedelta(minutes=minutes)
            q = next((c for c in m["candles"] if c[0] == at), None)
            if q is None or q[1] is None or q[2] is None or not 0 < q[1] < q[2] < 100 or q[2] - q[1] > 5:
                continue
            i = bisect_right(ends, at.timestamp()) - 1
            if i < 0 or at.timestamp() - ends[i] > 120:
                continue
            cls = classify(bars[i].close, step, near_bps) or "not_near"
            groups[cls].append((m["close"], int(m["result"] == "yes"), (q[1] + q[2]) / 200, q[2], 100 - q[1]))
        print(f"  {kind} levels (every ${step:g}):")
        for cls, rows in groups.items():
            if not rows:
                print(f"    {cls:<12} 0 markets")
                continue
            n = len(rows)
            yes = sum(r[1] for r in rows)
            mid = sum(r[2] for r in rows) / n
            lo, hi = wilson(yes, n)
            line = (f"    {cls:<12} {n:5} mkts  YES won {yes / n:5.1%} [{lo:4.0%}-{hi:4.0%}]  Kalshi implied {mid:5.1%}  "
                    f"excess {(yes / n - mid) * 100:+5.1f} pts")
            if cls != "not_near":
                side = "no" if cls == "below_level" else "yes"  # the key-level rule
                pnl_dev, pnl_hold = [], []
                for close, y, _, yes_ask, no_ask in rows:
                    price = no_ask if side == "no" else yes_ask
                    won = (y == 0) if side == "no" else (y == 1)
                    p = ((100 if won else 0) - price - fee_c(price) - 1) / 100
                    (pnl_hold if close >= hold_from else pnl_dev).append(p)
                for name, ps in (("earlier", pnl_dev), ("last 7d", pnl_hold)):
                    ci = boot(ps)
                    avg = f"{sum(ps) / len(ps) * 100:+.1f}c" if ps else "-"
                    rng = f" ({ci[0] * 100:+.1f}..{ci[1] * 100:+.1f}c)" if ci else ""
                    line += f"\n{'':18}rule: buy {side.upper()} · {name}: {len(ps)} trades, avg {avg}/trade{rng}"
            print(line)
    print("  excess = how much more (+) or less (-) often YES won than Kalshi's price said. The rule only works if\n"
          "  'below_level' has a clearly NEGATIVE excess and 'above_level' a clearly POSITIVE one, AND the rule's\n"
          "  average stays positive after fees on both the earlier days and the last 7 days.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--assets", default="BTC,ETH,SOL")
    ap.add_argument("--cache", default=str(ROOT / "data" / "replay_cache"))
    ap.add_argument("--minutes", type=int, default=10, help="decision time, minutes before close (default 10)")
    ap.add_argument("--near-bps", type=float, default=15.0, help="how close counts as 'near' (default 15 bps)")
    ap.add_argument("--holdout-days", type=int, default=7)
    a = ap.parse_args()
    for asset in [x.strip().upper() for x in a.assets.split(",") if x.strip()]:
        run(asset, Path(a.cache), a.minutes, a.near_bps, a.holdout_days)
    print(f"\n(generated {datetime.now(UTC):%Y-%m-%d %H:%M} UTC · research only, nothing in the bot changed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
