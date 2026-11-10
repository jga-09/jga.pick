"""Does cashing out early beat holding to settlement? Tested on real Kalshi history.

    python scripts/exit_test.py                          # all cached coins
    python scripts/exit_test.py --assets HYPE --before 2026-10-05

Entries: the bot's fair-value rule (mid-window, edge >= 6c after fees), one per market, paying the ask + fee +
slippage. Then the SAME trades are managed by each exit rule, fixed before looking at results, checked once a
minute until 1 minute before close. A cash-out sells at the bid minus slippage and pays Kalshi's exit fee.

  HOLD          hold to settlement (what the bot does now)
  TP +10/15/20  sell when the bid is 10/15/20 cents above the entry price
  TP 85/90      sell when the bid reaches 85 / 90 cents
  STOP 50%      sell when the bid falls to half the entry price
  MODEL <40%    sell when the fair-value model's own odds for our side fall below 40% (the bot's EARLY_EXIT)
  TP+15 & STOP  both

For each rule: average P&L per trade after all fees (95% range), how often it sold early, and how many of those
early sells would have WON if held (profit given up). Read-only, research only.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.research.model_data import load_crypto_bars, load_markets  # noqa: E402

_spec = importlib.util.spec_from_file_location("guide_test", ROOT / "scripts" / "guide_test.py")
G = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(G)  # type: ignore[union-attr]


def entry(m, q, sp, edge, slip):
    """(minute_left, side, price paid) for the bot's rule, or None."""
    for k in range(11, 4, -1):
        at = m["close"] - timedelta(minutes=k)
        if at not in q or (i := sp.at(at)) is None:
            continue
        p = G.p_yes(sp.close[i], float(m["strike"]), sp.sigma_per_sec(i), k * 60)
        if p is None:
            continue
        bid, ask = q[at]
        for side, price, pw in (("yes", ask, p), ("no", 100 - bid, 1 - p)):
            if 5 <= price <= 95 and pw * 100 - price - G.fee_c(price) - slip >= edge:
                return k, side, price + slip
    return None


def bid_for(c, side):
    yb, ya = c[1], c[2]
    if side == "yes":
        return yb if yb is not None and 0 < yb < 100 else None
    return 100 - ya if ya is not None and 0 < ya < 100 else None


RULES = ["HOLD", "TP +10", "TP +15", "TP +20", "TP 85", "TP 90", "STOP 50%", "MODEL <40%", "TP+15 & STOP"]


def should_sell(rule, bid, paid, p_side):
    tp = {"TP +10": bid >= paid + 10, "TP +15": bid >= paid + 15, "TP +20": bid >= paid + 20,
          "TP 85": bid >= 85, "TP 90": bid >= 90}
    if rule in tp:
        return tp[rule]
    if rule == "STOP 50%":
        return bid <= paid * 0.5
    if rule == "MODEL <40%":
        return p_side is not None and p_side < 0.40
    if rule == "TP+15 & STOP":
        return bid >= paid + 15 or bid <= paid * 0.5
    return False


def run(assets, cache, edge, slip, before, after):
    res = {r: [] for r in RULES}
    early = {r: [0, 0] for r in RULES}  # [sold early, of which would have won]
    for asset in assets:
        bars = load_crypto_bars(cache, asset)
        markets = [m for m in load_markets(cache, f"KX{asset}15M-") if m["result"] in ("yes", "no") and m["strike"]]
        if before:
            markets = [m for m in markets if m["close"] < before]
        if after:
            markets = [m for m in markets if m["close"] >= after]
        if not bars or not markets:
            continue
        sp = G.Spot(bars)
        for m in markets:
            e = entry(m, G.quotes(m), sp, edge, slip)
            if e is None:
                continue
            k0, side, paid = e
            won = m["result"] == side
            hold = (100 if won else 0) - paid - G.fee_c(paid - slip)
            candles = {c[0]: c for c in m["candles"]}
            for rule in RULES:
                out = hold
                for k in range(k0 - 1, 0, -1):
                    at = m["close"] - timedelta(minutes=k)
                    c = candles.get(at)
                    if c is None:
                        continue
                    bid = bid_for(c, side)
                    if bid is None:
                        continue
                    p_side = None
                    if rule == "MODEL <40%" and (i := sp.at(at)) is not None:
                        py = G.p_yes(sp.close[i], float(m["strike"]), sp.sigma_per_sec(i), k * 60)
                        p_side = None if py is None else (py if side == "yes" else 1 - py)
                    if should_sell(rule, bid, paid, p_side):
                        sell = max(1.0, bid - slip)
                        out = sell - paid - G.fee_c(paid - slip) - G.fee_c(sell)
                        early[rule][0] += 1
                        early[rule][1] += won
                        break
                res[rule].append(out)
    return res, early


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--assets", default="BTC,ETH,SOL,XRP,HYPE,DOGE")
    ap.add_argument("--cache", default=str(ROOT / "data" / "replay_cache"))
    ap.add_argument("--edge", type=float, default=6.0)
    ap.add_argument("--slippage", type=float, default=1.0)
    ap.add_argument("--before")
    ap.add_argument("--after")
    a = ap.parse_args()
    before = datetime.fromisoformat(a.before).replace(tzinfo=UTC) if a.before else None
    after = datetime.fromisoformat(a.after).replace(tzinfo=UTC) if a.after else None
    assets = [x.strip().upper() for x in a.assets.split(",") if x.strip()]
    for group in ([*assets], *([[x] for x in assets] if len(assets) > 1 else [])):
        res, early = run(group, Path(a.cache), a.edge, a.slippage, before, after)
        n = len(res["HOLD"])
        if not n:
            continue
        print(f"\n=== {','.join(group)} · {n} trades ===")
        base = sum(res["HOLD"]) / n
        for rule in RULES:
            xs = res[rule]
            avg = sum(xs) / n
            ci = G.boot(xs)
            rng = f"({ci[0]:+.1f}..{ci[1]:+.1f}c)" if ci else ""
            sold, wouldwin = early[rule]
            extra = "" if rule == "HOLD" else (f"  vs HOLD {avg - base:+.1f}c · sold early {sold / n:4.0%}"
                                               f" (of those {wouldwin / sold if sold else 0:4.0%} would have won)")
            print(f"  {rule:<13} avg {avg:+5.1f}c/trade {rng:<16}{extra}")
    print("\nA cash-out rule is only worth switching on if it beats HOLD on the coin you trade, on more than one\n"
          "period (run with --before 2026-10-05 too). Selling early pays a 2nd fee + the spread every time.\n"
          f"(generated {datetime.now(UTC):%Y-%m-%d %H:%M} UTC · research only, nothing traded)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
