"""Turn features into a composite directional score, confidence and reasons.

The confidence (0-100) is a *signal-strength* score. It is not a calibrated
probability of winning.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.strategy.indicators import clamp, squash
from app.strategy.signals import Reason

# Component weights (re-normalised over the components that have data).
DEFAULT_WEIGHTS: dict[str, float] = {
    "momentum": 0.25,
    "trend": 0.20,
    "book": 0.15,
    "flow": 0.15,
    "level": 0.10,
    "underlying": 0.15,
}
NEUTRAL_BAND = 0.08  # |score| below this is treated as no direction


@dataclass(frozen=True)
class ScoreResult:
    score: float
    confidence: int
    components: dict[str, float]
    reasons: tuple[Reason, ...]


def _components(f: dict[str, float | None]) -> dict[str, float]:
    c: dict[str, float] = {}
    if f.get("mom_60s") is not None:
        m = squash(f["mom_60s"], 3.0)  # a 3c move in 60s is a strong move
        if f.get("accel") is not None:
            m = clamp(m + 0.15 * squash(f["accel"], 3.0))
        c["momentum"] = m
    trend_parts = []
    if f.get("ema_diff") is not None:
        trend_parts.append(squash(f["ema_diff"], 2.0))
    if f.get("slope_cpm") is not None:
        trend_parts.append(squash(f["slope_cpm"], 1.5))
    if trend_parts:
        c["trend"] = sum(trend_parts) / len(trend_parts)
    if f.get("book_imbalance") is not None:
        c["book"] = clamp(f["book_imbalance"] * 1.2)
    if f.get("trade_flow") is not None:
        conf = min(1.0, (f.get("trade_count_120s") or 0) / 5)
        c["flow"] = f["trade_flow"] * conf
    if f.get("yes_mid") is not None:
        c["level"] = squash(f["yes_mid"] - 50, 25.0)
    und = []
    if f.get("strike_dist_pct") is not None:
        und.append(squash(f["strike_dist_pct"], 0.10))
    if f.get("spot_mom_pct") is not None:
        und.append(squash(f["spot_mom_pct"], 0.05))
    if und:
        c["underlying"] = sum(und) / len(und)
    return c


def score_features(
    f: dict[str, float | None], weights: dict[str, float] | None = None
) -> ScoreResult:
    weights = weights or DEFAULT_WEIGHTS
    comps = _components(f)
    total_w = sum(weights.get(k, 0) for k in comps)
    if total_w <= 0:
        return ScoreResult(0.0, 0, comps, (Reason(False, "No usable market data yet"),))
    score = sum(weights.get(k, 0) * v for k, v in comps.items()) / total_w
    sign = 1 if score > 0 else -1
    agree_w = sum(weights.get(k, 0) for k, v in comps.items() if v * sign > 0.1)
    agreement = agree_w / total_w

    confidence = 50 + 50 * (abs(score) ** 0.8) * (0.6 + 0.4 * agreement)
    reasons: list[Reason] = []
    penalties = 0.0
    spread = f.get("spread")
    if spread is not None and spread > 6:
        penalties += min(10.0, spread - 6)
        reasons.append(Reason(False, f"Spread wide ({spread:.0f}¢)"))
    vol = f.get("volatility")
    if vol is not None and vol > 2.5:
        penalties += 5
        reasons.append(Reason(False, "Volatility elevated"))
    hist = f.get("history_sec") or 0
    if hist < 60:
        confidence = min(confidence, 60)
        reasons.append(Reason(False, "Limited price history"))
    tr = f.get("time_remaining")
    if tr is not None and tr < 60:
        penalties += 10
        reasons.append(Reason(False, "Market about to close"))
    confidence = int(round(max(0.0, min(100.0, confidence - penalties))))

    reasons = _direction_reasons(comps, sign, f) + reasons
    return ScoreResult(round(score, 4), confidence, comps, tuple(reasons))


def _direction_reasons(c: dict[str, float], sign: int, f: dict[str, float | None]) -> list[Reason]:
    up = sign > 0
    r: list[Reason] = []
    m = c.get("momentum")
    if m is not None:
        if m * sign >= 0.5:
            r.append(Reason(True, f"Strong {'positive' if up else 'negative'} momentum"))
        elif m * sign >= 0.2:
            r.append(Reason(True, f"{'Positive' if up else 'Negative'} momentum"))
        elif abs(m) < 0.2:
            r.append(Reason(False, "Momentum weak"))
        else:
            r.append(Reason(False, "Momentum against signal"))
    b = c.get("book")
    if b is not None:
        if b * sign >= 0.2:
            r.append(Reason(True, f"{'Buyers' if up else 'Sellers'} dominating order book"))
        elif abs(b) < 0.2:
            r.append(Reason(False, "Order book mixed"))
        else:
            r.append(Reason(False, "Order book leans the other way"))
    t = c.get("trend")
    if t is not None:
        if t * sign >= 0.2:
            r.append(Reason(True, f"Market probability trending {'upward' if up else 'downward'}"))
        elif t * sign <= -0.2:
            r.append(Reason(False, "Trend disagrees"))
    fl = c.get("flow")
    if fl is not None and fl * sign >= 0.3:
        r.append(Reason(True, f"Recent trades favour {'YES' if up else 'NO'}"))
    u = c.get("underlying")
    if u is not None and u * sign >= 0.3:
        r.append(Reason(True, f"Spot price {'above' if up else 'below'} strike / moving with signal"))
    spread = f.get("spread")
    if spread is not None and spread <= 6:
        r.append(Reason(True, "Spread acceptable"))
    return r
