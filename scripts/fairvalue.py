"""Fair-value research on the replay data already downloaded by scripts/replay.py.

    python scripts/fairvalue.py
    python scripts/fairvalue.py --assets BTC,ETH --slippage 1

Prices every replayed minute with  fair P(YES) = Phi(ln(spot/strike) / (sigma*sqrt(T)))
(spot = Coinbase, sigma = volatility of the previous hour) and checks:
  1. basis      - does Coinbase at close agree with Kalshi's result?
  2. accuracy   - is the model more accurate (Brier) than Kalshi's own prices?
  3. strategy   - buy when Kalshi is cheaper than fair by more than costs + margin,
                  margin chosen on EARLIER days only, tested on the next day.
Research only: it never touches the bot, the database or Kalshi.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.research.fairvalue import (
    DEFAULT_MARGINS,
    FVPoint,
    basis_agreement,
    brier,
    build_points,
    calibration_table,
    fv_metrics,
    simulate_fv,
    walk_forward_fv,
)
from app.research.replay import _record_candles, parse_coinbase
from app.research.reporting import bootstrap_ev
from app.utils.time import parse_ts

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ("BTC", "ETH", "SOL", "XRP", "DOGE")


def asset_of(series: str) -> str | None:
    s = series.upper()
    return next((a for a in ASSETS if s.startswith(f"KX{a}")), None)


def load_spot(cache: Path, asset: str) -> list:
    pts = []
    for f in sorted(cache.glob(f"spot_{asset}_*.json.gz")):
        try:
            pts.extend(parse_coinbase(json.loads(gzip.decompress(f.read_bytes())), asset))
        except (OSError, ValueError) as exc:
            print(f"  skipping unreadable {f.name}: {type(exc).__name__}")
    dedup = {p.ts: p for p in pts}
    return [dedup[t] for t in sorted(dedup)]


def fmt_m(m) -> str:
    if m.trades == 0:
        return "0 trades"
    return (f"{m.trades} trades  WR {m.win_rate:.1%}  EV {m.ev * 100:+.2f}c/contract  "
            f"total {m.pnl:+.2f}$  avg entry {m.avg_entry:.0f}c  max DD {m.max_drawdown:.2f}$")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--assets", default="BTC,ETH,SOL")
    ap.add_argument("--cache", default=str(ROOT / "data" / "replay_cache"))
    ap.add_argument("--fee-rate", type=float, default=0.07)
    ap.add_argument("--slippage", type=float, default=1.0, help="extra cents paid per contract (default 1)")
    args = ap.parse_args()
    cache = Path(args.cache)
    wanted = {a.strip().upper() for a in args.assets.split(",") if a.strip()}
    files = [f for f in cache.glob("*.json.gz") if not f.name.startswith("spot_")] if cache.exists() else []
    if not files:
        print(f"No replay data in {cache}. Run first:  python scripts/replay.py --days 14")
        return

    spot: dict[str, list] = {}
    points: dict[str, list[FVPoint]] = {}
    basis_rows = []
    skipped: dict[str, int] = defaultdict(int)
    for f in sorted(files):
        try:
            rec = json.loads(gzip.decompress(f.read_bytes()))
        except (OSError, ValueError):
            skipped["unreadable file"] += 1
            continue
        m = rec.get("market") or {}
        asset = asset_of(str(rec.get("series", "")))
        if asset is None or asset not in wanted:
            continue
        if asset not in spot:
            spot[asset] = load_spot(cache, asset)
            print(f"{asset}: {len(spot[asset]):,} Coinbase minutes cached")
        close = parse_ts(m.get("close_time"))
        strike = m.get("floor_strike")
        result = m.get("result")
        if close is None or not isinstance(strike, (int, float)) or m.get("cap_strike") is not None:
            skipped["no single floor strike"] += 1
            continue
        if result not in ("yes", "no"):
            skipped["not settled yes/no"] += 1
            continue
        basis_rows.append((asset, float(strike), close, result))
        open_t = parse_ts(m.get("open_time")) or close - timedelta(minutes=15)
        quotes = [(c.ts, c.yes_bid, c.yes_ask) for c in _record_candles(rec) if open_t < c.ts <= close]
        pts = build_points(m["ticker"], asset, float(strike), close, result, quotes, spot[asset])
        if pts:
            points[m["ticker"]] = pts
        else:
            skipped["no spot price / volatility for its minutes"] += 1

    allp = [p for v in points.values() for p in v]
    print(f"\nMarkets priced: {len(points):,}   minutes priced: {len(allp):,}")
    for k, v in skipped.items():
        print(f"  skipped {v:,}: {k}")
    if not points:
        print("Nothing to analyse. Run  python scripts/replay.py --days 14  (it downloads the Coinbase prices too).")
        return

    print("\n1) BASIS  (Kalshi settles on its own index, not Coinbase)")
    agree, total = basis_agreement(basis_rows, spot)
    if total:
        print(f"   Coinbase-at-close agreed with Kalshi's result in {agree}/{total} markets ({agree / total:.1%}).")
        if agree / total < 0.97:
            print("   ⚠️  Big gap: the model's inputs don't match what Kalshi settles on; signals near the strike are noise.")

    print("\n2) ACCURACY  (Brier score, lower is better)")
    b_fair, b_mid, n = brier(allp)
    if n:
        print(f"   fair model {b_fair:.4f}   Kalshi mid {b_mid:.4f}   ({n:,} minutes)")
        if b_fair >= b_mid:
            print("   Kalshi's own prices are at least as accurate as the model -> most 'mispricings' are model error.")
        else:
            print("   The model is MORE accurate than Kalshi's prices here - that is the precondition for an edge.")
    print("   fair bucket   minutes markets  avg fair  actual YES  Kalshi mid")
    for bucket, npts, nm, f_avg, act, mid in calibration_table(allp):
        mid_s = f"{mid:9.1%}" if mid is not None else "      n/a"
        print(f"   {bucket:>10} {npts:9,} {nm:7,} {f_avg:9.1%} {act:11.1%} {mid_s}")

    print(f"\n3) STRATEGY  (one trade per market, costs = fee + {args.slippage:.0f}c slippage)")
    print("   In-sample by margin (for orientation only - picking the best row here is overfitting):")
    for mg in DEFAULT_MARGINS:
        print(f"   margin {mg:4.0f}c: {fmt_m(fv_metrics(simulate_fv(points, mg, args.fee_rate, args.slippage)))}")

    wf = walk_forward_fv(points, DEFAULT_MARGINS, args.fee_rate, args.slippage)
    print("\n   WALK-FORWARD (margin chosen on earlier days, tested on the next day):")
    for day, mg in wf.chosen:
        day_tr = [t for t in wf.trades if t.point.close_time.strftime("%Y-%m-%d") == day]
        tag = "sit out (no margin profitable before)" if mg is None else f"margin {mg:.0f}c -> {fmt_m(fv_metrics(day_tr))}"
        print(f"   {day}: {tag}")
    m = fv_metrics(wf.trades)
    print(f"\n   OUT-OF-SAMPLE TOTAL: {fmt_m(m)}")
    for side in ("yes", "no"):
        st = [t for t in wf.trades if t.side == side]
        if st:
            print(f"     buys {side.upper():3}: {fmt_m(fv_metrics(st))}")
    by_asset = defaultdict(list)
    for t in wf.trades:
        by_asset[t.point.asset].append(t)
    for a, ts in sorted(by_asset.items()):
        print(f"     {a}: {fmt_m(fv_metrics(ts))}")

    print("\nVERDICT")
    if m.trades == 0 and wf.chosen and all(mg is None for _, mg in wf.chosen):
        print("   ❌ No edge after costs: no margin was ever profitable on past days, so it never traded.")
        return
    if m.trades < 30:
        print(f"   ⚠️ INSUFFICIENT DATA: {m.trades} out-of-sample trades (need 30+). Download more days.")
        return
    lo, hi, p_neg = bootstrap_ev([t.pnl for t in wf.trades])
    print(f"   EV 95% range {lo * 100:+.2f}c to {hi * 100:+.2f}c per contract; chance true EV <= 0: {p_neg:.1%}")
    if lo > 0:
        print("   ✅ Positive out of sample with the whole range above zero. Next step: paper-trade it live.")
    elif (m.ev or 0) > 0:
        print("   🟡 Positive on average but could be luck. Collect more days before trusting it.")
    else:
        print("   ❌ No edge after costs. Do not trade this.")
    print(f"   (generated {datetime.now(UTC):%Y-%m-%d %H:%M} UTC; research only - the bot is unchanged)")


if __name__ == "__main__":
    main()
