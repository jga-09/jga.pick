from dataclasses import replace
from datetime import timedelta

import pytest

from app.risk.manager import RiskContext, RiskManager
from app.risk.profiles import RiskLevel, load_profiles
from app.risk.sizing import PositionSizer
from app.strategy.signals import Direction, SignalResult, Validity
from app.utils.time import utcnow
from tests.conftest import make_info, quote_snapshot

PROFILES = load_profiles(read_env=False)


def sig(direction=Direction.UP, confidence=90, validity=Validity.VALID, ticker="KXBTC15M-TEST") -> SignalResult:
    return SignalResult(ticker, "BTC", "BTC 15M", direction, direction if direction != Direction.WAIT else Direction.UP,
                        confidence, 0.6, (), utcnow(), validity=validity)


def ctx(**kw) -> RiskContext:
    base = dict(trading_enabled=True, disabled_reason="", mode="paper", mode_permitted=True, balance_usd=1000.0,
                open_positions=0, open_exposure_usd=0.0, open_tickers=frozenset(), inflight_tickers=frozenset(),
                daily_realized_pnl=0.0)
    base.update(kw)
    return RiskContext(**base)


@pytest.fixture
def rm() -> RiskManager:
    return RiskManager(PositionSizer(0.07), stale_after_sec=30, price_buffer_cents=2)


def test_low_rejects_confidence_below_threshold(rm):
    snap = quote_snapshot(make_info())
    d = rm.evaluate(sig(confidence=79), snap, PROFILES[RiskLevel.LOW], ctx())
    assert not d.approved
    assert "below LOW threshold (80)" in d.reason
    assert rm.evaluate(sig(confidence=85), snap, PROFILES[RiskLevel.LOW], ctx()).approved


def test_medium_uses_correct_limits(rm):
    p = PROFILES[RiskLevel.MEDIUM]
    assert (p.min_confidence, p.max_position_usd, p.max_open_positions, p.max_daily_loss_usd,
            p.min_time_remaining_sec, p.max_spread_cents) == (70, 15, 2, 30, 120, 8)
    snap = quote_snapshot(make_info())
    assert rm.evaluate(sig(confidence=72), snap, p, ctx()).approved  # would fail LOW
    assert not rm.evaluate(sig(confidence=72), snap, PROFILES[RiskLevel.LOW], ctx()).approved
    d = rm.evaluate(sig(confidence=72), snap, p, ctx(open_positions=2))
    assert not d.approved and "Max open positions" in d.reason
    wide = quote_snapshot(make_info(), yes_bid=55, yes_ask=64)  # 9c spread > 8c
    assert any("Spread" in f for f in rm.evaluate(sig(confidence=72), wide, p, ctx()).failures)


def test_high_still_enforces_max_exposure(rm):
    p = PROFILES[RiskLevel.HIGH]
    snap = quote_snapshot(make_info())
    d = rm.evaluate(sig(confidence=95), snap, p, ctx(open_exposure_usd=p.max_total_exposure_usd))
    assert not d.approved and "Max exposure" in d.reason
    # Partial headroom: size is capped by the remaining exposure budget.
    d = rm.evaluate(sig(confidence=95), snap, p, ctx(open_exposure_usd=p.max_total_exposure_usd - 3, open_positions=1))
    assert d.approved and d.total_usd <= 3.0
    d = rm.evaluate(sig(confidence=95), snap, p, ctx(open_positions=3))
    assert not d.approved


def test_daily_loss_limit_blocks_new_trades(rm):
    snap = quote_snapshot(make_info())
    d = rm.evaluate(sig(), snap, PROFILES[RiskLevel.LOW], ctx(daily_realized_pnl=-10.0))
    assert not d.approved and "Daily loss limit" in d.reason
    assert rm.evaluate(sig(), snap, PROFILES[RiskLevel.LOW], ctx(daily_realized_pnl=-9.0)).approved


def test_stale_market_data_blocks_trade(rm):
    snap = quote_snapshot(make_info(), ts=utcnow() - timedelta(seconds=120))
    d = rm.evaluate(sig(), snap, PROFILES[RiskLevel.HIGH], ctx())
    assert not d.approved and any("Stale" in f for f in d.failures)


def test_expiry_proximity_blocks_low_but_not_high(rm):
    snap = quote_snapshot(make_info(remaining=100))
    low = rm.evaluate(sig(), snap, PROFILES[RiskLevel.LOW], ctx())
    assert not low.approved and "Too close to expiry" in low.reason
    assert rm.evaluate(sig(), snap, PROFILES[RiskLevel.HIGH], ctx()).approved
    closed = quote_snapshot(make_info(remaining=-5))
    assert not rm.evaluate(sig(), closed, PROFILES[RiskLevel.HIGH], ctx()).approved


def test_trading_disabled_and_duplicates_block(rm):
    snap = quote_snapshot(make_info())
    p = PROFILES[RiskLevel.HIGH]
    assert not rm.evaluate(sig(), snap, p, ctx(trading_enabled=False, disabled_reason="x")).approved
    assert not rm.evaluate(sig(), snap, p, ctx(open_tickers=frozenset({snap.ticker}), open_positions=1)).approved
    assert not rm.evaluate(sig(), snap, p, ctx(inflight_tickers=frozenset({snap.ticker}))).approved
    assert not rm.evaluate(sig(direction=Direction.WAIT, confidence=50), snap, p, ctx()).approved
    assert not rm.evaluate(sig(), snap, p, ctx(mode="live", mode_permitted=False)).approved


def test_loss_cooldown(rm):
    snap = quote_snapshot(make_info())
    d = rm.evaluate(sig(), snap, PROFILES[RiskLevel.LOW], ctx(last_loss_at=utcnow() - timedelta(seconds=60)))
    assert not d.approved and "cooldown" in d.reason.lower()


def test_position_sizing_respects_configured_maximum():
    sizer = PositionSizer(0.07)
    for level, p in PROFILES.items():
        for price in (5, 30, 62, 90):
            r = sizer.size(p, price_cents=price, confidence=100, balance_usd=1_000_000, open_exposure_usd=0,
                           liquidity_contracts=None)
            assert r.cost_usd <= p.max_position_usd + 1e-9, (level, price)
            assert r.total_usd <= p.max_position_usd + 1e-9, (level, price)
    low = PROFILES[RiskLevel.LOW]
    r = sizer.size(low, price_cents=50, confidence=100, balance_usd=1000, open_exposure_usd=0, liquidity_contracts=3)
    assert r.contracts == 3  # liquidity cap
    r = sizer.size(low, price_cents=50, confidence=100, balance_usd=0.4, open_exposure_usd=0, liquidity_contracts=None)
    assert r.contracts == 0  # cannot afford one contract
    custom = replace(low, max_position_usd=2.0)
    r = sizer.size(custom, price_cents=40, confidence=100, balance_usd=1000, open_exposure_usd=0,
                   liquidity_contracts=None)
    assert r.total_usd <= 2.0
