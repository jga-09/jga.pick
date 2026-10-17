"""BTC 15-minute engine: contract rules, data feed, probability, edge, filters, risk, backtest integrity."""

from __future__ import annotations

import json
import math
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from app.data.btc_feed import BTCFeed, MinuteBar, parse_candles
from app.errors import ConfigError
from app.kalshi.contract import interpret_contract
from app.kalshi.market_data import MarketInfo, MarketSnapshot, OrderBook
from app.research.btc_backtest import (
    SimCosts,
    Trade,
    load_dataset,
    metrics,
    simulate,
    v_model_edge,
    walk_forward,
    params_for,
)
from app.research.btc_calibration import calibration_report
from app.research.btc_synthetic import generate
from app.risk.manager import RiskContext, RiskManager
from app.risk.profiles import RiskLevel, load_profiles
from app.risk.sizing import PositionSizer
from app.strategy.btc_decision import (
    BTCInputs,
    BTCParams,
    CalibrationState,
    NoTrade,
    decide,
    fee_cents,
    side_edges,
)
from app.strategy.btc_model import (
    BTCRegime,
    ModelConfig,
    classify_regime,
    compute_btc_features,
    effective_horizon,
    estimate_probability,
    p_above,
)
from app.strategy.signals import Direction, SignalResult
from app.trading.fees import estimate_fee
from tests.conftest import make_settings

NOW = datetime(2026, 10, 14, 15, 0, tzinfo=UTC)
RULES = ("If the simple average of the sixty seconds of CF Benchmarks' Bitcoin Real-Time Index (BRTI) "
         "before 11:15 AM ET is above 60000.00, then the market resolves to Yes.")
CAL_OK = CalibrationState("CALIBRATED", 120, 0.02, 0.20, 0.21)


def info(strike_type: str = "greater_or_equal", floor: float | None = 60000.0, cap: float | None = None,
         rules: str = RULES, asset: str = "BTC", t_left: float = 540) -> MarketInfo:
    return MarketInfo(ticker="KXBTC15M-26OCT141115", event_ticker="KXBTC15M-26OCT141115", series_ticker="KXBTC15M",
                      asset=asset, title="Bitcoin price up?", subtitle="", open_time=NOW - timedelta(seconds=900 - t_left),
                      close_time=NOW + timedelta(seconds=t_left), expiration_time=None, status="active",
                      floor_strike=floor, cap_strike=cap, strike_type=strike_type, rules_primary=rules)


def bars(n: int = 120, price: float = 60300.0, step_bps: float = 5.0, drift_bps: float = 0.0,
         end: datetime = NOW) -> list[MinuteBar]:
    out, p = [], price * math.exp(-(n * drift_bps) / 1e4)
    for i in range(n):
        p *= math.exp((drift_bps + (step_bps if i % 2 else -step_bps)) / 1e4)
        out.append(MinuteBar(end - timedelta(minutes=n - 1 - i), p, p * 1.0002, p * 0.9998, p, 10.0))
    return out


# ------------------------------------------------------------------ contract rules
def test_above_contract_verified_with_averaging_window() -> None:
    t = interpret_contract(info())
    assert t.verified and t.kind == "above" and t.threshold == 60000.0
    assert t.averaging_window_sec == 60 and t.settlement_source == "CF Benchmarks BRTI"
    assert t.direction_for_side("yes") == "UP" and t.side_for_direction("DOWN") == "no"
    assert "ABOVE $60,000.00" in t.yes_means


def test_below_contract_maps_yes_to_down() -> None:
    t = interpret_contract(info("less", floor=None, cap=59000.0, rules=RULES.replace("above", "below")))
    assert t.verified and t.kind == "below" and t.threshold == 59000.0
    assert t.direction_for_side("yes") == "DOWN" and t.side_for_direction("UP") == "no"


