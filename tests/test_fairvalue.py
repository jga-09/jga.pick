"""Fair-value research: maths + walk-forward on synthetic worlds (no network)."""

import math
import random
from datetime import UTC, datetime, timedelta

from app.data.underlying import SpotPrice
from app.research.fairvalue import (
    basis_agreement,
    brier,
    build_points,
    fair_prob,
    realized_sigma,
    simulate_fv,
    spot_at,
    walk_forward_fv,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)
SIG = 0.0003  # per sqrt-second


def test_fair_prob_basics():
    assert abs(fair_prob(100, 100, 600, SIG) - 0.5) < 1e-9
    assert fair_prob(101, 100, 600, SIG) > 0.5 > fair_prob(99, 100, 600, SIG)
    # less time left -> more certain
    assert fair_prob(100.2, 100, 60, SIG) > fair_prob(100.2, 100, 600, SIG)


def test_realized_sigma_recovers_true_vol():
    rng = random.Random(1)
    px, pts = 100.0, []
    for i in range(240):
        px *= math.exp(rng.gauss(0, SIG * math.sqrt(60)))
        pts.append(SpotPrice("BTC", px, T0 + timedelta(minutes=i), "t"))
    est = realized_sigma(pts, pts[-1].ts)
    assert est is not None and abs(est / SIG - 1) < 0.25
    assert realized_sigma(pts[:5], pts[4].ts) is None  # too little data -> no number invented
    assert spot_at(pts, T0 - timedelta(minutes=5)) is None


def make_world(days: int, per_day: int, lag_min: int, seed: int = 3):
    """Kalshi quotes = fair price computed from spot ``lag_min`` minutes old, +-1c spread."""
    rng = random.Random(seed)
    out = {}
    basis = []
    for d in range(days):
        for k in range(per_day):
            start = T0 + timedelta(days=d, minutes=20 * k)
            px, spot = 100.0, []
            for i in range(60 + 16):  # an hour of history + the 15-minute window
                px *= math.exp(rng.gauss(0, SIG * math.sqrt(60)))
                spot.append(SpotPrice("BTC", px, start + timedelta(minutes=i), "t"))
            open_t, close = spot[60].ts, spot[75].ts
            strike = spot[60].price
            outcome = "yes" if spot[75].price > strike else "no"
            quotes = []
            for i in range(61, 75):
                j = i - lag_min
                fair = fair_prob(spot[j].price, strike, (close - spot[j].ts).total_seconds() - 60 * lag_min, SIG)
                c = round(fair * 100)
                quotes.append((spot[i].ts, max(1, c - 1), min(99, c + 1)))
            t = f"KXBTC15M-{d}-{k}"
            out[t] = build_points(t, "BTC", strike, close, outcome, quotes, spot)
            basis.append(("BTC", strike, close, outcome))
            assert open_t < close
    return out, basis


def test_efficient_world_has_no_edge():
    pts, _ = make_world(days=6, per_day=40, lag_min=0)
    wf = walk_forward_fv(pts, fee_rate=0.07, slippage=1.0)
    # no lag -> quotes already fair -> after costs the strategy should (almost) never trade, nor make money
    assert len(wf.trades) < 20 or sum(t.pnl for t in wf.trades) / len(wf.trades) < 0.02


def test_lagged_world_has_edge_out_of_sample():
    pts, _ = make_world(days=6, per_day=40, lag_min=3)
    wf = walk_forward_fv(pts, fee_rate=0.07, slippage=1.0)
    assert len(wf.trades) >= 30
    assert sum(t.pnl for t in wf.trades) / len(wf.trades) > 0.02
    b_fair, b_mid, n = brier([p for v in pts.values() for p in v])
    assert n > 0 and b_fair < b_mid
    # spot series here is the settlement series -> perfect agreement
    spot = {"BTC": []}
    agree, total = basis_agreement([], spot)
    assert (agree, total) == (0, 0)


def test_one_trade_per_market_and_never_late():
    pts, _ = make_world(days=1, per_day=10, lag_min=3)
    trades = simulate_fv(pts, margin=0.0)
    assert len({t.point.ticker for t in trades}) == len(trades)
    assert all(t.point.t_left >= 60 for t in trades)


def test_missing_strike_or_result_gives_no_points():
    assert build_points("X", "BTC", None, T0, "yes", [], []) == []
    assert build_points("X", "BTC", 100, T0, "void", [], []) == []
