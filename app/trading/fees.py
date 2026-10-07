"""Fee estimation.

Kalshi's published general trading-fee formula is
``ceil_to_cent(rate * contracts * P * (1 - P))`` with P in dollars. The rate is
configurable (FEE_RATE) because it varies by series; treat this as an estimate.
"""

from __future__ import annotations

import math


def estimate_fee(contracts: int, price_cents: float, rate: float) -> float:
    if contracts <= 0 or rate <= 0:
        return 0.0
    p = price_cents / 100
    raw = rate * contracts * p * (1 - p)
    return math.ceil(round(raw * 100, 6)) / 100