@pytest.mark.parametrize("kw, problem", [
    (dict(strike_type="between", cap=61000.0), "range"),
    (dict(strike_type="custom"), "unknown strike type"),
    (dict(floor=None), "threshold"),
    (dict(rules=""), "rules text missing"),
    (dict(asset="ETH"), "not a Bitcoin market"),
    (dict(rules=RULES.replace("above", "below")), "BELOW condition"),
    (dict(rules="If the average of Bitcoin prices during the final window is above 60000, Yes."), "averaging window"),
    (dict(rules="If the S&P 500 closes above 6000, Yes."), "does not refer to Bitcoin"),
])
def test_unverifiable_contracts_are_rejected(kw: dict, problem: str) -> None:
    t = interpret_contract(info(**kw))
    assert not t.verified and problem in " ".join(t.problems)


def test_research_mode_may_assume_rules_but_live_may_not() -> None:
    assert not interpret_contract(info(rules="")).verified
    t = interpret_contract(info(rules=""), require_rules=False, assumed_avg_window_sec=60)
    assert t.verified and t.averaging_window_sec == 60 and t.notes


@pytest.mark.parametrize("text, sec", [("average of the 60 seconds before close", 60),
                                       ("simple average over one minute prior", 60),
                                       ("average of the final 5 minutes", 300)])
def test_averaging_window_parsing(text: str, sec: int) -> None:
    assert interpret_contract(info(rules=f"Bitcoin BRTI {text} above 60000")).averaging_window_sec == sec


# ------------------------------------------------------------------ BTC feed
class Clock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t


def feed_with(responses: dict[str, object], clock: Clock) -> BTCFeed:
    def handler(req: httpx.Request) -> httpx.Response:
        for key, body in responses.items():
            if req.url.path.endswith(key):
                return httpx.Response(200, json=body)
        return httpx.Response(404)
    return BTCFeed(stale_after_sec=15, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)), clock=clock)


async def test_feed_connected_then_stale() -> None:
    clock = Clock(NOW)
    t0 = int((NOW - timedelta(minutes=3)).timestamp())
    candles = [[t0, 1, 2, 1.5, 1.6, 3], [t0 + 60, 1, 2, 1.6, 1.7, 3], [t0 + 120, 1, 2, 1.7, 1.8, 3],
               [t0 + 180, 1, 2, 1.8, 1.9, 3]]  # the last one is still forming at NOW
    f = feed_with({"/ticker": {"price": "60123.5", "bid": "60123", "ask": "60124",
                               "time": (NOW - timedelta(seconds=2)).isoformat()}, "/candles": candles}, clock)
    sp = await f.get_price("BTC")
    assert sp is not None and sp.price == 60123.5 and f.status().state == "CONNECTED"
    assert [b.ts for b in f.bars()] == [datetime.fromtimestamp(t0 + 60 * k + 60, UTC) for k in range(3)]
    assert await f.get_price("ETH") is None
    clock.t = NOW + timedelta(seconds=30)
    assert f.current() is None and f.status().state == "STALE"
    await f.close()


@pytest.mark.parametrize("ticker, why", [
    ({"price": "-1", "time": NOW.isoformat()}, "non-positive"),
    ({"price": "60000", "bid": "60010", "ask": "60000", "time": NOW.isoformat()}, "crossed"),
    ({"price": "60000", "time": (NOW + timedelta(minutes=5)).isoformat()}, "future"),
    ({"price": "60000"}, "timestamp"),
])
async def test_feed_rejects_invalid_prints(ticker: dict, why: str) -> None:
    f = feed_with({"/ticker": ticker, "/candles": []}, Clock(NOW))
    assert await f.get_price("BTC") is None
    assert why in (f.status().error or "") and not f.status().ok
    await f.close()


def test_candle_parser_drops_bad_rows() -> None:
    rows = [[0, 1, 2, 1, 1.5, 3], [60, -1, 2, 1, 1.5, 3], "junk", [120, 3, 2, 1, 1.5, 3]]
    assert len(parse_candles(rows)) == 1


# ------------------------------------------------------------------ probability model
def test_probability_basics() -> None:
    assert p_above(60000, 60000, 1e-4, 300) == pytest.approx(0.5)
    assert p_above(60300, 60000, 1e-4, 300) > p_above(60100, 60000, 1e-4, 300) > 0.5
    assert p_above(60300, 60000, 1e-4, 60) > p_above(60300, 60000, 1e-4, 600)  # less time, less uncertainty
    assert effective_horizon(600, 0) == 600
    assert effective_horizon(600, 60) == pytest.approx(540 + 20)
    assert effective_horizon(30, 60) is None


