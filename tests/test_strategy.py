from datetime import timedelta

from app.kalshi.fixtures import make_snapshot
from app.strategy.engine import SignalEngine
from app.strategy.signals import Direction, Validity
from app.utils.time import utcnow
from tests.conftest import make_info


def run(trend: str, threshold: int = 60, steps: int = 37):
    engine = SignalEngine(min_confidence=55)
    info = make_info()
    now = utcnow()
    res = None
    for i in range(steps):
        ts = now - timedelta(seconds=(steps - 1 - i) * 5)
        res = engine.evaluate(make_snapshot("BTC", trend, ts, info=info, ts=ts), threshold=threshold, now=ts)
    return engine, res


def test_signal_engine_returns_up():
    _, r = run("up")
    assert r.direction is Direction.UP
    assert r.recommended_action == "BUY_YES"
    assert 0 <= r.confidence <= 100
    assert any(x.positive for x in r.reasons)


def test_signal_engine_returns_down():
    _, r = run("down")
    assert r.direction is Direction.DOWN
    assert r.recommended_action == "BUY_NO"


def test_signal_engine_returns_wait():
    _, r = run("flat", threshold=80)
    assert r.direction is Direction.WAIT
    assert r.recommended_action == "NONE"
    assert any("threshold" in x.text or "direction" in x.text for x in r.reasons)


def test_strong_signal_below_threshold_is_wait_but_keeps_leaning():
    _, r = run("up", threshold=99)
    assert r.direction is Direction.WAIT and r.leaning is Direction.UP


def test_insufficient_history_is_wait():
    _, r = run("up", steps=2)
    assert r.direction is Direction.WAIT and r.validity is Validity.INSUFFICIENT_DATA


def test_stale_snapshot_is_invalid():
    engine = SignalEngine()
    snap = make_snapshot("BTC", "up", utcnow(), info=make_info(), ts=utcnow() - timedelta(minutes=5))
    assert engine.evaluate(snap).validity is Validity.STALE


def test_flip_detection():
    engine, up = run("up")
    assert engine.detect_flip(up) is None
    info = make_info()
    now = utcnow()
    for i in range(40):
        ts = now + timedelta(seconds=i * 5)
        snap = make_snapshot("BTC", "down", ts, info=info, ts=ts)
        down = engine.evaluate(snap, threshold=60, now=ts)
    flip = engine.detect_flip(down)
    assert down.leaning is Direction.DOWN
    assert flip is not None and flip.previous.leaning is Direction.UP
    # A late, extended move is graded NO TRADE even though it leans DOWN.
    assert down.direction is Direction.WAIT and "extended_move" in down.analysis.hard_flags


def test_underlying_unavailable_is_not_fabricated():
    _, r = run("up")
    assert r.features["spot"] is None and r.features["strike_dist_pct"] is None
    assert "underlying" not in r.components


def test_fast_updates_do_not_freeze_history():
    """Regression: sub-spacing updates must not keep overwriting a single sample."""
    engine = SignalEngine()
    info = make_info()
    now = utcnow()
    for i in range(100):  # ten updates per second for 10s
        ts = now + timedelta(milliseconds=100 * i)
        engine.record(make_snapshot("BTC", "up", ts, info=info, ts=ts))
    assert len(engine.history(info.ticker)) >= 5
    for i in range(30):
        engine.stability.record(info.ticker, now + timedelta(seconds=5 * i), "UP", 80)
    assert len(engine.stability.history(info.ticker)) >= 10
