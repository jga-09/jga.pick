"""Phase 4/7: indicators are causal, splits never leak, calibration works, models are judged after fees."""

import math
import random
from datetime import UTC, datetime, timedelta
from statistics import NormalDist

import pytest

from app.research import ta
from app.research.modeling import (
    FairModel,
    HistModel,
    MarketModel,
    Row,
    build_rows,
    evaluate_asset,
    fee_cents,
    isotonic,
    pick_trades,
    platt,
    prob_metrics,
    verdict,
    weights,
)

ND = NormalDist()
T0 = datetime(2026, 9, 1, tzinfo=UTC)
SIG = 0.0007


def world(days=16, lag=0, seed=5):
    """Synthetic minute bars + 15-minute markets. Kalshi prices the fair value of spot ``lag`` minutes ago."""
    rng = random.Random(seed)
    n = days * 1440
    px, bars = 60000.0, []
    for i in range(n):
        o = px
        px *= math.exp(rng.gauss(0, SIG))
        bars.append(ta.Bar(T0.timestamp() + 60 * (i + 1), max(o, px), min(o, px), px, rng.uniform(1, 9)))
    markets = []
    for s in range(120, n - 16, 15):
        k = bars[s - 1].close
        close = T0 + timedelta(minutes=s + 15)
        cs = []
        for m in range(1, 16):
            j = max(s - 1, s + m - 1 - lag)
            left = (s + 15) - (j + 1)
            f = ND.cdf(math.log(bars[j].close / k) / (SIG * math.sqrt(max(left, 0.5))))
            c = min(97, max(3, round(f * 100)))
            cs.append((T0 + timedelta(minutes=s + m), c - 1, c + 1, 5.0))
        markets.append({"ticker": f"KXBTC15M-{s}", "close": close, "strike": k,
                        "result": "yes" if bars[s + 14].close > k else "no", "candles": cs})
    return bars, markets


def test_indicators_are_causal():
    bars, _ = world(days=1)
    full = ta.compute(bars)
    cut = ta.compute(bars[:800])
    for name in ("ema9", "rsi14", "macd", "bb", "atr14", "vwap_z", "vol_accel", "rv60"):
        a, b = getattr(full, name)[:800], getattr(cut, name)
        assert a == b, name  # future bars never change a past value
    assert all(v is None or 0 <= v <= 100 for v in full.rsi14)
    nov = [ta.Bar(b.end, b.high, b.low, b.close, None) for b in bars[:200]]
    assert ta.compute(nov).vwap_z == [None] * 200  # no volume -> no VWAP, never invented


def test_rows_use_only_past_bars_and_keep_markets_together():
    bars, markets = world(days=2)
    ind = ta.compute(bars)
    rows, _ = build_rows("BTC", markets, ind)
    assert rows and all(2 <= r.t_left <= 12 for r in rows)
    for r in rows[:200]:
        i = max(j for j, e in enumerate(ind.ends) if e <= r.ts.timestamp())
        assert ind.bars[i].end <= r.ts.timestamp()
    w = weights(rows)
    per = {}
    for r, wi in zip(rows, w, strict=True):
        per[r.ticker] = per.get(r.ticker, 0) + wi
    assert all(v == pytest.approx(1.0) for v in per.values())  # each market counts once


def test_exact_fee_rounds_up_per_order():
    assert fee_cents(50, 1, 0.07) == pytest.approx(2.0)  # ceil(1.75c)
    assert fee_cents(50, 10, 0.07) == pytest.approx(1.8)  # ceil(17.5c)/10
    assert fee_cents(95, 1, 0.07) == pytest.approx(1.0)


def test_platt_and_isotonic_fix_miscalibration():
    rng = random.Random(3)
    p = [rng.random() for _ in range(4000)]
    y = [int(rng.random() < 0.5 + (x - 0.5) * 0.4) for x in p]  # true prob is much flatter than p
    w = [1.0] * len(p)
    for cal in (platt(p, y, w), isotonic(p, y, w)):
        assert 0.25 < cal(0.05) < 0.45 and 0.55 < cal(0.95) < 0.75


def test_fair_world_has_no_edge_lag_world_does():
    bars, markets = world(lag=0)
    rows, _ = build_rows("BTC", markets, ta.compute(bars))
    fair = evaluate_asset("BTC", rows, holdout_days=4, min_train_days=4, model_names=("MARKET", "FAIR", "HIST"))
    assert fair.holdout["MARKET"]["trades"]["trades"] == 0  # the market never beats its own ask
    assert not verdict(fair.dev["FAIR"], fair.holdout["FAIR"]).startswith("PASSES")
    bars, markets = world(lag=3)
    rows, _ = build_rows("BTC", markets, ta.compute(bars))
    lag = evaluate_asset("BTC", rows, holdout_days=4, min_train_days=4, model_names=("MARKET", "FAIR"))
    assert lag.holdout["FAIR"]["prob"]["brier"] < lag.holdout["FAIR"]["prob"]["brier_market"]
    assert verdict(lag.dev["FAIR"], lag.holdout["FAIR"]).startswith("PASSES")


def test_walk_forward_trains_only_on_markets_closed_before_the_test_day(monkeypatch):
    seen = []
    orig = HistModel.fit

    def spy(self, rows, feats):
        seen.append(max(r.close for r in rows))
        return orig(self, rows, feats)

    monkeypatch.setattr(HistModel, "fit", spy)
    bars, markets = world(days=12)
    rows, _ = build_rows("BTC", markets, ta.compute(bars))
    orig_pred = HistModel.predict
    checks = []

    def pred_spy(self, rs):
        if rs:
            checks.append((seen[-1], min(r.close for r in rs), min(r.ts for r in rs)))
        return orig_pred(self, rs)

    monkeypatch.setattr(HistModel, "predict", pred_spy)
    evaluate_asset("BTC", rows, holdout_days=3, min_train_days=4, model_names=("HIST",))
    assert checks and all(train_max <= test_close for train_max, test_close, _ in checks)


def test_insufficient_data_is_reported_not_faked():
    bars, markets = world(days=3)
    rows, _ = build_rows("BTC", markets, ta.compute(bars))
    res = evaluate_asset("BTC", rows, holdout_days=7)
    assert res.note.startswith("INSUFFICIENT DATA") and not res.holdout
    assert verdict(None, None) == "INSUFFICIENT DATA"


def test_pick_trades_one_per_market_and_respects_spread():
    r = Row("T", "BTC", T0, T0 - timedelta(minutes=5), 5.0, 40, 42, 1, {"k_mid": 0.41, "rv60": 5.0})
    wide = Row("U", "BTC", T0, T0 - timedelta(minutes=5), 5.0, 30, 42, 1, {"k_mid": 0.36, "rv60": 5.0})
    tr = pick_trades([r, r, wide], [0.9, 0.9, 0.9], min_ev=2, fee_rate=0.07, slippage=1, order_size=1,
                     max_spread=5, regime=lambda x: "n")
    assert [t.ticker for t in tr] == ["T"] and tr[0].pnl == pytest.approx((100 - 42 - 2 - 1) / 100)


def test_benchmarks_predict_sensible_probabilities():
    bars, markets = world(days=2)
    rows, _ = build_rows("BTC", markets, ta.compute(bars))
    for m in (MarketModel(), FairModel(), HistModel().fit(rows, [])):
        p = m.predict(rows)
        assert all(0 <= x <= 1 for x in p)
        assert prob_metrics(rows, p)["n_markets"] > 0