def test_estimate_interval_and_below_symmetry() -> None:
    b = bars()
    up = estimate_probability(interpret_contract(info()), 60300, b, NOW, 540)
    assert up.ok and up.p_yes_low <= up.p_yes <= up.p_yes_high
    down = estimate_probability(interpret_contract(info("less", floor=None, cap=60000.0,
                                                        rules=RULES.replace("above", "below"))), 60300, b, NOW, 540)
    assert down.p_yes == pytest.approx(1 - up.p_yes)
    few = estimate_probability(interpret_contract(info()), 60300, b[-25:], NOW, 540,
                               ModelConfig(sigma_window_min=60, min_bars=20))
    assert few.width > up.width  # fewer returns -> wider (more uncertain) range


def test_no_probability_without_data_or_inside_averaging_window() -> None:
    t = interpret_contract(info())
    assert not estimate_probability(t, None, bars(), NOW, 540).ok
    assert not estimate_probability(t, 60300, bars(5), NOW, 540).ok
    assert "averaging" in " ".join(estimate_probability(t, 60300, bars(), NOW, 40).problems)
    assert not estimate_probability(interpret_contract(info(rules="")), 60300, bars(), NOW, 540).ok


def test_features_and_estimate_ignore_future_bars() -> None:
    past = bars()
    future = [MinuteBar(NOW + timedelta(minutes=k), 90000, 90000, 90000, 90000, 999) for k in range(1, 30)]
    t = interpret_contract(info())
    f1 = compute_btc_features(past, 60300, NOW, threshold=60000, t_left_sec=540, avg_window_sec=60)
    f2 = compute_btc_features(past + future, 60300, NOW, threshold=60000, t_left_sec=540, avg_window_sec=60)
    assert f1.as_dict() == f2.as_dict()
    assert estimate_probability(t, 60300, past, NOW, 540) == estimate_probability(t, 60300, past + future, NOW, 540)


def test_regimes() -> None:
    assert classify_regime(compute_btc_features(bars(10), 60300, NOW)) is BTCRegime.INSUFFICIENT_DATA
    assert classify_regime(compute_btc_features(bars(), 60300, NOW)) is BTCRegime.SIDEWAYS
    trend = bars(drift_bps=4.0, step_bps=1.0)
    assert classify_regime(compute_btc_features(trend, trend[-1].close, NOW)) is BTCRegime.STRONG_UPTREND
    shock = bars()
    shock[-1] = replace(shock[-1], close=shock[-2].close * 1.01)
    assert classify_regime(compute_btc_features(shock, shock[-1].close, NOW)) is BTCRegime.HIGH_VOLATILITY


# ------------------------------------------------------------------ edge, fees, decisions
def params(**kw) -> BTCParams:
    base = dict(min_ev_cents=2.0, min_strength=30, max_prob_width=0.2, max_spread_cents=6, min_liquidity=10,
                min_time_sec=120, blocked_regimes=frozenset({"HIGH_VOLATILITY", "INSUFFICIENT_DATA"}),
                max_late_move_cents=15, require_calibration=True)
    base.update(kw)
    return BTCParams(**base)


def inputs(yes_ask: float = 70, yes_bid: float = 68, t_left: float = 540, spot: float = 60300.0, **kw) -> BTCInputs:
    m = info(t_left=t_left)
    t = interpret_contract(m)
    b = bars(price=spot)
    f = compute_btc_features(b, spot, NOW, threshold=t.threshold, t_left_sec=t_left, avg_window_sec=60)
    base = dict(now=NOW, terms=t, features=f, regime=classify_regime(f), estimate=estimate_probability(t, spot, b, NOW, t_left),
                yes_ask=yes_ask, no_ask=100 - yes_bid, yes_bid=yes_bid, no_bid=100 - yes_ask, t_left_sec=t_left,
                kalshi_ts=NOW, btc_ts=NOW, liquidity={"yes": 100.0, "no": 100.0}, calibration=CAL_OK)
    base.update(kw)
    return BTCInputs(**base)


