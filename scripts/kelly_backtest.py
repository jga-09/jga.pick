"""Backtest position SIZING: fixed units vs fractional Kelly (Low / Medium / High) on real Kalshi history.

    python scripts/kelly_backtest.py                       # HYPE, DOGE (the coins in the forward test)
    python scripts/kelly_backtest.py --assets BTC,ETH,SOL,XRP,HYPE,DOGE --edge 6

Trades: the bot's fair-value rule (scripts/guide_test.py approach A, mid-window, edge >= --edge cents after
fees), one per market, paying the ask + Kalshi's rounded-up fee + slippage, held to settlement. The SAME trades
are sized four ways, chronologically, from the same starting bankroll:

  FIXED    risk 1% of the STARTING bankroll on every trade (fixed unit)
  LOW      0.25x Kelly, max 1% of the current bankroll per trade       (app/risk/kelly.py - the bot's engine)
  MEDIUM   0.50x Kelly, max 2%
  HIGH     0.75x Kelly, max 3%

Kelly's probability is (1) the model's P(win), (2) CALIBRATED walk-forward - only on markets that settled before
the trade's day, per coin, by 10% bucket, shrunk toward the model when data is thin - and (3) made cautious with
an uncertainty band (volatility +/-20%, 3 bps settlement-index basis). Daily loss limit: 6% of the day's starting
bankroll. Reported on the earlier days and, separately and from a fresh bankroll, on the untouched last 7 days.

Limits: 1-minute quotes (no order book: real fills may be worse, size may not be available), trades are taken one
at a time in close-time order (overlapping markets are not netted), Coinbase prices as a proxy for CF Benchmarks.
Kelly cannot create an edge: with a negative edge it shrinks bets toward zero, it never turns losses into profit.
Read-only, research only.
"""

from __future__ import annotations

import argparse
import importlib.util
import math
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import NormalDist

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.research.model_data import load_crypto_bars, load_markets  # noqa: E402
from app.risk.kelly import KellyInput, KellyMode  # noqa: E402
from app.risk.kelly import size as kelly_size  # noqa: E402
from app.trading.fees import estimate_fee  # noqa: E402

ND = NormalDist()
_spec = importlib.util.spec_from_file_location("guide_test", ROOT / "scripts" / "guide_test.py")
G = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(G)  # type: ignore[union-attr]


@dataclass(frozen=True)
class Trade:
    close: datetime
    asset: str
    side: str
    price: float  # cents paid per contract (ask + slippage)
    won: bool
    p: float  # model P(win)
    p_low: float  # cautious P(win)


def _p_side(spot: float, strike: float, sigma: float, t_left: float, side: str) -> float | None:
    p = G.p_yes(spot, strike, sigma, t_left)
    return None if p is None else (p if side == "yes" else 1 - p)


def collect(asset: str, cache: Path, edge: float, slip: float) -> list[Trade]:
    bars = load_crypto_bars(cache, asset)
    markets = [m for m in load_markets(cache, f"KX{asset}15M-") if m["result"] in ("yes", "no") and m["strike"]]
    if not bars or not markets:
        print(f"{asset}: no data (run: python scripts/replay.py --days 30 --no-trades --assets {asset})")
        return []
    sp = G.Spot(bars)
    out = []
    for m in markets:
        q = G.quotes(m)
        pick = G.rule_a(m, q, sp, 5, 11, edge, slip)
        if pick is None:
            continue
        side, price = pick
        # recompute the probability at the entry minute (rule_a returns the first qualifying minute)
        for k in range(11, 4, -1):
            at = m["close"] - timedelta(minutes=k)
            if at not in q or (i := sp.at(at)) is None:
                continue
            bid, ask = q[at]
            if (ask if side == "yes" else 100 - bid) != price:
                continue
            strike, s, sig = float(m["strike"]), sp.close[i], sp.sigma_per_sec(i)
            p = _p_side(s, strike, sig, k * 60, side)
            if p is None:
                continue
            band = [_p_side(s * (1 + b), strike, sig * f, k * 60, side)
                    for f in (0.8, 1.2) for b in (-3e-4, 3e-4)]
            p_low = min([p] + [x for x in band if x is not None])
            out.append(Trade(m["close"], asset, side, price + slip, (m["result"] == side), p, p_low))
            break
    return out


def calibrate(trades: list[Trade], prior: float = 30.0) -> list[Trade]:
    """Walk-forward bucket calibration per coin: uses only trades that SETTLED on earlier days."""
    out = []
    hist: dict[tuple[str, int], list[int]] = defaultdict(lambda: [0, 0])  # (asset, bucket) -> [n, wins]
    pending: list[Trade] = []
    day = None
    for t in sorted(trades, key=lambda x: x.close):
        d = t.close.date()
        if d != day:  # a new day: everything from earlier days is now known
            for x in pending:
                h = hist[(x.asset, min(9, int(x.p * 10)))]
                h[0] += 1
                h[1] += x.won
            pending, day = [], d
        n, w = hist[(t.asset, min(9, int(t.p * 10)))]
        p_cal = (w + prior * t.p) / (n + prior)  # shrunk toward the model when the bucket is thin
        shift = p_cal - t.p
        out.append(Trade(t.close, t.asset, t.side, t.price, t.won, min(0.999, max(0.001, p_cal)),
                         min(0.999, max(0.001, t.p_low + shift))))
        pending.append(t)
    return out


