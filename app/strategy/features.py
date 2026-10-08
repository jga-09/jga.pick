"""Feature extraction from Kalshi market history (+ optional underlying feed).

Only features supported by real data are computed; anything else is ``None``.
All directional features are expressed from the YES side (positive = YES/UP).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from statistics import NormalDist
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


def _slope_cpm(points: Sequence[PricePoint]) -> float | None:
    if len(points) < 3:
        return None
    t0 = points[0].ts.timestamp()
    return linreg_slope([(p.ts.timestamp() - t0) / 60 for p in points], [p.mid for p in points])


_ND = NormalDist()


def probit(mid_cents: float) -> float:
    """Probability -> standard-normal quantile (clipped away from 0/100)."""
    return _ND.inv_cdf(min(0.98, max(0.02, mid_cents / 100)))


def _zmove(points: Sequence[PricePoint], now: datetime, seconds: float, remaining: float) -> float | None:
    """Probit move over ``seconds`` in units of the move a fairly-priced binary would
    typically make over that span with ``remaining`` seconds left (~N(0,1) if efficient)."""
    win = _window(points, now, seconds)
    if len(win) < 2 or (win[-1].ts - win[0].ts).total_seconds() < seconds * 0.5:
        return None
    span = (win[-1].ts - win[0].ts).total_seconds()
    return (probit(win[-1].mid) - probit(win[0].mid)) / math.sqrt(span / max(remaining, 30.0))


def _vol_ratio(points: Sequence[PricePoint], remaining: float) -> float | None:
    """Realised probit volatility relative to what the price itself implies (~1 if consistent)."""
    if len(points) < 5:
        return None
    rates = []
    for a, b in zip(points, points[1:], strict=False):
        dt = (b.ts - a.ts).total_seconds()
        if dt > 0:
            rates.append((probit(b.mid) - probit(a.mid)) / math.sqrt(dt))
    if len(rates) < 4:
        return None
    m = sum(rates) / len(rates)
    sd = math.sqrt(sum((r - m) ** 2 for r in rates) / (len(rates) - 1))
    return sd * math.sqrt(max(remaining, 30.0))


def _spot_change_pct(hist: Sequence[SpotPrice], now: datetime, seconds: float) -> float | None:
    win = [s for s in hist if s.ts.timestamp() >= now.timestamp() - seconds]
    if len(win) < 2 or not win[0].price or (win[-1].ts - win[0].ts).total_seconds() < seconds * 0.5:
        return None
    return (win[-1].price - win[0].price) / win[0].price * 100


def compute_features(
    snap: MarketSnapshot,
    history: Sequence[PricePoint],
    now: datetime,
    spot: SpotPrice | None = None,
    spot_history: Sequence[SpotPrice] = (),
    book_history: Sequence[tuple[datetime, float]] = (),
) -> dict[str, float | None]:
    f: dict[str, float | None] = {}
    span = (history[-1].ts - history[0].ts).total_seconds() if len(history) >= 2 else 0.0
    f["history_sec"] = span
    f["yes_mid"] = snap.yes_mid
    f["spread"] = snap.spread
    f["time_remaining"] = snap.time_remaining(now)

    # --- A/F. Momentum and probability movement --------------------------
    f["mom_30s"] = _change(history, now, 30)
    f["mom_60s"] = _change(history, now, 60)
    f["mom_180s"] = _change(history, now, 180)
    f["prob_move_300s"] = _change(history, now, 300)
    base = _window(history, now, 60)
    f["roc_60s"] = (f["mom_60s"] / base[0].mid) if f["mom_60s"] is not None and base and base[0].mid else None
    # Move since the earliest point we have in this market window (extended-move filter).
    f["move_since_start"] = (history[-1].mid - history[0].mid) if len(history) >= 2 else None

    # Scale-free (normalised) moves: binary prices swing more as expiry approaches,
    # so raw cents are not comparable across time; these are ~N(0,1) under fair pricing.
    rem = f["time_remaining"] or 0.0
    f["zmove_30s"] = _zmove(history, now, 30, rem)
    f["zmove_60s"] = _zmove(history, now, 60, rem)
    f["zmove_180s"] = _zmove(history, now, 180, rem)
    f["zmove_300s"] = _zmove(history, now, 300, rem)

    # --- B. Trend -----------------------------------------------------------
    recent = _window(history, now, 300)
    if len(recent) >= 6:
        vals = [p.mid for p in recent]
        f["ema_fast"], f["ema_slow"] = ema(vals, 5), ema(vals, 20)
        f["ema_diff"] = (f["ema_fast"] or 0) - (f["ema_slow"] or 0)
    else:
        f["ema_fast"] = f["ema_slow"] = f["ema_diff"] = None
    w180 = _window(history, now, 180)
    f["slope_cpm"] = _slope_cpm(w180) if len(w180) >= 4 else None

    # --- I. Acceleration: last-60s slope vs the 120s before it -------------
    w60 = _window(history, now, 60)
    prior = [p for p in w180 if p.ts.timestamp() < now.timestamp() - 60]
    f["slope_60s"] = _slope_cpm(w60) if len(w60) >= 3 else None
    f["slope_prior"] = _slope_cpm(prior) if len(prior) >= 3 else None
    if f["zmove_60s"] is not None and f["zmove_180s"] is not None:
        # Velocity (probit units / second) over the last 60s vs the preceding 120s, divided by
        # the noise of that difference for a fair binary (per-second sd = 1/sqrt(remaining)).
        r = max(rem, 30.0)
        dz60 = f["zmove_60s"] * math.sqrt(60 / r)
        dz180 = f["zmove_180s"] * math.sqrt(180 / r)
        v_recent, v_prior = dz60 / 60, (dz180 - dz60) / 120
        noise = (1 / math.sqrt(r)) * math.sqrt(1 / 60 + 1 / 120)
        f["accel_z"] = (v_recent - v_prior) / noise
    else:
        f["accel_z"] = None
    if f["slope_60s"] is not None and f["slope_prior"] is not None:
        f["accel"] = f["slope_60s"] - f["slope_prior"]
    elif f["mom_60s"] is not None and f["mom_180s"] is not None:
        f["accel"] = f["mom_60s"] - (f["mom_180s"] - f["mom_60s"]) / 2
    else:
        f["accel"] = None

    # --- E. Volatility, range and trend efficiency -------------------------
    d180 = diffs([p.mid for p in w180])
    f["volatility"] = stdev(d180) if len(w180) >= 5 else None
    f["vol_ratio"] = _vol_ratio(w180, rem)
    f["range_180s"] = (max(p.mid for p in w180) - min(p.mid for p in w180)) if len(w180) >= 2 else None
    path = sum(abs(d) for d in d180)
    f["efficiency"] = (abs(w180[-1].mid - w180[0].mid) / path) if len(w180) >= 5 and path > 0 else None
    if len(w180) >= 5 and path == 0:
        f["efficiency"] = 0.0

    # --- C. Order book -------------------------------------------------------
    book = snap.orderbook
    if book and (book.yes_bids or book.no_bids):
        yd, nd = book.depth("yes"), book.depth("no")  # within 10c of each best
        yn, nn = book.depth("yes", 3), book.depth("no", 3)  # near the touch
        f["book_yes_depth"], f["book_no_depth"] = yd, nd
        f["book_imbalance"] = (yd - nd) / (yd + nd) if yd + nd > 0 else None
        f["book_near_imbalance"] = (yn - nn) / (yn + nn) if yn + nn > 0 else None
        f["book_total_depth"] = yd + nd
    else:
        for k in ("book_yes_depth", "book_no_depth", "book_imbalance", "book_near_imbalance", "book_total_depth"):
            f[k] = None
    old = [v for ts, v in book_history if now.timestamp() - 90 <= ts.timestamp() <= now.timestamp() - 30]
    f["book_imbalance_delta"] = (f["book_imbalance"] - old[0]) if old and f["book_imbalance"] is not None else None
    f["ask_liquidity_yes"] = snap.ask_liquidity("yes") if book else None
    f["ask_liquidity_no"] = snap.ask_liquidity("no") if book else None

    # --- D. Volume / aggressive flow -----------------------------------------
    ts_now = now.timestamp()
    trades = [t for t in snap.recent_trades if t.ts.timestamp() >= ts_now - 120]
    yes_v = sum(t.count for t in trades if t.taker_side == "yes")
    no_v = sum(t.count for t in trades if t.taker_side == "no")
    f["aggr_buy_yes"], f["aggr_buy_no"] = yes_v, no_v
    f["trade_count_120s"] = float(len(trades))
    f["trade_flow"] = (yes_v - no_v) / (yes_v + no_v) if yes_v + no_v > 0 else None
    v60 = sum(t.count for t in snap.recent_trades if t.ts.timestamp() >= ts_now - 60)
    v_prior = sum(t.count for t in snap.recent_trades if ts_now - 180 <= t.ts.timestamp() < ts_now - 60)
    f["volume_60s"] = v60
    f["volume_accel"] = (v60 / (v_prior / 2)) if v_prior > 0 else None
    f["volume"] = snap.volume

    # --- H. Underlying spot vs strike (only with a real feed) ---------------
    strike = snap.info.floor_strike if snap.info.floor_strike is not None else snap.info.cap_strike
    f["spot"] = spot.price if spot is not None else None
    f["strike_dist_pct"] = (spot.price - strike) / strike * 100 if spot is not None and strike else None
    f["spot_mom_pct"] = _spot_change_pct(spot_history, now, 60)
    f["spot_mom_180_pct"] = _spot_change_pct(spot_history, now, 180)
    return f