def test_fee_and_ev_formulas() -> None:
    assert fee_cents(50, 0.07) == pytest.approx(1.75)
    assert estimate_fee(10, 50, 0.07) == 0.18  # ceil(0.175) to the cent
    inp = inputs()
    yes = next(e for e in side_edges(inp, params()) if e.side == "yes")
    assert yes.cost_cents == pytest.approx(70 + 1 + fee_cents(70, 0.07))
    assert yes.ev_cents == pytest.approx(yes.p_point * 100 - yes.cost_cents)
    assert yes.ev_low_cents <= yes.ev_cents


def test_good_setup_is_approved() -> None:
    sig = decide(inputs(), params())
    assert sig.approved and sig.direction == "UP" and sig.side == "yes"
    assert sig.edge.ev_low_cents >= 2 and not sig.rejections


@pytest.mark.parametrize("kw, prm, code", [
    (dict(kalshi_ts=NOW - timedelta(seconds=90)), {}, NoTrade.STALE_DATA),
    (dict(btc_ts=None), {}, NoTrade.STALE_DATA),
    (dict(btc_ts=NOW - timedelta(seconds=10), kalshi_ts=NOW + timedelta(seconds=25)), {"kalshi_stale_sec": 99},
     NoTrade.STALE_DATA),
    (dict(t_left=100), {}, NoTrade.INSUFFICIENT_TIME),
    (dict(yes_bid=60), {}, NoTrade.EXCESSIVE_SPREAD),
    (dict(liquidity={"yes": 3.0, "no": 3.0}), {}, NoTrade.INSUFFICIENT_LIQUIDITY),
    (dict(regime=BTCRegime.HIGH_VOLATILITY), {}, NoTrade.UNFAVORABLE_REGIME),
    ({}, {"max_prob_width": 0.0001}, NoTrade.EXCESSIVE_UNCERTAINTY),
    (dict(kalshi_move_5m=-12.0), {"conflict_mom_z": -1.0}, NoTrade.CONFLICT),
    (dict(kalshi_move_5m=25.0), {"min_ev_cents": 20.0}, NoTrade.PRICE_REFLECTS_MOVE),
    ({}, {"min_strength": 101}, NoTrade.WEAK_DIRECTION),
    ({}, {"min_entry_cents": 95.0}, NoTrade.LONGSHOT),
    (dict(yes_ask=99, yes_bid=97), {}, NoTrade.INSUFFICIENT_EV),
    (dict(calibration=CalibrationState("INSUFFICIENT", 4)), {}, NoTrade.INSUFFICIENT_EVIDENCE),
    (dict(calibration=CalibrationState("NOT_CALIBRATED", 80, 0.12)), {}, NoTrade.NOT_CALIBRATED),
    (dict(paused_reason="volatility 99th pct"), {}, NoTrade.TRADING_PAUSED),
])
def test_each_no_trade_rule(kw: dict, prm: dict, code: NoTrade) -> None:
    move = kw.pop("kalshi_move_5m", None)
    inp = inputs(**{k: v for k, v in kw.items() if k in ("yes_ask", "yes_bid", "t_left", "spot")},
                 **{k: v for k, v in kw.items() if k not in ("yes_ask", "yes_bid", "t_left", "spot")})
    if move is not None:
        inp = replace(inp, kalshi_move_5m=move, features=replace(inp.features, mom_z_5m=1.5, ret_5m_bps=20.0))
    sig = decide(inp, params(**prm))
    assert not sig.approved and sig.direction == "WAIT"
    assert code in {r.code for r in sig.rejections if r.critical}, [r.text for r in sig.rejections]


def test_unverified_contract_never_trades() -> None:
    t = interpret_contract(info(rules=""))
    sig = decide(replace(inputs(), terms=t, estimate=estimate_probability(t, 60300, bars(), NOW, 540)), params())
    assert not sig.approved and sig.rejections[0].code is NoTrade.SETTLEMENT_UNVERIFIED


def test_calibration_gate_depends_on_profile_and_mode() -> None:
    raw = inputs(calibration=CalibrationState("INSUFFICIENT", 3))
    assert not decide(raw, params(require_calibration=True)).approved
    paper_high = decide(raw, params(require_calibration=False))
    assert paper_high.approved and paper_high.warnings  # HIGH paper may collect evidence, with a warning
    assert not decide(replace(raw, live=True), params(require_calibration=False)).approved  # never live


