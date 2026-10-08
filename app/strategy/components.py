"""Component scores A-J.

Directional components are signed in [-1, 1] (positive = YES/UP).
Quality components are in [0, 1] (higher = better trading conditions).
"""

from __future__ import annotations

from app.strategy.config import StrategyConfig
from app.strategy.indicators import clamp, squash

DIRECTIONAL = ("momentum", "trend", "orderbook", "volume", "prob_move", "underlying", "acceleration")
QUALITY = ("liquidity", "volatility", "stability", "time")


def directional_components(f: dict[str, float | None]) -> dict[str, float]:
    """Moves are measured in normalised (probit) units where available, else in cents."""
    c: dict[str, float] = {}
    z60 = f.get("zmove_60s")
    if z60 is not None:  # A. price momentum: a 1.5-sigma minute is strong
        c["momentum"] = squash(z60, 1.5)
    elif f.get("mom_60s") is not None:
        c["momentum"] = squash(f["mom_60s"], 3.0)
    z180, eff = f.get("zmove_180s"), f.get("efficiency")
    if z180 is not None:  # B. trend: sustained, efficient 3-minute move
        c["trend"] = squash(z180, 1.5) * (0.5 + 0.5 * (eff if eff is not None else 0.5))
    elif f.get("slope_cpm") is not None:
        c["trend"] = squash(f["slope_cpm"], 1.5)
    if f.get("book_imbalance") is not None:  # C. order book
        ob = 0.6 * f["book_imbalance"] + 0.4 * (f.get("book_near_imbalance") or f["book_imbalance"])
        if f.get("book_imbalance_delta") is not None:
            ob += 0.25 * squash(f["book_imbalance_delta"], 0.3)
        c["orderbook"] = clamp(ob * 1.2)
    if f.get("trade_flow") is not None:  # D. volume / aggressive flow
        intensity = min(1.0, (f.get("trade_count_120s") or 0) / 5)
        accel = f.get("volume_accel")
        boost = 0.75 + 0.25 * min(1.0, accel / 2) if accel is not None else 0.85
        c["volume"] = clamp(f["trade_flow"] * intensity * boost)
    z300 = f.get("zmove_300s")
    if z300 is not None:  # F. market-probability movement over 5 min
        c["prob_move"] = squash(z300, 1.5)
    elif f.get("prob_move_300s") is not None:
        c["prob_move"] = squash(f["prob_move_300s"], 6.0)
    und = []
    if f.get("strike_dist_pct") is not None:
        und.append(squash(f["strike_dist_pct"], 0.10))
    if f.get("spot_mom_pct") is not None:
        und.append(squash(f["spot_mom_pct"], 0.05))
    if und:  # H. underlying
        c["underlying"] = sum(und) / len(und)
    if f.get("accel_z") is not None:  # I. acceleration
        c["acceleration"] = squash(f["accel_z"], 1.5)
    elif f.get("accel") is not None:
        c["acceleration"] = squash(f["accel"], 1.5)
    return c


def quality_components(f: dict[str, float | None], stability: float | None, side: str | None,
                       cfg: StrategyConfig) -> dict[str, float]:
    q: dict[str, float] = {}
    spread = f.get("spread")
    if spread is not None:  # J. liquidity / spread
        spread_q = 1.0 if spread <= 2 else max(0.0, 1 - (spread - 2) / max(1.0, cfg.max_spread_cents - 2))
        liq = f.get(f"ask_liquidity_{side}") if side else None
        depth_q = min(1.0, liq / 50) if liq is not None else 0.5
        q["liquidity"] = 0.6 * spread_q + 0.4 * depth_q
    vr = f.get("vol_ratio")
    if vr is not None:  # E. volatility relative to what prices imply: fine up to vol_high
        span = max(0.1, cfg.vol_extreme - cfg.vol_high)
        q["volatility"] = 1.0 if vr <= cfg.vol_high else max(0.0, 1 - (vr - cfg.vol_high) / span)
    if stability is not None:
        q["stability"] = stability
    tr = f.get("time_remaining")
    if tr is not None:  # G. time remaining (a-priori shape; research decides the real best window)
        if tr < 60:
            q["time"] = 0.0
        elif tr < 180:
            q["time"] = (tr - 60) / 120
        elif tr <= 720:
            q["time"] = 1.0
        else:
            q["time"] = 0.7
    return q
