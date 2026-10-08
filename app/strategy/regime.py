"""Market-regime classification from recent price behaviour."""

from __future__ import annotations

from enum import StrEnum

from app.strategy.config import StrategyConfig


class Regime(StrEnum):
    STRONG_TREND = "STRONG_TREND"
    MODERATE_TREND = "MODERATE_TREND"
    SIDEWAYS = "SIDEWAYS"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    CHAOTIC = "CHAOTIC"
    LOW_LIQUIDITY = "LOW_LIQUIDITY"
    UNKNOWN = "UNKNOWN"

    @property
    def badge(self) -> str:
        return {
            "STRONG_TREND": "🟢 STRONG TREND", "MODERATE_TREND": "🟢 MODERATE TREND",
            "SIDEWAYS": "🟡 SIDEWAYS", "HIGH_VOLATILITY": "🔴 HIGH VOLATILITY", "CHAOTIC": "🔴 CHAOTIC",
            "LOW_LIQUIDITY": "⚪ LOW LIQUIDITY", "UNKNOWN": "⚪ UNKNOWN",
        }[self.value]


def classify(f: dict[str, float | None], cfg: StrategyConfig) -> Regime:
    spread, depth = f.get("spread"), f.get("book_total_depth")
    if spread is None or spread > cfg.low_liq_spread or (depth is not None and depth < cfg.low_liq_depth):
        return Regime.LOW_LIQUIDITY
    vol, eff, move = f.get("vol_ratio"), f.get("efficiency"), f.get("zmove_180s")
    if vol is None or eff is None or move is None:
        return Regime.UNKNOWN
    if vol >= cfg.vol_high and eff < cfg.chaotic_eff:
        return Regime.CHAOTIC
    if vol >= cfg.vol_high:
        return Regime.HIGH_VOLATILITY
    if eff >= cfg.strong_trend_eff and abs(move) >= cfg.strong_trend_move:
        return Regime.STRONG_TREND
    if eff >= cfg.moderate_trend_eff and abs(move) >= cfg.moderate_trend_move:
        return Regime.MODERATE_TREND
    return Regime.SIDEWAYS