# ------------------------------------------------------------------ risk profiles & sizing
def test_btc_profile_fields_are_bounded() -> None:
    profiles = load_profiles(read_env=False)
    low, high = profiles[RiskLevel.LOW], profiles[RiskLevel.HIGH]
    assert low.min_ev_cents > high.min_ev_cents and low.min_btc_strength > high.min_btc_strength
    assert low.max_prob_width < high.max_prob_width and low.kelly_fraction < high.kelly_fraction
    assert "INSUFFICIENT_DATA" in high.regimes_blocked  # HIGH never trades without data
    with pytest.raises(ConfigError):
        high.with_overrides({"kelly_fraction": 0.9})
    with pytest.raises(ConfigError):
        high.with_overrides({"max_consecutive_losses": 50})


def test_sizing_kelly_and_daily_loss_caps() -> None:
    prof = load_profiles(read_env=False)[RiskLevel.HIGH]
    s = PositionSizer(0.07)
    base = dict(price_cents=60, confidence=100, balance_usd=1000, open_exposure_usd=0, liquidity_contracts=None,
                scale=1.0)
    free = s.size(prof, **base)
    kelly = s.size(prof, **base, p_win=0.65)
    assert 0 < kelly.contracts < free.contracts and kelly.limiting_factor == "kelly cap"
    assert s.size(prof, **base, p_win=0.55).contracts == 0  # no edge after costs -> no stake
    capped = s.size(prof, **base, remaining_daily_loss_usd=3.0)
    assert capped.total_usd <= 3.0 and capped.limiting_factor == "remaining daily loss"


def _btc_signal_result(btc, ticker: str) -> SignalResult:
    return SignalResult(market_ticker=ticker, asset="BTC", label="BTC 15M", direction=Direction.WAIT,
                        leaning=Direction.WAIT, confidence=0, score=0.0, reasons=(), timestamp=NOW, btc=btc)


def _snap(m: MarketInfo, yes_ask: float = 70, yes_bid: float = 68) -> MarketSnapshot:
    book = OrderBook(yes_bids=((yes_bid, 200.0),), no_bids=((100 - yes_ask, 200.0),))
    return MarketSnapshot(info=m, ts=NOW, yes_bid=yes_bid, yes_ask=yes_ask, no_bid=100 - yes_ask,
                          no_ask=100 - yes_bid, orderbook=book)


def ctx(**kw) -> RiskContext:
    base = dict(trading_enabled=True, disabled_reason="", mode="paper", mode_permitted=True, balance_usd=1000.0,
                open_positions=0, open_exposure_usd=0.0, open_tickers=frozenset(), inflight_tickers=frozenset(),
                daily_realized_pnl=0.0)
    base.update(kw)
    return RiskContext(**base)


def test_risk_manager_btc_mode_approves_and_enforces_account_limits() -> None:
    prof = load_profiles(read_env=False)[RiskLevel.HIGH]
    rm = RiskManager(PositionSizer(0.07), stale_after_sec=30, price_buffer_cents=2, strategy_mode="btc")
    m = info()
    sig = _btc_signal_result(decide(inputs(), params()), m.ticker)
    ok = rm.evaluate(sig, _snap(m), prof, ctx(), now=NOW)
    assert ok.approved and ok.side == "yes" and ok.position_size >= 1, ok.failures
    assert any(c.label == "Net edge (conservative)" for c in ok.checklist)
    lost = rm.evaluate(sig, _snap(m), prof, ctx(daily_realized_pnl=-prof.max_daily_loss_usd), now=NOW)
    assert not lost.approved and "Daily loss limit" in lost.reason
    streak = rm.evaluate(sig, _snap(m), prof, ctx(consecutive_losses=prof.max_consecutive_losses,
                                                  last_loss_at=NOW - timedelta(minutes=5)), now=NOW)
    assert not streak.approved and any("losses in a row" in f for f in streak.failures)
    full = rm.evaluate(sig, _snap(m), prof, ctx(open_positions=prof.max_open_positions), now=NOW)
    assert not full.approved and any("Max open positions" in f for f in full.failures)
    rejected = rm.evaluate(_btc_signal_result(decide(inputs(yes_ask=99, yes_bid=97), params()), m.ticker),
                           _snap(m, 99, 97), prof, ctx(), now=NOW)
    assert not rejected.approved and rejected.position_size == 0


