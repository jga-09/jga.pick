"""Entry timing + session discipline (ENTRY_MIN/MAX_LEFT_SEC, SKIP_WINDOWS_AFTER_LOSS, MAX_TRADES_PER_DAY,
MIN_EDGE_CENTS)."""

from datetime import timedelta

import pytest

from app.config import Settings
from app.errors import ConfigError
from app.risk.manager import SessionRules
from app.risk.profiles import RiskLevel
from app.risk.sizing import PositionSizer
from app.strategy.btc_decision import NoTrade, decide
from app.utils.time import utcnow
from tests.conftest import make_info, quote_snapshot
from tests.test_btc import inputs, params
from tests.test_risk import _RM, PROFILES, ctx, sig

GUIDE = SessionRules(entry_min_left_sec=90, entry_max_left_sec=660, sit_out_after_loss_sec=900, max_trades_per_day=8)


def test_off_by_default_blocks_nothing():
    assert SessionRules().failures(840, ctx(last_loss_at=utcnow(), markets_today=999), utcnow()) == []


def test_entry_window():
    now = utcnow()
    assert any("Too early" in f for f in GUIDE.failures(800, ctx(), now))
    assert GUIDE.failures(600, ctx(), now) == []
    assert any("Too late" in f for f in GUIDE.failures(60, ctx(), now))


def test_sit_out_after_loss_and_daily_cap():
    now = utcnow()
    assert any("Sitting out" in f for f in GUIDE.failures(600, ctx(last_loss_at=now - timedelta(minutes=5)), now))
    assert GUIDE.failures(600, ctx(last_loss_at=now - timedelta(minutes=16)), now) == []
    assert any("Daily trade limit" in f for f in GUIDE.failures(600, ctx(markets_today=8), now))
    assert GUIDE.failures(600, ctx(markets_today=7), now) == []


def test_risk_manager_applies_rules_to_every_strategy():
    rm = _RM(PositionSizer(0.07), stale_after_sec=30, price_buffer_cents=2, session=GUIDE)
    p = PROFILES[RiskLevel.HIGH]
    early = quote_snapshot(make_info(remaining=800, elapsed=100))
    d = rm.evaluate(sig(), early, p, ctx())
    assert not d.approved and "Too early" in d.reason
    d = rm.evaluate(sig(), quote_snapshot(make_info(remaining=600, elapsed=300)), p, ctx(markets_today=8))
    assert not d.approved and "Daily trade limit" in d.reason


def test_btc_engine_says_too_early():
    s = decide(inputs(t_left=800), params(max_time_sec=660))
    assert not s.approved and any(r.code is NoTrade.TOO_EARLY for r in s.rejections)
    assert not any(r.code is NoTrade.TOO_EARLY for r in decide(inputs(t_left=540), params(max_time_sec=660)).rejections)


def test_bad_window_fails_safely():
    s = Settings(_env_file=None, entry_min_left_sec=700, entry_max_left_sec=600, telegram_admin_ids="1")
    with pytest.raises(ConfigError):
        s.validate_runtime(require_telegram=False)


async def test_markets_today_counts_distinct_markets(repo):
    from app.trading.paper import PaperExecutor
    from app.trading.portfolio import Portfolio
    from tests.test_paper import ticket

    pf = Portfolio(repo, 1000)
    paper = PaperExecutor(pf, slippage_cents=0, fee_rate=0.0)
    snap = quote_snapshot(make_info(), yes_bid=60, yes_ask=62)
    paper.settle(await paper.buy(ticket(), snap), "no")
    await paper.buy(ticket(), snap)  # same market again
    c = pf.risk_context(mode="paper", trading_enabled=True, disabled_reason="", mode_permitted=True)
    assert c.markets_today == 1
