import random
from datetime import UTC, datetime, timedelta

import pytest

from app.research.adaptive import assess
from app.research.backtest import (
    STRATEGIES,
    BacktestConfig,
    Strategy,
    chunk_markets,
    filter_ablation,
    simulate,
    walk_forward,
)
from app.research.calibration import HistoricalModel
from app.research.dataset import Observation
from app.research.importance import feature_importance
from app.research.stats import auc, trade_metrics, wilson
from app.research.tradeanalysis import analyze_trade
from app.strategy.config import StrategyConfig
from app.strategy.quality import analyze, price_bucket, time_bucket
from app.strategy.regime import Regime, classify
from app.strategy.stability import Stability, StabilityTracker

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def obs(i, *, minute=7, direction="UP", outcome="yes", price=60.0, grade="B", regime="STRONG_TREND",
        quality=80, flags=(), comps=None, confidence=80) -> Observation:
    close = T0 + timedelta(minutes=15 * (i + 1))
    yes_ask, no_ask = (price, 100 - price + 2) if direction == "UP" else (100 - price + 2, price)
    return Observation(
        ts=close - timedelta(minutes=minute), ticker=f"M{i:05d}", asset="BTC", close_time=close, minute=minute,
        time_remaining=minute * 60 + 30, direction=direction, confidence=confidence, quality=quality,
        grade=grade, regime=regime, accel_state="STEADY", underlying_state="CONFIRMED", stability=0.9,
        yes_ask=yes_ask, no_ask=no_ask, spread=2, hard_flags=list(flags), soft_flags=[],
        components=comps or {}, outcome=outcome,
    )


# ------------------------------------------------------------------- stats
def test_wilson_and_auc():
    lo, hi = wilson(50, 100)
    assert 0.40 < lo < 0.5 < hi < 0.60
    assert wilson(0, 0) == (0.0, 1.0)
    assert auc([1, 2, 3, 4], [0, 0, 1, 1]) == 1.0
    assert auc([4, 3, 2, 1], [0, 0, 1, 1]) == 0.0
    assert auc([1, 1, 1, 1], [0, 1, 0, 1]) == 0.5


def test_trade_metrics():
    m = trade_metrics([0.4, -0.6, -0.6, 0.4, 0.4])
    assert m.trades == 5 and m.wins == 3 and m.win_rate == 0.6
    assert m.profit_factor == pytest.approx(1.2 / 1.2)
    assert m.max_drawdown == pytest.approx(1.2) and m.max_losing_streak == 2


# ---------------------------------------------------------- historical model
def test_model_counts_each_market_once_and_reports_insufficient():
    many_obs_one_market = [obs(0, minute=m) for m in range(12)]
    model = HistoricalModel(many_obs_one_market, min_samples=30)
    assert model.n_markets == 1
    est = model.estimate("B", "STRONG_TREND", "6-9", "yes", 60)
    assert not est.sufficient and est.ev_cents is None


def test_model_shrinks_edge_and_never_invents_ev():
    rng = random.Random(1)
    # True win rate 70% at a 60c price -> raw edge ~ +10pp.
    data = [obs(i, outcome="yes" if rng.random() < 0.7 else "no") for i in range(200)]
    model = HistoricalModel(data, min_samples=30, prior_strength=20)
    est = model.estimate("B", "STRONG_TREND", "6-9", "yes", 60)
    assert est.sufficient and est.n_markets == 200
    assert 0 < est.edge < est.raw_edge  # shrunk towards zero
    assert est.ev_cents == pytest.approx(est.edge * 100 - (0.07 * 0.6 * 0.4 * 100 + 1.0))
    # Fair (50/50 at 50c) history -> EV is negative after costs: nothing to trade.
    fair = [obs(i, price=50, outcome="yes" if i % 2 else "no") for i in range(200)]
    est2 = HistoricalModel(fair).estimate("B", "STRONG_TREND", "6-9", "yes", 50)
    assert est2.sufficient and est2.ev_cents < 0


