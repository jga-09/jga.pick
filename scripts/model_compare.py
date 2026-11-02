"""Compare prediction models per asset on real settled Kalshi markets (research only, never trades).

    pip install -r requirements-research.txt        # numpy + scikit-learn (+ lightgbm, optional)
    python scripts/model_compare.py                  # BTC, ETH, SOL from data/replay_cache
    python scripts/model_compare.py --assets BTC,GOLD --holdout-days 7 --min-ev 2

Needs history first:  python scripts/replay.py --days 30 --no-trades   (and scripts/gold_check.py --days 30)
Each asset is modelled on its OWN data. Walk-forward on development days, then one look at the untouched last
``--holdout-days``. A model is only interesting if it beats Kalshi's own price (MARKET) on probability accuracy
AND makes money after the exact fee on both the walk-forward and the holdout.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.research.model_data import (
    load_crypto_bars,
    load_gold_bars,
    load_markets,
)
from app.research.modeling import (
    MODEL_NAMES,
    _has,
    build_rows,
    evaluate_asset,
    verdict,
)
from app.research.ta import compute

ROOT = Path(__file__).resolve().parents[1]


def pct(x):
    return "   -  " if x is None else f"{x:6.1%}"


def cents(x):
    return "    -  " if x is None else f"{x * 100:+6.2f}c"


def show(res) -> list[tuple[str, str, str]]:
    print(f"\n{'=' * 100}\n{res.asset}: {res.n_markets:,} markets {res.days[0]} .. {res.days[1]} · "
          f"holdout from {res.holdout_from or '-'} · features: {len(res.feats)}")
    if res.note:
        print(f"  {res.note}")
        return [(res.asset, "-", res.note)]
    print("  PROBABILITY ACCURACY (lower is better; MARKET = Kalshi's own price)")
    print("  model        part       markets  Brier   (market)  logloss   ECE")
    for part, d in (("walk-fwd", res.dev), ("holdout", res.holdout)):
        for name, r in d.items():
            p = r["prob"]
            if not p.get("n_markets"):
                continue
            flag = " <- beats market" if p["brier"] < p["brier_market"] else ""
            print(f"  {name:<11} {part:<9} {p['n_markets']:8,}  {p['brier']:.4f}  ({p['brier_market']:.4f})  "
                  f"{p['logloss']:.4f}  {p['ece']:.3f}{flag}")
    print("\n  TRADING (one trade/market, ask + exact fee + slippage)")
    print("  model        part      trades   WR     P&L $    ROI   PF     maxDD$  avg/trade  95% range")
    verdicts = []
    for name in res.holdout:
        for part, d in (("walk-fwd", res.dev), ("holdout", res.holdout)):
            t = d.get(name, {}).get("trades")
            if not t:
                continue
            ci = t["ev_ci"]
            ci_s = f"{ci[0] * 100:+.1f}..{ci[1] * 100:+.1f}c" if ci else "-"
            pf = "inf" if t["profit_factor"] == float("inf") else f"{t['profit_factor']:.2f}" if t["profit_factor"] else "-"
            print(f"  {name:<11} {part:<9} {t['trades']:6}  {pct(t['win_rate'])} {t['pnl']:+8.2f} "
                  f"{pct(t['roi'])} {pf:>5} {t['max_dd']:7.2f}  {cents(t['avg'])}  {ci_s}")
        v = ("benchmark (Kalshi's own price; buying at the ask can never beat it)" if name == "MARKET"
             else verdict(res.dev.get(name), res.holdout.get(name)))
        verdicts.append((res.asset, name, v))
    best = max(res.holdout.items(), key=lambda kv: kv[1]["trades"]["pnl"] if kv[1]["trades"]["trades"] else -1e9)
    t = best[1]["trades"]
    if t["trades"]:
        print(f"\n  breakdown of {best[0]} on the holdout (n, avg $/trade):")
        for k in ("by_time", "by_price", "by_regime"):
            print("   ", k, {a: f"{n}, {v:+.3f}" for a, (n, v) in t[k].items()})
    if res.calibration_method:
        print(f"  calibration used: {res.calibration_method}")
    return verdicts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--assets", default="BTC,ETH,SOL")
    ap.add_argument("--cache", default=str(ROOT / "data" / "replay_cache"))
    ap.add_argument("--gold-cache", default=str(ROOT / "data" / "gold_cache"))
    ap.add_argument("--holdout-days", type=int, default=7)
    ap.add_argument("--min-ev", type=float, default=2.0, help="cents per contract after fee+slippage (fixed)")
    ap.add_argument("--order-size", type=int, default=1, help="contracts per order (fee rounds up per order)")
    ap.add_argument("--slippage", type=float, default=1.0)
    ap.add_argument("--fee-rate", type=float, default=0.07)
    ap.add_argument("--no-calibration", action="store_true")
    ap.add_argument("--models", default=",".join(MODEL_NAMES))
    ap.add_argument("--json", default=str(ROOT / "data" / "model_compare.json"))
    a = ap.parse_args()
    if not _has("sklearn"):
        print("Note: scikit-learn not installed - only MARKET/FAIR/HIST run. pip install -r requirements-research.txt")
    all_verdicts, dump = [], {}
    for asset in [x.strip().upper() for x in a.assets.split(",") if x.strip()]:
        if asset == "GOLD":
            cache = Path(a.gold_cache)
            # Gold price = Coinbase PAXG proxy (scripts/gold_check.py --days N downloads it and measures its basis
            # vs Kalshi's Pyth strikes); falls back to any cached Pyth closes.
            bars = load_gold_bars(cache)  # exact Kalshi/Pyth settlement prices, when Kalshi provides them
            label = "Kalshi/Pyth settlement prices (exact)"
            if not bars:
                bars, label = load_crypto_bars(cache, "PAXG"), "Coinbase PAXG proxy (not the settlement source)"
            markets = load_markets(cache, "KXGOLD15M-")
            print(f"\nGOLD: price = {label if bars else 'none'}")
        else:
            cache = Path(a.cache)
            bars, markets = load_crypto_bars(cache, asset), load_markets(cache, f"KX{asset}15M-")
        if not bars or not markets:
            print(f"\n{asset}: no cached history ({len(markets)} markets, {len(bars)} price minutes) - skipped")
            all_verdicts.append((asset, "-", "NO DATA"))
            continue
        rows, skipped = build_rows(asset, markets, compute(bars))
        print(f"\n{asset}: {len(rows):,} decision minutes from {len(markets):,} markets; skipped {skipped}")
        res = evaluate_asset(asset, rows, holdout_days=a.holdout_days, min_ev=a.min_ev, fee_rate=a.fee_rate,
                             slippage=a.slippage, order_size=a.order_size, calibrate=not a.no_calibration,
                             model_names=[m.strip().upper() for m in a.models.split(",")])
        all_verdicts += show(res)
        dump[asset] = {"dev": res.dev, "holdout": res.holdout, "note": res.note, "days": res.days,
                       "holdout_from": res.holdout_from, "features": res.feats}
    print(f"\n{'=' * 100}\nVERDICTS (holdout judged once; {len(all_verdicts)} model/asset pairs tested, so one "
          "'PASSES' can still be luck)")
    for asset, name, v in all_verdicts:
        print(f"  {asset:<5} {name:<10} {v}")
    Path(a.json).write_text(json.dumps(dump, default=str, indent=1))
    print(f"\nSaved {a.json} (the bot's /models screen reads it). Research only - nothing in the bot changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
