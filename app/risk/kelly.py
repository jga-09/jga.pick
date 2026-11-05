"""Fractional Kelly position sizing for Kalshi binaries ($1 settlement).

    b          = (1 - c) / c                      c = expected execution price in dollars (before fees)
    full Kelly = (p * b - (1 - p)) / b            = (p - c) / (1 - c)
    fee Kelly  = (p - c_fee) / (1 - c_fee)        c_fee = c + fee per contract  (what sizing actually uses)

``p`` is the strategy's probability of winning, made conservative for estimation uncertainty (the lower end
of the model's range), never a confidence score. Sizing uses the smaller fee-inclusive Kelly, multiplied by the
mode's fraction (never full Kelly), and the loss budget is the smallest of:

    bankroll x fractional Kelly · bankroll x mode cap · remaining daily loss · remaining open-risk allowance

Contracts are rounded DOWN so that contracts x price + Kalshi's rounded-up fee <= budget. One contract over
budget -> no trade. A binary buy can lose its whole cost + fee, so that is the maximum loss per contract.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from app.trading.fees import estimate_fee


class KellyMode(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    @property
    def multiplier(self) -> float:
        return {"low": 0.25, "medium": 0.50, "high": 0.75}[self.value]

    @property
    def position_cap(self) -> float:
        """Hard cap on one position's maximum loss, as a fraction of bankroll."""
        return {"low": 0.01, "medium": 0.02, "high": 0.03}[self.value]

    @property
    def label(self) -> str:
        return f"{self.value.title()} ({self.multiplier:g}x Kelly, max {self.position_cap:.0%} per trade)"


@dataclass(frozen=True)
class KellyInput:
    p_point: float | None  # model's estimated P(win) for the side being bought
    p_low: float | None  # lower end of its uncertainty range (sizing uses this)
    price_cents: float | None  # expected execution price (worst-case limit), cents
    bankroll_usd: float
    remaining_daily_loss_usd: float
    remaining_open_risk_usd: float
    calibrated: bool  # probability checked against settled out-of-sample results for THIS coin
    fee_rate: float | None
    max_position_usd: float | None = None  # profile dollar cap (still applies)
    liquidity_contracts: float | None = None
    require_calibration: bool = False  # True for live money


@dataclass(frozen=True)
class KellySizing:
    mode: KellyMode
    contracts: int
    reason: str  # "" when a trade is allowed, else why not
    p_used: float | None = None
    price_cents: float | None = None
    breakeven: float | None = None  # P(win) needed to break even, fee included
    full_kelly: float | None = None  # spec formula (before fees)
    fee_kelly: float | None = None  # fee-inclusive full Kelly
    fractional_kelly: float | None = None  # fee Kelly x multiplier (fraction of bankroll)
    budget_usd: float = 0.0
    limiting_factor: str = ""
    cost_usd: float = 0.0
    fee_usd: float = 0.0
    calibrated: bool = False

    @property
    def ok(self) -> bool:
        return self.contracts >= 1 and not self.reason

    @property
    def max_loss_usd(self) -> float:
        return round(self.cost_usd + self.fee_usd, 2)


def full_kelly(p: float, price: float) -> float:
    """Spec formula: f = (p*b - (1-p)) / b with b = (1-c)/c (c in dollars)."""
    b = (1 - price) / price
    return (p * b - (1 - p)) / b


def max_loss(contracts: int, price_cents: float, fee_rate: float) -> float:
    return contracts * price_cents / 100 + estimate_fee(contracts, price_cents, fee_rate)


def size(mode: KellyMode, inp: KellyInput) -> KellySizing:
    def no(reason: str, **kw: object) -> KellySizing:
        return KellySizing(mode, 0, reason, calibrated=inp.calibrated, **kw)  # type: ignore[arg-type]

    p, price, rate = inp.p_low, inp.price_cents, inp.fee_rate
    if p is None or inp.p_point is None or not (0 < p < 1) or not (0 < inp.p_point < 1) or p > inp.p_point:
        return no("Kelly: probability missing or invalid")
    if price is None or not (1 <= price <= 99) or not math.isfinite(price):
        return no("Kelly: execution price cannot be verified")
    if rate is None or not (0 < rate < 1):
        return no("Kelly: fee rate cannot be verified")
    if inp.bankroll_usd <= 0:
        return no("Kelly: no bankroll")
    if inp.require_calibration and not inp.calibrated:
        return no("Kelly: probability not yet calibrated on this coin's settled results (required for live)",
                  p_used=p, price_cents=price)
    c = price / 100
    c_fee = c + estimate_fee(1, price, rate)  # worst case: 1 contract pays the most fee per contract
    be = min(0.999, c_fee)
    fk = full_kelly(p, c)
    fee_k = (p - c_fee) / (1 - c_fee) if c_fee < 1 else -1.0
    base = dict(p_used=p, price_cents=price, breakeven=be, full_kelly=fk, fee_kelly=fee_k)
    if p <= be:
        return no(f"Kelly: win chance {p:.1%} does not beat the {be:.1%} break-even (fee included) - Kelly says 0%",
                  fractional_kelly=0.0, **base)
    frac = max(0.0, min(fk, fee_k) * mode.multiplier)
    budgets = {
        "fractional Kelly": inp.bankroll_usd * frac,
        f"{mode.value} cap {mode.position_cap:.0%} of bankroll": inp.bankroll_usd * mode.position_cap,
        "remaining daily loss": inp.remaining_daily_loss_usd,
        "remaining open-risk allowance": inp.remaining_open_risk_usd,
    }
    if inp.max_position_usd is not None:
        budgets["profile max position"] = inp.max_position_usd
    factor, budget = min(budgets.items(), key=lambda kv: kv[1])
    budget = max(0.0, budget)
    n = math.floor(budget / c + 1e-9) if budget > 0 else 0
    if inp.liquidity_contracts is not None and n > inp.liquidity_contracts:
        n, factor = int(inp.liquidity_contracts), "book liquidity"
    while n > 0 and max_loss(n, price, rate) > budget + 1e-9:
        n -= 1
    if n < 1:
        return no(f"Kelly: one contract (${max_loss(1, price, rate):.2f} max loss) exceeds the ${budget:.2f} budget "
                  f"({factor})", fractional_kelly=frac, budget_usd=budget, limiting_factor=factor, **base)
    fee = estimate_fee(n, price, rate)
    return KellySizing(mode, n, "", fractional_kelly=frac, budget_usd=round(budget, 2), limiting_factor=factor,
                       cost_usd=round(n * price / 100, 2), fee_usd=fee, calibrated=inp.calibrated, **base)