def test_poor_condition_needs_statistical_support():
    bad = [obs(i, regime="SIDEWAYS", outcome="no") for i in range(60)]
    model = HistoricalModel(bad, min_samples=30)
    assert any("regime SIDEWAYS" in p for p in model.poor_conditions("SIDEWAYS", "6-9", "yes", 60, "B"))
    few = HistoricalModel([obs(i, regime="SIDEWAYS", outcome="no") for i in range(10)], min_samples=30)
    assert few.poor_conditions("SIDEWAYS", "6-9", "yes", 60, "B") == ()


# --------------------------------------------------------------- backtesting
def test_chunks_are_chronological_and_disjoint():
    data = [obs(i) for i in range(100)]
    random.Random(3).shuffle(data)
    chunks = chunk_markets(data, "markets", 25)
    assert len(chunks) == 4
    for a, b in zip(chunks, chunks[1:], strict=False):
        assert max(o.close_time for o in a) < min(o.close_time for o in b)


def test_v6_only_learns_from_the_past():
    seen = []

    def fit(train, val, cfg):
        seen.append((max(o.close_time for o in train + val), min(o.close_time for o in train + val)))
        return {}

    probe = Strategy("P", "probe", lambda o, c: None, fit)
    data = [obs(i) for i in range(120)]
    wf = walk_forward(data, BacktestConfig(), "markets", 30, [probe])
    chunks = chunk_markets(data, "markets", 30)
    assert wf.folds == 2 and len(seen) == 2
    for fold, (latest_train, _) in enumerate(seen):
        test_start = min(o.close_time for o in chunks[fold + 2])
        assert latest_train < test_start


def test_one_trade_per_market_with_costs():
    data = [obs(0, minute=m, outcome="yes", price=60) for m in (9, 8, 7)]
    trades = simulate(data, lambda o, c: o.side, {}, BacktestConfig(slippage=1.0))
    assert len(trades) == 1
    assert trades[0].pnl == pytest.approx((100 - 60 - (0.07 * 0.6 * 0.4 * 100 + 1)) / 100)


def test_walk_forward_finds_edge_only_when_it_exists():
    rng = random.Random(5)
    edge = [obs(i, outcome="yes" if rng.random() < 0.75 else "no", comps={"momentum": 0.8, "trend": 0.6})
            for i in range(600)]
    noise = [obs(i, outcome="yes" if rng.random() < 0.60 else "no", comps={"momentum": 0.8, "trend": 0.6})
             for i in range(600)]  # 60% at a 60c price = no edge, costs make it negative
    v6 = next(s for s in STRATEGIES if s.key == "V6")
    good = walk_forward(edge, BacktestConfig(), "markets", 100, [v6]).results[0].oos
    flat = walk_forward(noise, BacktestConfig(), "markets", 100, [v6]).results[0].oos
    assert good.trades > 100 and good.ev > 0
    assert flat.trades == 0 or flat.ev <= 0.02


def test_filter_ablation_verdicts():
    data = [obs(i, flags=("sideways",) if i % 2 else (), outcome="no" if i % 2 else "yes") for i in range(200)]
    v = {x.name: x for x in filter_ablation(data)}
    assert v["sideways"].verdict == "helps"
    flipped = [obs(i, flags=("sideways",) if i % 2 else (), outcome="yes" if i % 2 else "no") for i in range(200)]
    assert {x.name: x for x in filter_ablation(flipped)}["sideways"].verdict == "hurts"
    assert {x.name: x for x in filter_ablation(data[:20])}["sideways"].verdict == "insufficient"


def test_feature_importance_is_measured():
    rng = random.Random(9)
    rows = []
    for i in range(300):
        up = rng.random() < 0.5
        rows.append(obs(i, outcome="yes" if up else "no",
                        comps={"momentum": (0.6 if up else -0.6) + rng.gauss(0, 0.3), "trend": rng.gauss(0, 0.5)}))
    scores = {s.name: s for s in feature_importance(rows)}
    assert scores["momentum"].stars >= 4 and scores["momentum"].effect > 0.3
    assert scores["trend"].status == "not_significant" and scores["trend"].stars == 0
    assert scores["underlying"].status == "insufficient"