def test_btc_mode_is_bitcoin_only_and_live_needs_manual_confirmation(repo) -> None:
    from app.data.btc_feed import FixtureBTCFeed
    from app.kalshi.fixtures import FixtureKalshiClient
    from app.runtime import BotRuntime

    s = make_settings(strategy_mode="btc", assets="BTC,ETH,SOL")
    assert s.asset_list == ["BTC"] and any("Bitcoin only" in w for w in s.validate_runtime())
    rt = BotRuntime(s, repo, FixtureKalshiClient(), underlying=FixtureBTCFeed(), profiles=load_profiles(read_env=False))
    rt.store.update(mode="live")
    ok, msg = rt.set_auto_trade(True)
    assert not ok and "manual confirmation" in msg
    rt.set_risk_level(RiskLevel.HIGH)
    assert rt.state.mode == "live" and not rt.state.auto_trade  # risk changes never touch mode/auto


async def test_end_to_end_paper_trade_from_btc_signal(repo) -> None:
    from app.kalshi.fixtures import FixtureKalshiClient
    from app.research.btc_calibration import CalibrationReport
    from app.runtime import BotRuntime

    class Feed:
        name = "test"

        def __init__(self) -> None:
            self._bars = bars(end=datetime.now(UTC).replace(second=0, microsecond=0))

        async def get_price(self, asset: str):
            from app.data.underlying import SpotPrice
            return SpotPrice("BTC", 60300.0, datetime.now(UTC), "test")

        def history(self, asset: str):
            return []

        def bars(self):
            return self._bars

        def status(self):
            return None

        async def close(self) -> None:
            return None

    s = make_settings(strategy_mode="btc", auto_trading=True, default_risk_level="high")
    rt = BotRuntime(s, repo, FixtureKalshiClient(), underlying=Feed(), profiles=load_profiles(read_env=False))
    rt.calibration = CalibrationReport(CAL_OK, (), 120, 1000)
    rt.store.update(running=True, auto_trade=True)
    now = datetime.now(UTC)
    m = replace(info(), open_time=now - timedelta(seconds=360), close_time=now + timedelta(seconds=540))
    rt.discovery.state.current["BTC"] = m
    rt.discovery.state.by_ticker[m.ticker] = m
    snap = replace(_snap(m), ts=now)
    rt.market_data.latest[m.ticker] = snap

    async def fresh_poll(_info):  # risk is re-checked on FRESH quotes right before the order
        rt.market_data.latest[m.ticker] = replace(snap, ts=datetime.now(UTC))
        return rt.market_data.latest[m.ticker]

    rt.market_data.poll = fresh_poll  # type: ignore[method-assign]
    sig = await rt.process_snapshot(snap)
    assert sig.btc is not None and sig.btc.approved, [r.text for r in sig.btc.rejections]
    pos = rt.portfolio.open_positions("paper")
    assert len(pos) == 1 and pos[0].side == "yes" and pos[0].entry_price == 71.0  # ask + 1c slippage
    assert repo.observations(source="btc", settled_only=False)  # every evaluation is recorded
    pnl = rt.paper.settle(pos[0], "yes")
    assert pnl == pytest.approx(pos[0].contracts * (100 - 71) / 100 - pos[0].entry_fee, abs=0.01)


# ------------------------------------------------------------------ calibration
class Obs:
    def __init__(self, ticker: str, p: float, outcome: str, mid: float | None = None) -> None:
        self.ticker, self.outcome = ticker, outcome
        self.features = {"p_yes": p, **({"yes_mid": mid} if mid is not None else {})}


def test_calibration_counts_markets_once_and_needs_enough_data() -> None:
    few = calibration_report([Obs("A", 0.7, "yes")] * 5, min_markets=30)
    assert few.state.status == "INSUFFICIENT" and few.markets == 1
    good = [Obs(f"M{i}", 0.75, "yes" if i % 4 else "no", mid=60) for i in range(40)]
    rep = calibration_report(good, min_markets=30, ece_tol=0.05)
    assert rep.state.status == "CALIBRATED" and rep.buckets[2].markets == 40
    assert rep.buckets[2].actual == pytest.approx(0.75)
    bad = [Obs(f"M{i}", 0.95, "yes" if i % 2 else "no") for i in range(40)]
    assert calibration_report(bad, min_markets=30).state.status == "NOT_CALIBRATED"