@dataclass
class Result:
    name: str
    start: float
    bankroll: float = 0.0
    peak: float = 0.0
    max_dd: float = 0.0
    trades: int = 0
    wins: int = 0
    staked: float = 0.0
    pnl: float = 0.0
    streak: int = 0
    longest: int = 0
    rets: list[float] | None = None

    def line(self) -> str:
        if not self.trades:
            return f"  {self.name:<7} no trades (Kelly says 0% - no measured edge)"
        growth = self.bankroll / self.start - 1
        roi = self.pnl / self.staked if self.staked else 0.0
        rets = self.rets or []
        mean = sum(rets) / len(rets)
        sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / max(1, len(rets) - 1)) or 1e-12
        return (f"  {self.name:<7} {self.trades:5} trades  win {self.wins / self.trades:5.1%}  "
                f"bankroll ${self.bankroll:9,.2f} ({growth:+7.1%})  maxDD {self.max_dd:6.1%}  ROI {roi:+6.1%}  "
                f"avg risk ${self.staked / self.trades:7.2f}  longest losing {self.longest:3}  "
                f"risk-adj {mean / sd * math.sqrt(len(rets)):+5.2f}")


def simulate(name: str, trades: list[Trade], start: float, fee_rate: float, mode: KellyMode | None) -> Result:
    r = Result(name, start, bankroll=start, peak=start, rets=[])
    day, day_start, day_pnl = None, start, 0.0
    for t in trades:
        if t.close.date() != day:
            day, day_start, day_pnl = t.close.date(), r.bankroll, 0.0
        daily_left = max(0.0, 0.06 * day_start + min(0.0, day_pnl))
        if mode is None:
            per = t.price / 100 + estimate_fee(1, t.price, fee_rate)
            n = int(min(0.01 * start, daily_left, r.bankroll) // per)
            while n > 0 and n * t.price / 100 + estimate_fee(n, t.price, fee_rate) > min(0.01 * start, daily_left):
                n -= 1
        else:
            ks = kelly_size(mode, KellyInput(p_point=t.p, p_low=t.p_low, price_cents=t.price, bankroll_usd=r.bankroll,
                                             remaining_daily_loss_usd=daily_left, remaining_open_risk_usd=r.bankroll,
                                             calibrated=True, fee_rate=fee_rate))
            n = ks.contracts
        if n < 1:
            continue
        cost = n * t.price / 100 + estimate_fee(n, t.price, fee_rate)
        pnl = (n if t.won else 0) - cost
        before = r.bankroll
        r.bankroll += pnl
        day_pnl += pnl
        r.trades += 1
        r.wins += t.won
        r.staked += cost
        r.pnl += pnl
        r.rets.append(pnl / before)  # type: ignore[union-attr]
        r.streak = 0 if t.won else r.streak + 1
        r.longest = max(r.longest, r.streak)
        r.peak = max(r.peak, r.bankroll)
        r.max_dd = max(r.max_dd, 1 - r.bankroll / r.peak)
        if r.bankroll <= 0.5:
            break
    return r


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--assets", default="HYPE,DOGE")
    ap.add_argument("--cache", default=str(ROOT / "data" / "replay_cache"))
    ap.add_argument("--edge", type=float, default=6.0, help="required edge after fees, cents (default 6)")
    ap.add_argument("--slippage", type=float, default=1.0)
    ap.add_argument("--fee-rate", type=float, default=0.07)
    ap.add_argument("--bankroll", type=float, default=1000.0)
    ap.add_argument("--holdout-days", type=int, default=7)
    a = ap.parse_args()
    trades: list[Trade] = []
    for asset in [x.strip().upper() for x in a.assets.split(",") if x.strip()]:
        trades += collect(asset, Path(a.cache), a.edge, a.slippage)
    if not trades:
        return 1
    trades = calibrate(trades)
    cut = max(t.close for t in trades) - timedelta(days=a.holdout_days)
    parts = (("EARLIER DAYS", [t for t in trades if t.close < cut]),
             (f"LAST {a.holdout_days} DAYS (untouched, fresh ${a.bankroll:,.0f})", [t for t in trades if t.close >= cut]))
    print(f"KELLY vs FIXED sizing · {a.assets} · fair value mid-window, edge >= {a.edge:g}c · {len(trades)} candidate "
          f"trades {min(t.close for t in trades):%Y-%m-%d}..{max(t.close for t in trades):%Y-%m-%d}")
    for title, ts in parts:
        print(f"\n=== {title}: {len(ts)} candidate trades ===")
        if not ts:
            continue
        print(simulate("FIXED", ts, a.bankroll, a.fee_rate, None).line())
        for m in KellyMode:
            print(simulate(m.value.upper(), ts, a.bankroll, a.fee_rate, m).line())
    print("\nHow to read it: sizing cannot create an edge. If FIXED loses, Kelly should lose LESS (it bets less when\n"
          "the cautious edge is small) - not win. Only trust a Kelly mode if it is positive on BOTH parts.\n"
          "maxDD = worst fall from a peak; risk-adj = mean/sd of per-trade bankroll returns x sqrt(trades).\n"
          f"(generated {datetime.now(UTC):%Y-%m-%d %H:%M} UTC · research only, nothing traded)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