# ------------------------------------------------------------ quality / regime
def _feat(**kw):
    base = dict(spread=2, book_total_depth=200, vol_ratio=1.0, efficiency=0.8, zmove_180s=1.5, zmove_60s=1.2,
                zmove_30s=0.5, zmove_300s=1.0, time_remaining=480, trade_count_120s=8, ask_liquidity_yes=80,
                ask_liquidity_no=80, yes_mid=60, accel_z=0.5)
    base.update(kw)
    return base


def test_regime_classification():
    cfg = StrategyConfig()
    assert classify(_feat(), cfg) is Regime.STRONG_TREND
    assert classify(_feat(efficiency=0.4, zmove_180s=0.6), cfg) is Regime.MODERATE_TREND
    assert classify(_feat(efficiency=0.2, zmove_180s=0.2), cfg) is Regime.SIDEWAYS
    assert classify(_feat(vol_ratio=2.0, efficiency=0.1), cfg) is Regime.CHAOTIC
    assert classify(_feat(vol_ratio=2.0, efficiency=0.5), cfg) is Regime.HIGH_VOLATILITY
    assert classify(_feat(spread=15), cfg) is Regime.LOW_LIQUIDITY


GOOD = {"momentum": 0.95, "trend": 0.9, "orderbook": 0.85, "volume": 0.8, "prob_move": 0.85, "underlying": 0.9,
        "acceleration": 0.8}
STABLE = Stability(0.95, 0, 12, 120, 2)


def test_quality_grades_and_no_trade_filter():
    cfg = StrategyConfig()
    a = analyze(_feat(), GOOD, "UP", STABLE, Regime.STRONG_TREND, cfg, 60)
    assert a.grade in ("A+", "A") and a.quality >= 80 and not a.hard_flags
    conflicted = dict(GOOD, trend=-0.6, orderbook=-0.5, underlying=-0.5)
    c = analyze(_feat(), conflicted, "UP", STABLE, Regime.STRONG_TREND, cfg, 60)
    assert c.grade == "NO_TRADE" and "conflict" in c.hard_flags and "underlying_conflict" in c.hard_flags
    side = analyze(_feat(), GOOD, "UP", STABLE, Regime.SIDEWAYS, cfg, 60)
    assert "sideways" in side.hard_flags and side.grade == "NO_TRADE"
    ext = analyze(_feat(zmove_300s=3.0), GOOD, "UP", STABLE, Regime.STRONG_TREND, cfg, 60)
    assert "extended_move" in ext.hard_flags
    expensive = analyze(_feat(), GOOD, "UP", STABLE, Regime.STRONG_TREND, cfg, 85)
    assert "extended_move" in expensive.hard_flags
    flip = analyze(_feat(), GOOD, "UP", Stability(0.4, 3, 12, 10, 0), Regime.STRONG_TREND, cfg, 60)
    assert "flip_flop" in flip.hard_flags
    disabled = StrategyConfig(disabled_filters=frozenset({"sideways"}))
    assert "sideways" not in analyze(_feat(), GOOD, "UP", STABLE, Regime.SIDEWAYS, disabled, 60).hard_flags


def test_missing_underlying_is_flagged_not_faked():
    no_und = {k: v for k, v in GOOD.items() if k != "underlying"}
    a = analyze(_feat(), no_und, "UP", STABLE, Regime.STRONG_TREND, StrategyConfig(), 60)
    assert a.underlying_state == "UNAVAILABLE" and "underlying_unavailable" in a.soft_flags
    assert "underlying" not in a.points


