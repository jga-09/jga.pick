"""Tunable strategy parameters (weights, no-trade thresholds, regime cut-offs).

Defaults are deliberately round numbers chosen a priori, NOT fitted to data.
Override via environment (see .env.example) once research shows a reason to.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any

# Directional components (signed, YES-positive) and their maximum quality points.
DEFAULT_DIRECTIONAL_POINTS: dict[str, float] = {
    "momentum": 16, "trend": 13, "orderbook": 11, "volume": 9,
    "prob_move": 6, "underlying": 12, "acceleration": 8,
}
# Non-directional quality components (0..1) and their maximum points.
DEFAULT_QUALITY_POINTS: dict[str, float] = {"liquidity": 9, "volatility": 6, "stability": 6, "time": 4}

ALL_FILTERS = (
    "conflict", "weak_momentum", "low_volume", "abnormal_spread", "low_liquidity", "sideways",
    "extreme_volatility", "too_little_time", "flip_flop", "coin_flip", "underlying_conflict",
    "weak_book", "extended_move", "adverse_selection", "decelerating", "high_volatility",
    "low_stability", "underlying_unavailable", "divergence",
)
# Soft flags count against a profile's max_soft_flags; hard flags always block.
SOFT_FILTERS = frozenset({"decelerating", "high_volatility", "weak_book", "low_stability",
                          "underlying_unavailable", "divergence"})


@dataclass(frozen=True)
class StrategyConfig:
    directional_points: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_DIRECTIONAL_POINTS))
    quality_points: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_QUALITY_POINTS))
    conflict_penalty: float = 0.5  # fraction of a component's points subtracted when it disagrees
    renormalize_missing: bool = True  # rescale directional points when a component has no data
    disabled_filters: frozenset[str] = frozenset()

    # No-trade thresholds
    conflict_level: float = 0.3  # component counts as "against" at this aligned score
    conflict_count: int = 2
    min_momentum: float = 0.25
    min_trades_2m: int = 3
    max_spread_cents: float = 10.0
    min_ask_liquidity: float = 10.0
    min_time_sec: float = 60.0
    max_flips: int = 2
    coin_flip_strike_pct: float = 0.02
    coin_flip_mid_band: float = 3.0  # |mid - 50| below this with < coin_flip_time_sec left
    coin_flip_time_sec: float = 240.0
    weak_book_level: float = 0.05
    extended_z: float = 2.5  # 5-min normalised move already beyond this = move extended
    extended_price: float = 80.0  # or the side we would buy already costs this much
    adverse_spike_z: float = 2.5  # 30s normalised spike in our direction = adverse-selection risk
    min_stability: float = 0.5

    # Regime cut-offs
    vol_high: float = 1.6  # realised / implied probit volatility (1.0 = consistent with prices)
    vol_extreme: float = 2.6
    strong_trend_eff: float = 0.6
    strong_trend_move: float = 1.0  # normalised 3-min move (sigmas)
    moderate_trend_eff: float = 0.35
    moderate_trend_move: float = 0.5
    chaotic_eff: float = 0.2
    low_liq_spread: float = 10.0
    low_liq_depth: float = 20.0

    # Setup grades (minimum quality)
    grade_a_plus: int = 90
    grade_a: int = 80
    grade_b: int = 70
    grade_c: int = 60

    def enabled(self, name: str) -> bool:
        return name not in self.disabled_filters

    @classmethod
    def from_settings(cls, s: Any) -> StrategyConfig:
        kw: dict[str, Any] = {}
        if getattr(s, "signal_weights", None):
            dp = dict(DEFAULT_DIRECTIONAL_POINTS)
            qp = dict(DEFAULT_QUALITY_POINTS)
            for k, v in s.signal_weights.items():
                if k in dp:
                    dp[k] = float(v)
                elif k in qp:
                    qp[k] = float(v)
            kw["directional_points"], kw["quality_points"] = dp, qp
        disabled = getattr(s, "disabled_filters", "") or ""
        kw["disabled_filters"] = frozenset(x.strip() for x in disabled.split(",") if x.strip())
        overrides = getattr(s, "strategy_params", None) or {}
        names = {f.name for f in fields(cls)}
        for k, v in overrides.items():
            if k in names and k not in kw:
                kw[k] = type(getattr(cls(), k))(v)
        return cls(**kw)
