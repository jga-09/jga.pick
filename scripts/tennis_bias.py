"""Kalshi-only tennis price-bias test (no outside odds needed).

    python scripts/tennis_bias.py
    python scripts/tennis_bias.py --cache data/tennis_cache --slippage 1

Reads the Kalshi tennis markets already downloaded into data/tennis_cache/markets
by scripts/tennis_download.py. Research only: never touches the bot or Kalshi.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.research.tennis_bias import build_matches, load_records, run

ROOT = Path(__file__).resolve().parents[1]


def fmt(s: dict) -> str:
    if not s["trades"]:
        return "0 trades"
    ci = f"  95% {s['ev_95'][0] * 100:+.1f} to {s['ev_95'][1] * 100:+.1f}c" if s["ev_95"] else ""
    return (f"{s['trades']:4d} trades  WR {s['win_rate']:.1%}  avg price {s['avg_price']:.0f}c  "
            f"EV {s['ev_c']:+.2f}c{ci}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", default=str(ROOT / "data" / "tennis_cache"))
    ap.add_argument("--fee-rate", type=float, default=0.07)
    ap.add_argument("--slippage", type=float, default=1.0)
    a = ap.parse_args()
    mdir = Path(a.cache) / "markets"
    if not mdir.exists():
        print(f"No tennis data in {mdir}. Run first:  python scripts/tennis_download.py")
        return 1
    print("Loading Kalshi tennis markets...", flush=True)
    matches, skipped = build_matches(load_records(mdir))
    print(f"  {len(matches):,} matches with a pre-match quote")
    for k, v in skipped.items():
        print(f"  skipped {v:,}: {k}")
    try:
        r = run(matches, a.fee_rate, a.slippage)
    except ValueError as exc:
        print(f"Not enough data: {exc}.")
        return 1
    print(f"  ATP {r['by_tour']['atp']:,} · WTA {r['by_tour']['wta']:,}")
    print(f"  development {r['dev'][0]} .. {r['dev'][1]} ({r['n_dev']:,} matches) · "
          f"holdout {r['holdout'][0]} .. {r['holdout'][1]} ({r['n_hold']:,} matches, untouched)")

    print("\n1) ARE KALSHI'S PRICES RIGHT?  (development days; favourite = higher-priced player)")
    print("   fav price  matches  avg price  fav won   95% range      gap   costs")
    for c in r["calibration_dev"]:
        lo, hi = c["ci"]
        print(f"   {c['range']:>9} {c['n']:8,} {c['avg_mid']:9.1f}c {c['fav_win']:8.1%}  {lo:5.1%}-{hi:5.1%} "
              f"{c['gap_c']:+6.1f}c {c['cost_c']:5.1f}c")
    print("   gap = how much more (+) or less (-) the favourite won than its price implied.")
    print("   A rule can only profit where |gap| is bigger than costs - consistently.")

    print(f"\n2) EVERY RULE  (one trade per match, costs = fee + {a.slippage:.0f}c slippage)")
    for rule, d, h in r["table"]:
        print(f"   {rule.name}")
        print(f"       dev:     {fmt(d)}")
        print(f"       holdout: {fmt(h)}")
    print(f"   ({len(r['table'])} rules tested: one or two looking good on holdout by chance is expected.)")

    print("\n3) THE HONEST TEST  (rule picked on development days only, judged once on the holdout)")
    if r["pick"]:
        rule, d, h = r["pick"]
        print(f"   picked: {rule.name}")
        print(f"   dev:     {fmt(d)}")
        print(f"   holdout: {fmt(h)}")
    else:
        print("   picked: nothing (no rule was profitable on development days)")
    print(f"\nVERDICT: {r['verdict']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
