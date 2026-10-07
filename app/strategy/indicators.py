"""Small, dependency-free numeric indicators."""

from __future__ import annotations

import math
from collections.abc import Sequence


def ema(values: Sequence[float], period: int) -> float | None:
    if not values:
        return None
    k = 2 / (period + 1)
    out = values[0]
    for v in values[1:]:
        out = v * k + out * (1 - k)
    return out


def linreg_slope(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Least-squares slope of ys over xs (units of y per unit of x)."""
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / den


def stdev(values: Sequence[float]) -> float | None:
    n = len(values)
    if n < 2:
        return None
    m = sum(values) / n
    return math.sqrt(sum((v - m) ** 2 for v in values) / (n - 1))


def diffs(values: Sequence[float]) -> list[float]:
    return [b - a for a, b in zip(values, values[1:], strict=False)]


def clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def squash(x: float, scale: float) -> float:
    """Map x to (-1, 1) with ``scale`` giving ~0.76 (tanh)."""
    if scale <= 0:
        return 0.0
    return math.tanh(x / scale)
