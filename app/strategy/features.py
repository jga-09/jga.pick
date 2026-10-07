"""Feature extraction from Kalshi market history (+ optional underlying feed).

Only features supported by real data are computed; anything else is ``None``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from app.data.underlying import SpotPrice
from app.kalshi.market_data import MarketSnapshot
from app.strategy.indicators import diffs, ema, linreg_slope, stdev


@dataclass(frozen=True)
class PricePoint:
    ts: datetime
    mid: float  # YES mid, cents


def _window(points: Sequence[PricePoint], now: datetime, seconds: float) -> list[PricePoint]:
    cutoff = now.timestamp() - seconds
    return [p for p in points if p.ts.timestamp() >= cutoff]


def _change(points: Sequence[PricePoint], now: datetime, seconds: float) -> float | None:
    win = _window(points, now, seconds)
    if len(win) < 2 or (win[-1].ts - win[0].ts).total_seconds() < seconds * 0.5:
        return None
    return win[-1].mid - win[0].mid


def compute_features(
    snap: MarketSnapshot,
    history: Sequence[PricePoint],
    now: datetime,
    spot: SpotPrice | None = None,
    spot_history: Sequence[SpotPrice] = (),
) -> dict[str, float | None]:
    f: dict[str, float | None] = {}
    mids = [p.mid for p in history]
    span = (history[-1].ts - history[0].ts).total_seconds() if len(history) >= 2 else 0.0
    f["history_sec"] = span
    f["yes_mid"] = snap.yes_mid
    f["spread"] = snap.spread
    f["time_remaining"] = snap.time_remaining(now)

    # Momentum / rate of change of the market-implied probability.
    f["mom_60s"] = _change(history, now, 60)
    f["mom_180s"] = _change(history, now, 180)
    base = _window(history, now, 60)
    f["roc_60s"] = (f["mom_60s"] / base[0].mid) if f["mom_60s"] is not None and base and base[0].mid else None

    # EMA trend and regression slope (cents / minute).
    recent = _window(history, now, 300)
    if len(recent) >= 6:
        vals = [p.mid for p in recent]
        f["ema_fast"] = ema(vals, 5)
        f["ema_slow"] = ema(vals, 20)
        f["ema_diff"] = (f["ema_fast"] or 0) - (f["ema_slow"] or 0)
    else:
        f["ema_fast"] = f["ema_slow"] = f["ema_diff"] = None
    w180 = _window(history, now, 180)
    if len(w180) >= 4:
        t0 = w180[0].ts.timestamp()
        slope = linreg_slope([(p.ts.timestamp() - t0) / 60 for p in w180], [p.mid for p in w180])
        f["slope_cpm"] = slope
    else:
        f["slope_cpm"] = None
    # Momentum acceleration: recent minute vs the prior two minutes' average pace.
    if f["mom_60s"] is not None and f["mom_180s"] is not None:
        f["accel"] = f["mom_60s"] - (f["mom_180s"] - f["mom_60s"]) / 2
    else:
        f["accel"] = None

    # Volatility of mid changes and recent range.
    f["volatility"] = stdev(diffs([p.mid for p in w180])) if len(w180) >= 5 else None
    f["range_180s"] = (max(p.mid for p in w180) - min(p.mid for p in w180)) if len(w180) >= 2 else None

    # Order-book imbalance (YES bids vs NO bids within 10c of each best).
    if snap.orderbook and (snap.orderbook.yes_bids or snap.orderbook.no_bids):
        yd, nd = snap.orderbook.depth("yes"), snap.orderbook.depth("no")
        f["book_yes_depth"], f["book_no_depth"] = yd, nd
        f["book_imbalance"] = (yd - nd) / (yd + nd) if yd + nd > 0 else None
    else:
        f["book_yes_depth"] = f["book_no_depth"] = f["book_imbalance"] = None

    # Recent trade flow: taker YES vs taker NO volume.
    cutoff = now.timestamp() - 120
    trades = [t for t in snap.recent_trades if t.ts.timestamp() >= cutoff]
    yes_v = sum(t.count for t in trades if t.taker_side == "yes")
    no_v = sum(t.count for t in trades if t.taker_side == "no")
    f["trade_count_120s"] = float(len(trades))
    f["trade_flow"] = (yes_v - no_v) / (yes_v + no_v) if yes_v + no_v > 0 else None
    v60 = sum(t.count for t in snap.recent_trades if t.ts.timestamp() >= now.timestamp() - 60)
    v_prior = sum(t.count for t in snap.recent_trades
                  if now.timestamp() - 180 <= t.ts.timestamp() < now.timestamp() - 60)
    f["volume_accel"] = (v60 / (v_prior / 2)) if v_prior > 0 else None
    f["volume"] = snap.volume

    # Underlying spot vs strike (only with a real feed and a numeric strike).
    strike = snap.info.floor_strike if snap.info.floor_strike is not None else snap.info.cap_strike
    if spot is not None and strike:
        f["spot"] = spot.price
        f["strike_dist_pct"] = (spot.price - strike) / strike * 100
    else:
        f["spot"] = f["strike_dist_pct"] = None
    hist = [s for s in spot_history if s.ts.timestamp() >= now.timestamp() - 60]
    if len(hist) >= 2 and hist[0].price:
        f["spot_mom_pct"] = (hist[-1].price - hist[0].price) / hist[0].price * 100
    else:
        f["spot_mom_pct"] = None
    return f
