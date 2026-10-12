"""Fair-value (mispricing) research - a different idea from momentum.

For a "will the coin finish above the strike" contract with T seconds left:

    fair P(YES) = Phi( ln(spot / strike) / (sigma * sqrt(T)) )

where sigma is the per-sqrt-second volatility realised by the underlying over
the previous hour. The strategy buys only when Kalshi's ask is cheaper than the
fair price by more than all costs plus a margin. The margin is chosen on EARLIER
days only and tested on the next day (walk-forward), so nothing is fitted to the
data it is judged on.

Two honesty checks come with it:
* calibration - is the model at least as accurate (Brier score) as Kalshi's
  own prices? If Kalshi is more accurate, "mispricings" are mostly model error;
* basis - how often does the Coinbase price at close agree with Kalshi's
  actual result? Kalshi settles on its own index, and a gap creates fake signals.
"""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import pairwise
from statistics import NormalDist

from app.data.underlying import SpotPrice
from app.research.calibration import cost_cents
from app.research.stats import TradeMetrics, mean, trade_metrics

_ND = NormalDist()
DEFAULT_MARGINS = (0.0, 1.0, 2.0, 3.0, 5.0, 8.0, 12.0)


def fair_prob(spot: float, strike: float, t_left_sec: float, sigma: float) -> float:
    """P(final > strike) for a driftless log-normal with per-sqrt-second volatility ``sigma``."""
    if spot <= 0 or strike <= 0:
        return 0.5
    t = max(t_left_sec, 1.0)
    s = max(sigma, 1e-7) * math.sqrt(t)
    return min(0.999, max(0.001, _ND.cdf(math.log(spot / strike) / s)))


def realized_sigma(spot: Sequence[SpotPrice], end: datetime, window_sec: float = 3600,
                   min_returns: int = 10) -> float | None:
    """Per-sqrt-second volatility from log returns in (end - window, end]."""
    lo = bisect_left(spot, end - timedelta(seconds=window_sec), key=lambda p: p.ts)
    hi = bisect_right(spot, end, key=lambda p: p.ts)  # spot is sorted by time
    pts = [p for p in spot[lo:hi] if p.price > 0]
    var_terms = []
    for a, b in pairwise(pts):
        dt = (b.ts - a.ts).total_seconds()
        if dt > 0:
            var_terms.append(math.log(b.price / a.price) ** 2 / dt)
    if len(var_terms) < min_returns:
        return None
    return math.sqrt(sum(var_terms) / len(var_terms))


def spot_at(spot: Sequence[SpotPrice], ts: datetime, max_age_sec: float = 120) -> SpotPrice | None:
    lo, hi = 0, len(spot)
    while lo < hi:  # last price with ts <= target (spot is sorted)
        mid = (lo + hi) // 2
        if spot[mid].ts <= ts:
            lo = mid + 1
        else:
            hi = mid
    if lo == 0:
        return None
    p = spot[lo - 1]
    return p if (ts - p.ts).total_seconds() <= max_age_sec else None


@dataclass(frozen=True)
class FVPoint:
    ticker: str
    asset: str
    ts: datetime
    close_time: datetime
    t_left: float
    spot: float
    strike: float
    sigma: float
    fair: float  # 0..1
    yes_bid: float | None  # cents
    yes_ask: float | None
    outcome: str  # yes | no

    @property
    def mid(self) -> float | None:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return (self.yes_bid + self.yes_ask) / 200

    @property
    def no_ask(self) -> float | None:
        return None if self.yes_bid is None else 100 - self.yes_bid


def build_points(ticker: str, asset: str, strike: float | None, close_time: datetime, outcome: str,
                 quotes: Iterable[tuple[datetime, float | None, float | None]],
                 spot: Sequence[SpotPrice]) -> list[FVPoint]:
    """quotes: (ts, yes_bid, yes_ask) in cents. Points are skipped (never invented) without spot/vol."""
    if not strike or outcome not in ("yes", "no"):
        return []
    out = []
    for ts, bid, ask in quotes:
        t_left = (close_time - ts).total_seconds()
        if t_left <= 0 or (bid is None and ask is None):
            continue
        s = spot_at(spot, ts)
        sig = realized_sigma(spot, ts)
        if s is None or sig is None:
            continue
        out.append(FVPoint(ticker, asset, ts, close_time, t_left, s.price, strike, sig,
                           fair_prob(s.price, strike, t_left, sig), bid, ask, outcome))
    return out


# ------------------------------------------------------------------ trading
@dataclass(frozen=True)
class FVTrade:
    point: FVPoint
    side: str
    price: float
    edge: float  # cents of expected value after costs at entry (model view)
    pnl: float  # dollars per contract, realised

    @property
    def won(self) -> bool:
        return self.point.outcome == self.side


