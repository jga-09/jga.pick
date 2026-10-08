"""Signal Quality score, no-trade filter and setup grade.

Confidence answers "how strongly does the data lean one way?".
Signal Quality answers "how good is this setup to actually trade?" - it rewards
agreement between independent components, penalises conflicts, and scores
execution conditions (liquidity, volatility, stability, timing).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.strategy.components import DIRECTIONAL, quality_components
from app.strategy.config import SOFT_FILTERS, StrategyConfig
from app.strategy.regime import Regime
from app.strategy.stability import Stability

GRADES = ("A+", "A", "B", "C")
NO_TRADE = "NO_TRADE"
TIME_BUCKETS = ("12-15", "9-12", "6-9", "3-6", "1-3", "<1")

FILTER_TEXT = {
    "conflict": "Conflicting indicators",
    "weak_momentum": "Momentum weak",
    "low_volume": "Volume below threshold",
    "abnormal_spread": "Spread abnormal",
    "low_liquidity": "Insufficient liquidity",
    "sideways": "Market moving sideways",
    "extreme_volatility": "Extreme volatility",
    "too_little_time": "Too little time remaining",
    "flip_flop": "Signal changing direction repeatedly",
    "coin_flip": "Price too close to the strike (coin flip)",
    "underlying_conflict": "Underlying asset disagrees",
    "weak_book": "Order-book imbalance too weak",
    "extended_move": "Move already extended",
    "adverse_selection": "Sharp spike - adverse selection risk",
    "decelerating": "Momentum decelerating",
    "high_volatility": "High volatility",
    "low_stability": "Signal not yet stable",
    "underlying_unavailable": "Underlying feed unavailable",
    "divergence": "Underlying / Kalshi divergence",
}


def time_bucket(seconds: float) -> str:
    m = seconds / 60
    if m >= 12:
        return "12-15"
    if m >= 9:
        return "9-12"
    if m >= 6:
        return "6-9"
    if m >= 3:
        return "3-6"
    if m >= 1:
        return "1-3"
    return "<1"


def price_bucket(cents: float | None) -> str:
    if cents is None:
        return "n/a"
    for lo, hi in ((0, 50), (50, 55), (55, 60), (60, 65), (65, 70), (70, 75), (75, 80), (80, 90)):
        if cents < hi:
            return f"<{hi}" if lo == 0 else f"{lo}-{hi}"
    return "90+"


@dataclass(frozen=True)
class SetupAnalysis:
    direction: str  # UP / DOWN / WAIT (the leaning being graded)
    quality: int
    grade: str  # A+ / A / B / C / NO_TRADE
    points: dict[str, float]  # signed point contribution per component
    max_points: dict[str, float]
    components: dict[str, float]  # directional (YES-signed) + quality (0..1)
    regime: Regime
    accel_state: str  # ACCELERATING / STEADY / DECELERATING / n/a
    underlying_state: str  # CONFIRMED / CONFLICT / NEUTRAL / UNAVAILABLE
    divergence: bool
    stability: Stability
    hard_flags: tuple[str, ...]
    soft_flags: tuple[str, ...]
    time_bucket: str
    entry_price: float | None
    extended: bool = False
    notes: dict[str, float] = field(default_factory=dict)

    @property
    def tradeable(self) -> bool:
        return self.grade != NO_TRADE

    @property
    def reasons(self) -> list[str]:
        return [FILTER_TEXT.get(f, f) for f in (*self.hard_flags, *self.soft_flags)]


def _aligned(c: dict[str, float], name: str, sign: int) -> float | None:
    v = c.get(name)
    return None if v is None else v * sign


def analyze(
    f: dict[str, float | None],
    directional: dict[str, float],
    leaning: str,
    stability: Stability,
    regime: Regime,
    cfg: StrategyConfig,
    entry_price: float | None,
) -> SetupAnalysis:
    sign = 1 if leaning == "UP" else -1 if leaning == "DOWN" else 0
    side = {"UP": "yes", "DOWN": "no"}.get(leaning)
    qc = quality_components(f, stability.score, side, cfg)
    comps = {**directional, **qc}

    # ---- points -------------------------------------------------------
    dp = cfg.directional_points
    avail = [k for k in DIRECTIONAL if k in directional and dp.get(k, 0) > 0]
    total_dir = sum(dp.get(k, 0) for k in DIRECTIONAL)
    scale = (total_dir / sum(dp[k] for k in avail)) if avail and cfg.renormalize_missing else 1.0
    points: dict[str, float] = {}
    max_points: dict[str, float] = {}
    for k in avail:
        a = directional[k] * sign
        w = dp[k] * scale
        max_points[k] = w
        points[k] = w * max(0.0, a) - w * cfg.conflict_penalty * max(0.0, -a) if sign else 0.0
    for k, w in cfg.quality_points.items():
        max_points[k] = w
        points[k] = w * qc[k] if k in qc else 0.0
    quality = int(round(max(0.0, min(100.0, sum(points.values()))))) if sign else 0

    # ---- states ---------------------------------------------------------
    acc = f.get("accel_z") if f.get("accel_z") is not None else f.get("accel")
    if acc is None or sign == 0:
        accel_state = "n/a"
    elif acc * sign > 0.3:
        accel_state = "ACCELERATING"
    elif acc * sign < -0.3:
        accel_state = "DECELERATING"
    else:
        accel_state = "STEADY"
    und = _aligned(directional, "underlying", sign)
    if und is None:
        underlying_state = "UNAVAILABLE"
    elif und >= 0.2:
        underlying_state = "CONFIRMED"
    elif und <= -0.2:
        underlying_state = "CONFLICT"
    else:
        underlying_state = "NEUTRAL"
    spot_mom, prob_move = f.get("spot_mom_pct"), f.get("mom_60s")
    divergence = bool(spot_mom is not None and prob_move is not None and abs(spot_mom) >= 0.03
                      and abs(prob_move) >= 1.0 and spot_mom * prob_move < 0)

    # ---- no-trade filter -----------------------------------------------
    flags: list[str] = []
    if sign == 0:
        flags.append("weak_momentum")
    else:
        against = sum(1 for k in avail if directional[k] * sign <= -cfg.conflict_level)
        if against >= cfg.conflict_count:
            flags.append("conflict")
        mom = _aligned(directional, "momentum", sign)
        if mom is None or mom < cfg.min_momentum:
            flags.append("weak_momentum")
    if (f.get("trade_count_120s") or 0) < cfg.min_trades_2m:
        flags.append("low_volume")
    spread = f.get("spread")
    if spread is None or spread > cfg.max_spread_cents:
        flags.append("abnormal_spread")
    liq = f.get(f"ask_liquidity_{side}") if side else None
    if liq is not None and liq < cfg.min_ask_liquidity:
        flags.append("low_liquidity")
    if regime is Regime.SIDEWAYS:
        flags.append("sideways")
    if regime is Regime.CHAOTIC or (f.get("vol_ratio") or 0) >= cfg.vol_extreme:
        flags.append("extreme_volatility")
    elif regime is Regime.HIGH_VOLATILITY:
        flags.append("high_volatility")
    if regime is Regime.LOW_LIQUIDITY and "low_liquidity" not in flags:
        flags.append("low_liquidity")
    tr = f.get("time_remaining") or 0
    if tr < cfg.min_time_sec:
        flags.append("too_little_time")
    if stability.flips >= cfg.max_flips:
        flags.append("flip_flop")
    if stability.score is None or stability.score < cfg.min_stability:
        flags.append("low_stability")
    dist, mid = f.get("strike_dist_pct"), f.get("yes_mid")
    if tr < cfg.coin_flip_time_sec and (
        (dist is not None and abs(dist) < cfg.coin_flip_strike_pct)
        or (mid is not None and abs(mid - 50) < cfg.coin_flip_mid_band)
    ):
        flags.append("coin_flip")
    if underlying_state == "CONFLICT":
        flags.append("underlying_conflict")
    elif underlying_state == "UNAVAILABLE":
        flags.append("underlying_unavailable")
    if divergence:
        flags.append("divergence")
    book = _aligned(directional, "orderbook", sign)
    if book is not None and book < cfg.weak_book_level:
        flags.append("weak_book")
    move = f.get("zmove_300s")
    extended = bool(sign and ((move is not None and move * sign >= cfg.extended_z)
                              or (entry_price is not None and entry_price >= cfg.extended_price)))
    if extended:
        flags.append("extended_move")
    spike = f.get("zmove_30s")
    if sign and spike is not None and spike * sign >= cfg.adverse_spike_z:
        flags.append("adverse_selection")
    if accel_state == "DECELERATING":
        flags.append("decelerating")

    flags = [x for x in dict.fromkeys(flags) if cfg.enabled(x)]
    soft = tuple(x for x in flags if x in SOFT_FILTERS)
    hard = tuple(x for x in flags if x not in SOFT_FILTERS)

    if hard or sign == 0:
        grade = NO_TRADE
    elif quality >= cfg.grade_a_plus and not soft:
        grade = "A+"
    elif quality >= cfg.grade_a:
        grade = "A"
    elif quality >= cfg.grade_b:
        grade = "B"
    elif quality >= cfg.grade_c:
        grade = "C"
    else:
        grade = NO_TRADE
    return SetupAnalysis(
        direction=leaning, quality=quality, grade=grade, points={k: round(v, 1) for k, v in points.items()},
        max_points={k: round(v, 1) for k, v in max_points.items()}, components=comps, regime=regime,
        accel_state=accel_state, underlying_state=underlying_state, divergence=divergence,
        stability=stability, hard_flags=hard, soft_flags=soft, time_bucket=time_bucket(tr),
        entry_price=entry_price, extended=extended,
    )