# ------------------------------------------------------------------ backtest integrity & P&L
def test_trade_pnl_accounting_and_metrics() -> None:
    st = type("S", (), {"outcome": "yes", "ts": NOW, "day": "d", "regime": BTCRegime.SIDEWAYS, "t_left": 300})()
    win = Trade(st, "yes", 60, 1, 1.68)
    loss = Trade(st, "no", 40, 1, 1.68)
    assert win.gross == pytest.approx(0.40) and win.net == pytest.approx(0.40 - 0.0268)
    assert loss.gross == pytest.approx(-0.40) and loss.net == pytest.approx(-0.4268)
    m = metrics([win, loss, loss, win])
    assert m["trades"] == 4 and m["wins"] == 2 and m["longest_losing_streak"] == 2
    assert m["net_pnl"] == pytest.approx(2 * win.net + 2 * loss.net)
    assert m["fees"] == pytest.approx(4 * 0.0168) and m["slippage"] == pytest.approx(0.04)
    assert m["max_drawdown"] == pytest.approx(-2 * loss.net)


@pytest.fixture(scope="module")
def synthetic(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("syn")
    generate(path, days=6, mode="lagged")
    return path


def test_backtest_is_chronological_and_out_of_sample(synthetic: Path) -> None:
    ds = load_dataset(synthetic)
    days = ds.days
    assert len(days) >= 6 and all(s.ts <= s.close for steps in ds.markets.values() for s in steps)
    costs = SimCosts()
    sel = v_model_edge(params_for(load_profiles(read_env=False)[RiskLevel.MEDIUM], costs))
    wf = walk_forward(ds, "edge", sel, (0.0, 2.0, 5.0), costs, train_days=2, val_days=1, min_train_trades=5)
    tested = [d for d, _ in wf.chosen]
    assert tested == days[3:]  # every test day comes after its training + validation days
    assert all(t.step.day in tested for t in wf.trades)
    assert len({t.step.ticker for t in wf.trades}) == len(wf.trades)  # one trade per market

    # Outcome leakage check: flipping the results of the LAST test day cannot change the
    # parameter chosen for that day (it is picked on earlier days only).
    last = days[-1]
    flipped = {t: [replace(s, outcome="no" if s.outcome == "yes" else "yes") if s.day == last else s for s in steps]
               for t, steps in ds.markets.items()}
    wf2 = walk_forward(replace(ds, markets=flipped), "edge", sel, (0.0, 2.0, 5.0), costs, 2, 1, min_train_trades=5)
    assert dict(wf.chosen)[last] == dict(wf2.chosen)[last]


def test_backtest_entries_use_the_ask_and_one_trade_per_market(synthetic: Path) -> None:
    ds = load_dataset(synthetic)
    costs = SimCosts(slippage_cents=1.0)
    trades = simulate(ds.markets, lambda st, p, c: ("yes", None), 0.0, costs)
    assert len(trades) == len(ds.markets)
    for t in trades[:50]:
        assert t.ask == t.step.yes_ask and t.slippage <= 1.0 and t.step.t_left >= costs.min_t_left


def test_report_runs_and_is_labelled(synthetic: Path, tmp_path: Path) -> None:
    from app.research.btc_backtest import format_report, load_report, run, save_report

    rep = run(load_dataset(synthetic), train_days=2, val_days=1)
    assert rep["label"].startswith("BACKTEST") and "verdict" in rep and rep["component_tests"]
    for key in ("trades", "wins", "losses", "win_rate", "gross_pnl", "fees", "slippage", "net_pnl",
                "avg_profit_per_trade", "expected_value_per_trade", "profit_factor", "max_drawdown",
                "longest_losing_streak", "return_on_capital"):
        assert key in rep["variants"]["full strategy"]["oos"]
    assert set(rep["full_by_risk_mode"]) == {"low", "medium", "high"}
    path = tmp_path / "r.json"
    save_report(rep, path)
    assert load_report(path)["verdict"] == rep["verdict"] and "VERDICT" in format_report(rep)
    json.loads(path.read_text())


def test_trade_cards_layout():
    from app.telegram import messages as M
    from app.trading.positions import Position

    def pos(**kw):
        p = Position(mode="paper", ticker="KXBTC15M-X", event_ticker="E", asset="BTC", label="BTC 15M",
                     side="no", contracts=1, entry_price=44.0, entry_fee=0.02, risk_level="low", id=1)
        for k, v in kw.items():
            setattr(p, k, v)
        return p

    opened = M.trade_opened_alert(pos(), "DOWN", "bot's estimate 62%+ vs price 44¢")
    assert "Trade Taken" in opened and "BTC ▼ DOWN" in opened and "1 share @ 44¢" in opened
    assert "total $0.46" in opened and "Read:" in opened
    won = M.trade_closed_alert(pos(status="closed", exit_price=100.0, realized_pnl=0.54, close_reason="settled NO"),
                               "DOWN")
    assert "WON" in won and "+$0.54" in won
    lost = M.trade_closed_alert(pos(status="closed", exit_price=0.0, realized_pnl=-0.46, close_reason="settled YES"),
                                "UP")
    assert "Lost" in lost and "−$0.46" in lost and "BTC ▲ UP" in lost
    out = M.trade_closed_alert(pos(status="closed", exit_price=91.0, realized_pnl=0.44, close_reason="manual exit"),
                               "DOWN")
    assert "CASHED OUT" in out and "sold @ 91¢" in out and "Locked in" in out


def test_longshot_guard_blocks_cheap_contracts():
    from app.risk.profiles import RiskLevel, load_profiles

    profiles = load_profiles(read_env=False)
    assert profiles[RiskLevel.LOW].min_entry_price_cents >= 30
    assert profiles[RiskLevel.HIGH].min_entry_price_cents >= 15


async def test_quiet_mode_sends_no_signal_alerts(runtime, monkeypatch):
    from tests.conftest import make_info, quote_snapshot

    sent: list[str] = []

    async def notify(kind, key, text, buttons=None):
        sent.append(kind)

    monkeypatch.setattr(runtime, "notify", notify)
    monkeypatch.setattr(runtime.engine, "detect_flip", lambda sig: object())
    assert runtime.settings.signal_alerts is False
    runtime.store.update(auto_trade=True)
    await runtime.process_snapshot(quote_snapshot(make_info()))
    assert "flip" not in sent and "strong_signal" not in sent


async def test_signal_alerts_on_still_sends_flips(runtime, monkeypatch):
    from tests.conftest import make_info, quote_snapshot

    sent: list[str] = []

    async def notify(kind, key, text, buttons=None):
        sent.append(kind)

    runtime.settings = runtime.settings.model_copy(update={"signal_alerts": True})
    monkeypatch.setattr(runtime, "notify", notify)
    monkeypatch.setattr(runtime.engine, "detect_flip", lambda sig: "flip")
    monkeypatch.setattr("app.runtime.messages.flip_alert", lambda flip: "flip!")
    await runtime.process_snapshot(quote_snapshot(make_info()))
    assert "flip" in sent


def test_seed_calibration_from_history(tmp_path, monkeypatch):
    import importlib.util

    from app.config import Settings
    from app.database.db import Database
    from app.database.repository import Repository
    from app.research.btc_synthetic import generate

    generate(tmp_path / "cache", days=1, mode="efficient")
    db = tmp_path / "t.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    spec = importlib.util.spec_from_file_location("seed", "scripts/btc_seed_calibration.py")
    seed = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seed)
    monkeypatch.setattr("sys.argv", ["seed", "--cache", str(tmp_path / "cache")])
    assert seed.main() == 0
    repo = Repository(Database(Settings(_env_file=None).database_url))
    obs = repo.observations(source="btc")
    assert obs and all(o.features.get("backfill") == 1.0 and o.outcome in ("yes", "no") for o in obs)
    n = len(obs)
    assert seed.main() == 0 and len(repo.observations(source="btc")) == n  # idempotent
