"""Fractional Kelly engine (app/risk/kelly.py)."""

import pytest

from app.risk.kelly import KellyInput, KellyMode, full_kelly, max_loss, size


def inp(**kw) -> KellyInput:
    base = dict(p_point=0.62, p_low=0.60, price_cents=50, bankroll_usd=1000.0, remaining_daily_loss_usd=1e9,
                remaining_open_risk_usd=1e9, calibrated=True, fee_rate=0.07)
    base.update(kw)
    return KellyInput(**base)


def test_modes_are_fractional_and_capped():
    assert [m.multiplier for m in KellyMode] == [0.25, 0.5, 0.75]
    assert [m.position_cap for m in KellyMode] == [0.01, 0.02, 0.03]
    assert all(m.multiplier < 1 for m in KellyMode)  # never full Kelly


def test_spec_formula():
    assert full_kelly(0.60, 0.50) == pytest.approx(0.20)  # (p*b - q)/b with b=1
    assert full_kelly(0.80, 0.75) == pytest.approx((0.80 - 0.75) / 0.25)


@pytest.mark.parametrize("mode", list(KellyMode))
def test_position_loss_never_exceeds_mode_cap(mode):
    s = size(mode, inp(p_point=0.95, p_low=0.93))  # huge edge: the cap must bind
    assert s.ok and s.max_loss_usd <= 1000 * mode.position_cap + 1e-9
    assert max_loss(s.contracts + 1, 50, 0.07) > 1000 * mode.position_cap  # rounded DOWN to the largest that fits


def test_uses_conservative_probability_and_rejects_below_breakeven():
    # p_point beats break-even but the uncertainty-adjusted p_low does not -> no trade
    s = size(KellyMode.HIGH, inp(p_point=0.55, p_low=0.51))
    assert not s.ok and "break-even" in s.reason and s.contracts == 0 and s.fractional_kelly == 0
    assert s.breakeven == pytest.approx(0.52)  # 50c + 2c rounded-up fee


def test_never_negative_and_invalid_inputs_rejected():
    for bad in (dict(p_low=None), dict(p_point=1.2), dict(p_low=0.7, p_point=0.6), dict(price_cents=None),
                dict(price_cents=0), dict(fee_rate=None), dict(fee_rate=0), dict(bankroll_usd=0)):
        s = size(KellyMode.MEDIUM, inp(**bad))
        assert not s.ok and s.contracts == 0, bad
    assert size(KellyMode.LOW, inp(p_point=0.2, p_low=0.1)).contracts == 0


def test_daily_and_open_risk_limits_bind():
    s = size(KellyMode.HIGH, inp(p_point=0.95, p_low=0.93, remaining_daily_loss_usd=4.0))
    assert s.ok and s.max_loss_usd <= 4.0 and s.limiting_factor == "remaining daily loss"
    s = size(KellyMode.HIGH, inp(p_point=0.95, p_low=0.93, remaining_open_risk_usd=2.0))
    assert s.max_loss_usd <= 2.0 and s.limiting_factor == "remaining open-risk allowance"


def test_skip_when_one_contract_exceeds_budget():
    s = size(KellyMode.LOW, inp(bankroll_usd=20, price_cents=90, p_point=0.99, p_low=0.98))  # cap = $0.20
    assert not s.ok and "exceeds" in s.reason


def test_live_requires_calibration():
    s = size(KellyMode.MEDIUM, inp(calibrated=False, require_calibration=True))
    assert not s.ok and "calibrated" in s.reason
    assert size(KellyMode.MEDIUM, inp(calibrated=False)).ok  # paper may size an uncalibrated estimate


def test_more_aggressive_mode_never_smaller():
    a, b, c = (size(m, inp()).contracts for m in KellyMode)
    assert a <= b <= c


def test_fractional_kelly_matches_fee_inclusive_kelly():
    s = size(KellyMode.MEDIUM, inp())
    c_fee = 0.52
    assert s.fee_kelly == pytest.approx((0.60 - c_fee) / (1 - c_fee))
    assert s.fractional_kelly == pytest.approx(s.fee_kelly * 0.5)
    assert s.fee_kelly < s.full_kelly  # fees always shrink Kelly


def _kb():
    import importlib.util
    import sys
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("kelly_backtest", Path(__file__).parents[1] / "scripts" /
                                                  "kelly_backtest.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["kelly_backtest"] = m  # dataclasses need their module registered
    spec.loader.exec_module(m)
    return m


def test_backtest_calibration_never_looks_ahead():
    from datetime import UTC, datetime, timedelta

    kb = _kb()
    d0 = datetime(2026, 10, 1, 12, tzinfo=UTC)
    day1 = [kb.Trade(d0 + timedelta(minutes=15 * i), "HYPE", "yes", 40, False, 0.55, 0.50) for i in range(40)]
    day2 = [kb.Trade(d0 + timedelta(days=1), "HYPE", "yes", 40, True, 0.55, 0.50)]
    cal = kb.calibrate(day1 + day2)
    assert all(t.p == 0.55 for t in cal[:40])  # day 1: no earlier results -> model probability unchanged
    assert cal[-1].p < 0.55  # day 2 learns from day 1's (all lost) results only


def test_backtest_kelly_respects_mode_caps():
    from datetime import UTC, datetime, timedelta

    kb = _kb()
    d0 = datetime(2026, 10, 1, tzinfo=UTC)
    ts = [kb.Trade(d0 + timedelta(minutes=15 * i), "DOGE", "yes", 50, i % 2 == 0, 0.9, 0.85) for i in range(50)]
    for mode in KellyMode:
        r = kb.simulate(mode.value, ts, 1000.0, 0.07, mode)
        assert r.trades and r.staked / r.trades <= 1000 * 1.3 * mode.position_cap  # bankroll may grow a little
    fixed = kb.simulate("FIXED", ts, 1000.0, 0.07, None)
    assert fixed.staked / fixed.trades <= 10.0 + 1e-9