def best_side(p: FVPoint, fee_rate: float, slippage: float) -> tuple[str | None, float, float]:
    """(side, entry price, model edge after costs in cents) for the better side, if any quote exists."""
    cands = []
    if p.yes_ask is not None and 1 <= p.yes_ask <= 99:
        cands.append(("yes", p.yes_ask, p.fair * 100 - p.yes_ask - cost_cents(p.yes_ask, fee_rate, slippage)))
    if p.no_ask is not None and 1 <= p.no_ask <= 99:
        cands.append(("no", p.no_ask, (1 - p.fair) * 100 - p.no_ask - cost_cents(p.no_ask, fee_rate, slippage)))
    if not cands:
        return None, 0.0, -1e9
    return max(cands, key=lambda c: c[2])


def simulate_fv(points_by_market: dict[str, list[FVPoint]], margin: float, fee_rate: float = 0.07,
                slippage: float = 1.0, min_t_left: float = 60.0) -> list[FVTrade]:
    """One trade per market: the first minute the model edge (after costs) reaches ``margin`` cents."""
    trades = []
    for pts in points_by_market.values():
        for p in sorted(pts, key=lambda x: x.ts):
            if p.t_left < min_t_left:
                continue
            side, price, edge = best_side(p, fee_rate, slippage)
            if side is None or edge < margin:
                continue
            paid = price + cost_cents(price, fee_rate, slippage)
            pnl = ((100 if p.outcome == side else 0) - paid) / 100
            trades.append(FVTrade(p, side, price, edge, pnl))
            break
    return sorted(trades, key=lambda t: t.point.ts)


def fv_metrics(trades: list[FVTrade]) -> TradeMetrics:
    return trade_metrics([t.pnl for t in trades], [t.price for t in trades], [t.point.t_left for t in trades])


@dataclass
class FVWalkForward:
    trades: list[FVTrade]
    chosen: list[tuple[str, float | None]]  # (test day, margin chosen on earlier days)


def walk_forward_fv(points_by_market: dict[str, list[FVPoint]], margins: Sequence[float] = DEFAULT_MARGINS,
                    fee_rate: float = 0.07, slippage: float = 1.0, min_train_trades: int = 20,
                    min_train_days: int = 2) -> FVWalkForward:
    """For each day, choose the margin with the best EV on ALL earlier days, then trade that day."""
    by_day: dict[str, dict[str, list[FVPoint]]] = defaultdict(dict)
    for ticker, pts in points_by_market.items():
        if pts:
            by_day[pts[0].close_time.strftime("%Y-%m-%d")][ticker] = pts
    days = sorted(by_day)
    out: list[FVTrade] = []
    chosen: list[tuple[str, float | None]] = []
    for i, day in enumerate(days):
        if i < min_train_days:
            continue
        train = {k: v for d in days[:i] for k, v in by_day[d].items()}
        best, best_ev = None, None
        for m in margins:
            tr = simulate_fv(train, m, fee_rate, slippage)
            if len(tr) >= min_train_trades:
                ev = mean([t.pnl for t in tr])
                if ev > 0 and (best_ev is None or ev > best_ev):
                    best, best_ev = m, ev
        chosen.append((day, best))
        if best is not None:  # no margin was profitable on past data -> sit out
            out.extend(simulate_fv(by_day[day], best, fee_rate, slippage))
    return FVWalkForward(out, chosen)


# ------------------------------------------------------------ diagnostics
def brier(points: Sequence[FVPoint]) -> tuple[float | None, float | None, int]:
    """(Brier of fair model, Brier of Kalshi mid, n) on points that have both."""
    pairs = [(p.fair, p.mid, 1.0 if p.outcome == "yes" else 0.0) for p in points if p.mid is not None]
    if not pairs:
        return None, None, 0
    return (mean([(f - y) ** 2 for f, _, y in pairs]), mean([(m - y) ** 2 for _, m, y in pairs]), len(pairs))


def calibration_table(points: Sequence[FVPoint]) -> list[tuple[str, int, int, float, float, float | None]]:
    """Per fair-probability decile: (bucket, points, markets, avg fair, actual YES rate, avg Kalshi mid)."""
    rows: dict[int, list[FVPoint]] = defaultdict(list)
    for p in points:
        rows[min(9, int(p.fair * 10))].append(p)
    out = []
    for b in sorted(rows):
        pts = rows[b]
        mids = [p.mid for p in pts if p.mid is not None]
        out.append((f"{b * 10}-{b * 10 + 10}%", len(pts), len({p.ticker for p in pts}), mean([p.fair for p in pts]),
                    mean([1.0 if p.outcome == "yes" else 0.0 for p in pts]), mean(mids) if mids else None))
    return out


def basis_agreement(markets: Iterable[tuple[str, float | None, datetime, str]],
                    spot_by_asset: dict[str, list[SpotPrice]]) -> tuple[int, int]:
    """(agree, total): does Coinbase at close (vs strike) match Kalshi's result?"""
    agree = total = 0
    for asset, strike, close, outcome in markets:
        s = spot_at(spot_by_asset.get(asset, []), close)
        if s is None or not strike or outcome not in ("yes", "no"):
            continue
        total += 1
        agree += int((s.price > strike) == (outcome == "yes"))
    return agree, total
