"""Take-profit exits, daily recap, extra coins."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from app.kalshi.markets import detect_asset
from app.risk.profiles import RiskLevel
from app.trading.positions import Position
from tests.conftest import feed_history, make_info


def test_new_coins_are_detected_but_not_confused():
    assets = ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB"]
    assert detect_asset("KXBNB15M BNB 15 min", assets) == "BNB"
    assert detect_asset("KXXRP15M XRP", assets) == "XRP"
    assert detect_asset("KXDOGE15M Dogecoin", assets) == "DOGE"
    assert detect_asset("KXBCH15M Bitcoin Cash", assets) is None


async def _opened(runtime):
    runtime.store.update(running=True, risk_level=RiskLevel.HIGH)
    info = make_info()
    sig = feed_history(runtime, info, "up")
    t, _ = runtime.propose(info.ticker)
    pos = await runtime.execute_ticket(runtime.tickets.consume(t.id), confirmed=True)
    return pos, sig, runtime.market_data.latest[info.ticker]


async def test_take_profit_reason_by_cents_and_by_price(runtime):
    pos, sig, snap = await _opened(runtime)
    bid = snap.exit_price(pos.side)
    s = runtime.settings
    assert runtime.exit_reason(pos, sig, snap) is None or "take-profit" not in runtime.exit_reason(pos, sig, snap)
    object.__setattr__(s, "take_profit_price_cents", bid)
    assert "take-profit" in runtime.exit_reason(pos, sig, snap)
    object.__setattr__(s, "take_profit_price_cents", 0.0)
    object.__setattr__(s, "take_profit_cents", max(1.0, bid - pos.entry_price))
    reason = runtime.exit_reason(pos, sig, snap)
    if bid - pos.entry_price >= 1:
        assert "take-profit" in reason
    object.__setattr__(s, "take_profit_cents", 0.0)


async def test_take_profit_sells_paper_even_with_early_exit_off(runtime):
    pos, sig, snap = await _opened(runtime)
    s = runtime.settings
    object.__setattr__(s, "early_exit", False)
    object.__setattr__(s, "take_profit_price_cents", 1.0)  # any bid qualifies
    object.__setattr__(s, "early_exit_confirm_ticks", 1)
    await runtime._manage_exits(sig, snap)
    assert not pos.is_open and "take-profit" in pos.close_reason
    object.__setattr__(s, "take_profit_price_cents", 0.0)


async def test_daily_recap_once_per_day(runtime):
    runtime.notifier = AsyncMock()
    now = datetime(2026, 11, 3, 0, 5, tzinfo=UTC)
    p = Position(mode="paper", ticker="T", event_ticker="E", asset="BTC", label="BTC up", side="yes",
                 contracts=2, entry_price=60, entry_fee=0.04, risk_level="high")
    runtime.portfolio.add_open(p)
    runtime.portfolio.close(p, 100, 0, "settled")
    p.closed_at = now - timedelta(hours=3)
    runtime.repo.save_position(p)
    runtime.store.update(running=True)
    assert await runtime.send_daily_recap(now) == "2026-11-02"
    text = runtime.notifier.alert.call_args.args[2]
    assert "DAILY RECAP" in text and "Trades: 1" in text and "fees paid" in text
    assert await runtime.send_daily_recap(now + timedelta(hours=5)) is None  # never twice
    assert runtime.notifier.alert.await_count == 1


@pytest.mark.parametrize("cmd", ["xrp", "doge", "bnb"])
def test_coin_commands_exist(cmd):
    from app.telegram.callbacks import parse_callback
    from app.telegram.handlers import COMMAND_ROUTES

    parse_callback(COMMAND_ROUTES[cmd])


def test_only_single_coin_crypto_can_ever_be_traded(runtime):
    from dataclasses import replace

    for asset, ok in (("XRP", True), ("HYPE", True), ("EURUSD", False), ("SILVER", False), ("CRYPTOLEAD", False)):
        info = replace(make_info(), asset=asset)
        sig = feed_history(runtime, info, "up")
        dec = runtime.check_risk(sig, runtime.market_data.latest[info.ticker])
        blocked = any("research-only" in f for f in dec.failures)
        assert blocked is (not ok), asset


def test_kelly_math_and_calculator():
    import importlib.util
    import sys as _sys

    spec = importlib.util.spec_from_file_location("kelly_script", "scripts/kelly.py")
    k = importlib.util.module_from_spec(spec)
    _sys.modules["kelly_script"] = k
    spec.loader.exec_module(k)
    assert k.kelly(0.60, 50, fee_rate=0) == pytest.approx(0.20)  # the 1:1 rule of thumb: p - q
    assert k.kelly(0.60, 62) == 0.0  # 60% at 62c is not an edge
    assert 0 < k.kelly(0.80, 75) < k.kelly(0.80, 70)


def test_kelly_sizing_needs_measured_edge(runtime):
    from types import SimpleNamespace

    from app.risk.manager import kelly_win_rate

    assert kelly_win_rate(None) is None
    assert kelly_win_rate(SimpleNamespace(sufficient=False, p_model=0.8, ev_cents=5, ev_low_cents=1)) is None
    est = SimpleNamespace(sufficient=True, p_model=0.80, ev_cents=5.0, ev_low_cents=1.0)
    assert kelly_win_rate(est) == pytest.approx(0.76)  # uses the conservative bound
    runtime.risk.kelly_sizing = True
    runtime.store.update(running=True, risk_level=RiskLevel.HIGH)
    info = make_info()
    sig = feed_history(runtime, info, "up")
    dec = runtime.check_risk(sig, runtime.market_data.latest[info.ticker])
    assert not dec.approved and any("Kelly" in f for f in dec.failures)  # no history -> Kelly 0 -> no trade
