"""Position sizing: how many contracts, given a profile and current exposure."""

from __future__ import annotations

import math
from dataclasses import dataclass

from app.risk.profiles import RiskProfile
from app.trading.fees import estimate_fee


@dataclass(frozen=True)
class SizeResult:
    contracts: int
    price_cents: float
    cost_usd: float  # contracts * price
    fee_usd: float
    limiting_factor: str

    @property
    def total_usd(self) -> float:
        return round(self.cost_usd + self.fee_usd, 2)


class PositionSizer:
    def __init__(self, fee_rate: float = 0.07) -> None:
        self.fee_rate = fee_rate

    def size(
        self,
        profile: RiskProfile,
        *,
        price_cents: float,
        confidence: int,
        balance_usd: float,
        open_exposure_usd: float,
        liquidity_contracts: float | None,
    ) -> SizeResult:
        if price_cents <= 0 or price_cents >= 100:
            return SizeResult(0, price_cents, 0.0, 0.0, "invalid price")

        # Scale between 50% and 100% of max size by how far confidence clears the bar.
        span = max(1, 100 - profile.min_confidence)
        scale = 0.5 + 0.5 * min(1.0, max(0.0, (confidence - profile.min_confidence) / span))
        budgets = {
            "profile max position": profile.max_position_usd * scale,
            "total exposure limit": profile.max_total_exposure_usd - open_exposure_usd,
            "balance fraction": balance_usd * profile.max_balance_fraction,
            "available balance": balance_usd,
        }
        factor, budget = min(budgets.items(), key=lambda kv: kv[1])
        if budget <= 0:
            return SizeResult(0, price_cents, 0.0, 0.0, factor)

        per_contract = price_cents / 100 * (1 + self.fee_rate)  # conservative fee headroom
        contracts = math.floor(budget / per_contract + 1e-9)
        if liquidity_contracts is not None and contracts > liquidity_contracts:
            contracts, factor = int(liquidity_contracts), "book liquidity"
        # Final exact check including the real fee estimate.
        while contracts > 0:
            cost = contracts * price_cents / 100
            fee = estimate_fee(contracts, price_cents, self.fee_rate)
            if cost + fee <= budget + 1e-9 and cost <= profile.max_position_usd + 1e-9:
                return SizeResult(contracts, price_cents, round(cost, 2), fee, factor)
            contracts -= 1
        return SizeResult(0, price_cents, 0.0, 0.0, factor)
