"""Small statistics toolkit (no numpy dependency)."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass


def wilson(wins: float, n: float, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a proportion. Returns (low, high) in [0, 1]."""
    if n <= 0:
        return 0.0, 1.0
    p = wins / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (centre - margin) / denom), min(1.0, (centre + margin) / denom)


def mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def stdev(xs: Sequence[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    m = mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


def auc(scores: Sequence[float], labels: Sequence[int]) -> float | None:
    """Mann-Whitney AUC: P(score of a positive > score of a negative). Ties count 0.5."""
    pairs = sorted(zip(scores, labels, strict=True), key=lambda t: t[0])
    n_pos = sum(labels)
    n_neg = len(labels) - n_pos
    if n_pos == 0 or n_neg == 0:
        return None
    rank_sum = 0.0
    i = 0
    while i < len(pairs):
        j = i
        while j + 1 < len(pairs) and pairs[j + 1][0] == pairs[i][0]:
            j += 1
        avg_rank = (i + j) / 2 + 1
        rank_sum += avg_rank * sum(1 for k in range(i, j + 1) if pairs[k][1] == 1)
        i = j + 1
    return (rank_sum - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)


def auc_se(a: float, n_pos: int, n_neg: int) -> float:
    """Hanley & McNeil standard error of an AUC."""
    if n_pos == 0 or n_neg == 0:
        return 1.0
    q1, q2 = a / (2 - a), 2 * a * a / (1 + a)
    var = (a * (1 - a) + (n_pos - 1) * (q1 - a * a) + (n_neg - 1) * (q2 - a * a)) / (n_pos * n_neg)
    return math.sqrt(max(var, 1e-12))


@dataclass(frozen=True)
class TradeMetrics:
    trades: int
    wins: int
    win_rate: float | None
    wr_low: float
    wr_high: float
    pnl: float  # dollars (1 contract per trade)
    ev: float | None  # dollars per trade
    avg_win: float | None
    avg_loss: float | None
    profit_factor: float | None
    max_drawdown: float
    max_losing_streak: int
    sharpe_like: float | None  # mean / sd * sqrt(n) of per-trade P&L
    avg_entry: float | None  # cents
    avg_hold_sec: float | None

    @property
    def sufficient(self) -> bool:
        return self.trades >= 30


def trade_metrics(pnls: Sequence[float], entries: Sequence[float] = (), holds: Sequence[float] = ()) -> TradeMetrics:
    n = len(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    lo, hi = wilson(len(wins), n)
    equity = peak = dd = 0.0
    streak = worst = 0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        dd = max(dd, peak - equity)
        streak = streak + 1 if p <= 0 else 0
        worst = max(worst, streak)
    gross_win, gross_loss = sum(wins), -sum(losses)
    sd = stdev(pnls)
    return TradeMetrics(
        trades=n,
        wins=len(wins),
        win_rate=len(wins) / n if n else None,
        wr_low=lo,
        wr_high=hi,
        pnl=round(sum(pnls), 4),
        ev=sum(pnls) / n if n else None,
        avg_win=mean(wins) if wins else None,
        avg_loss=mean(losses) if losses else None,
        profit_factor=(gross_win / gross_loss) if gross_loss > 0 else (None if not wins else float("inf")),
        max_drawdown=round(dd, 4),
        max_losing_streak=worst,
        sharpe_like=(mean(pnls) / sd * math.sqrt(n)) if n >= 2 and sd > 0 else None,
        avg_entry=mean(entries) if entries else None,
        avg_hold_sec=mean(holds) if holds else None,
    )


def diff_z(a: Sequence[float], b: Sequence[float]) -> float | None:
    """Welch z-score for the difference of two means (a - b)."""
    if len(a) < 2 or len(b) < 2:
        return None
    se = math.sqrt(stdev(a) ** 2 / len(a) + stdev(b) ** 2 / len(b))
    diff = mean(a) - mean(b)
    if se == 0:  # both groups constant: the difference is either exact or nothing
        return None if diff == 0 else math.copysign(math.inf, diff)
    return diff / se
