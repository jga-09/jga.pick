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


# ---------------------------------------------------------------- fair-value forward test on HYPE / DOGE
def _coin_info(asset: str, rules: str):
    from dataclasses import replace as dc_replace

    from tests.test_btc import info

    i = info()
    return dc_replace(i, asset=asset, ticker=f"KX{asset}15M-T", event_ticker=f"KX{asset}15M-T",
                      series_ticker=f"KX{asset}15M", rules_primary=rules)


def test_contract_terms_for_hype_and_doge():
    from app.kalshi.contract import interpret_contract

    hype = ("If the simple average of the sixty seconds of CF Benchmarks' HYPE Real-Time Index before close "
            "is above 41.20, then the market resolves to Yes.")
    doge = ("If the simple average of the sixty seconds of CF Benchmarks' Dogecoin Real-Time Index before close "
            "is above 0.1834, then the market resolves to Yes.")
    for asset, rules in (("HYPE", hype), ("DOGE", doge)):
        t = interpret_contract(_coin_info(asset, rules))
        assert t.verified, t.problems
        assert t.asset == asset and t.averaging_window_sec == 60 and t.yes_means.startswith(asset)
    wrong = interpret_contract(_coin_info("DOGE", hype))  # DOGE market whose rules talk about HYPE
    assert not wrong.verified and any("Dogecoin" in p for p in wrong.problems)


async def test_multicoin_feed_keeps_coins_apart():
    from app.data.btc_feed import MultiCoinFeed

    f = MultiCoinFeed(["HYPE", "DOGE"])
    assert f.feeds["HYPE"].base.endswith("/HYPE-USD") and f.feeds["DOGE"].base.endswith("/DOGE-USD")
    assert await f.get_price("BTC") is None and f.bars("BTC") == []
    assert f.status().state == "DOWN" and "HYPE/DOGE" in f.status().source
    await f.close()


def test_non_btc_fair_value_and_pure_mode_are_paper_only():
    from dataclasses import replace as dc_replace

    from app.risk.manager import RiskManager
    from tests.test_btc import NOW, _btc_signal_result, _snap, info

    m = info()
    hype = dc_replace(m, asset="HYPE", ticker="KXHYPE15M-T")
    b = decide(inputs(), params())
    rm = RiskManager(PositionSizer(0.07), stale_after_sec=30, price_buffer_cents=2, strategy_mode="btc")
    d = rm.evaluate(_btc_signal_result(b, hype.ticker), _snap(hype), PROFILES[RiskLevel.HIGH], ctx(mode="live"),
                    now=NOW)
    assert any("paper-only" in f for f in d.failures)
    pure = RiskManager(PositionSizer(0.07), stale_after_sec=30, price_buffer_cents=2, strategy_mode="btc",
                       fair_value_pure=True)
    d = pure.evaluate(_btc_signal_result(b, m.ticker), _snap(m), PROFILES[RiskLevel.HIGH], ctx(mode="live"), now=NOW)
    assert any("never live" in f for f in d.failures)
    ok = pure.evaluate(_btc_signal_result(b, m.ticker), _snap(m), PROFILES[RiskLevel.HIGH], ctx(), now=NOW)
    assert ok.approved, ok.failures  # paper is fine


def test_pure_mode_skips_momentum_filters_but_keeps_edge_gate():
    strict = decide(inputs(), params(min_strength=99))
    assert any(r.code is NoTrade.WEAK_DIRECTION for r in strict.rejections)
    pure = decide(inputs(), params(min_strength=99, pure=True))
    assert not any(r.code is NoTrade.WEAK_DIRECTION for r in pure.rejections)
    assert any(r.code is NoTrade.INSUFFICIENT_EV for r in decide(inputs(), params(pure=True, min_ev_cents=60)).rejections)


def test_price_format_for_small_coins():
    from app.telegram.btc_views import _big

    assert _big(60300.4) == "$60,300" and _big(41.234) == "$41.23" and _big(0.18341) == "$0.1834"


def test_calibration_is_per_coin(repo):
    from app.data.btc_feed import FixtureBTCFeed
    from app.kalshi.fixtures import FixtureKalshiClient
    from app.risk.profiles import load_profiles
    from app.runtime import BotRuntime
    from tests.conftest import make_settings

    rt = BotRuntime(make_settings(strategy_mode="btc", assets="HYPE,DOGE"), repo, FixtureKalshiClient(),
                    underlying=FixtureBTCFeed(), profiles=load_profiles(read_env=False))
    assert set(rt.calibrations) == {"HYPE", "DOGE"} and rt.calibration_for("BTC") is None


def test_forward_test_start_is_recorded_and_reset_on_config_change(repo):
    from app.data.btc_feed import FixtureBTCFeed
    from app.kalshi.fixtures import FixtureKalshiClient
    from app.risk.profiles import load_profiles
    from app.runtime import BotRuntime
    from tests.conftest import make_settings

    def rt(**kw):
        return BotRuntime(make_settings(strategy_mode="btc", assets="HYPE,DOGE", **kw), repo, FixtureKalshiClient(),
                          underlying=FixtureBTCFeed(), profiles=load_profiles(read_env=False))

    first = rt(fair_value_pure=True).forward_test
    assert first and "HYPE,DOGE" in first["config"]
    assert rt(fair_value_pure=True).forward_test == first  # restart keeps the same test
    changed = rt(fair_value_pure=True, min_edge_cents=6).forward_test
    assert changed["config"] != first["config"] and changed["start"] >= first["start"]