def test_stability_tracker_flips():
    tr = StabilityTracker()
    now = T0
    for i, lean in enumerate(["UP", "DOWN", "UP", "DOWN", "UP", "UP"]):
        tr.record("X", now + timedelta(seconds=15 * i), lean, 75)
    st = tr.measure("X", "UP", now + timedelta(seconds=75))
    assert st.flips >= 3 and st.score < 0.6


def test_buckets():
    assert time_bucket(800) == "12-15" and time_bucket(400) == "6-9" and time_bucket(30) == "<1"
    assert price_bucket(62) == "60-65" and price_bucket(95) == "90+"


# ------------------------------------------------------------ adaptive / tags
def test_adaptive_mode():
    hist = [1.0] * 300
    assert assess([1.0], hist, [], True).mode == "NORMAL"
    assert assess([5.0], hist + [2.0] * 10, [], True).mode == "PAUSED"
    assert assess([1.0], hist, [], False).mode == "PAUSED"
    losing = [(False, 0.7)] * 20
    assert assess([], [], losing, True).mode == "CAUTION"
    assert assess([], [], losing[:5], True).mode == "NORMAL"  # never react to a handful of trades


def test_trade_analysis_tags():
    entry = {"direction": "UP", "components": {"momentum": 0.7, "orderbook": 0.4}, "time_remaining": 120,
             "regime": "STRONG_TREND", "underlying_state": "CONFIRMED", "stability": 0.9, "volume_accel": 1.5}
    exit_ = {"components": {"momentum": -0.5, "orderbook": -0.3}}
    loss = analyze_trade(entry, exit_, won=False)
    assert {"momentum_reversal", "orderbook_reversal", "late_entry"} <= set(loss)
    win = analyze_trade(entry, {}, won=True)
    assert {"strong_momentum", "rising_volume", "underlying_confirmed", "stable_signal"} <= set(win)


# -------------------------------------------------------------- runtime wiring
async def test_runtime_records_and_labels_observations(runtime):
    from app.risk.profiles import RiskLevel
    from tests.conftest import feed_history, make_info

    runtime.store.update(running=True, risk_level=RiskLevel.HIGH)
    info = make_info(remaining=200)
    sig = feed_history(runtime, info, "up")
    snap = runtime.market_data.latest[info.ticker]
    assert runtime.record_observation(sig, snap) is True
    assert runtime.record_observation(sig, snap) is False  # one per market-minute
    assert runtime.repo.count_observations() == (1, 0)
    later = info.close_time + timedelta(seconds=30)
    runtime.client.clock = lambda: later
    assert await runtime.label_observations(now=later) == 1
    assert runtime.repo.count_observations() == (1, 1)
    runtime.refresh_model()
    assert runtime.model.n_markets == 1


def test_feature_importance_ignores_what_the_price_already_knows():
    """A component that only mirrors the market price predicts outcomes but has NO edge."""
    rng = random.Random(4)
    rows = []
    for i in range(400):
        p = rng.uniform(0.2, 0.8)  # fair market probability
        o = obs(i, outcome="yes" if rng.random() < p else "no", comps={"momentum": (p - 0.5) * 2})
        o.features["yes_mid"] = p * 100
        rows.append(o)
    m = {s.name: s for s in feature_importance(rows)}["momentum"]
    assert m.stars == 0 and m.status == "not_significant"


def test_confirm_rule_matches_backtest_v4():
    from app.research.backtest import _v4
    from app.strategy.confirm import confirm_side

    good = {"momentum": 0.5, "trend": 0.4, "orderbook": 0.2, "underlying": 0.3}
    assert confirm_side(good) == ("yes", [])
    assert confirm_side({k: -v for k, v in good.items()})[0] == "no"
    assert confirm_side(dict(good, underlying=-0.3))[0] is None
    assert confirm_side({k: v for k, v in good.items() if k != "underlying"})[0] is None
    assert _v4(obs(0, comps=good), {}) == "yes"
    assert _v4(obs(0, comps=dict(good, orderbook=0.0)), {}) is None
