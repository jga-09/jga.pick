"""BTC 15-minute backtest: replays settled Kalshi BTC markets through the live decision code.

Data (from ``scripts/replay.py``'s cache, official APIs only):
* Kalshi: settled BTC 15-minute markets, 1-minute YES bid/ask closes, result.
* Coinbase: 1-minute BTC-USD OHLCV (a proxy for Kalshi's BRTI settlement index).
Missing: Kalshi order-book history (no depth/liquidity checks possible) and
sub-minute quotes. These limits are stated in every report.

Integrity:
* Each minute is evaluated "as of" its candle close: only BTC bars that ended by
  then (``bars_until``) and Kalshi quotes up to then are visible. Results are read
  only when settling trades.
* One trade per market (the first minute a strategy qualifies), entered at the ASK
  plus slippage, with Kalshi's fee formula, paid $1 if right and $0 if wrong.
* Chronological walk-forward by day: parameters are chosen on TRAINING days,
  confirmed on the following VALIDATION days, then applied to the next unseen TEST
  day. Nothing is shuffled. The calibration gate also only uses earlier days.
"""

from __future__ import annotations

import bisect
import gzip
import json
import math
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.data.btc_feed import MinuteBar, parse_candles
from app.kalshi.contract import ContractTerms, interpret_contract
from app.kalshi.market_data import parse_market_info
from app.errors import MalformedResponseError
from app.research.btc_calibration import calibration_report
from app.research.reporting import bootstrap_ev
from app.research.replay import _record_candles
from app.research.stats import diff_z, wilson
from app.risk.profiles import RiskLevel, RiskProfile, load_profiles
from app.risk.sizing import PositionSizer
from app.strategy.btc_decision import UNCALIBRATED, BTCInputs, BTCParams, CalibrationState, decide, side_edges
from app.strategy.btc_model import (
    BTCFeatures,
    BTCRegime,
    ModelConfig,
    ProbabilityEstimate,
    classify_regime,
    compute_btc_features,
    estimate_probability,
)
from app.trading.fees import estimate_fee
from app.utils.time import parse_ts

BAR_LOOKBACK = 400  # minutes of BTC history handed to the feature code per step


# ------------------------------------------------------------------ data
@dataclass
class Step:
    ticker: str
    day: str
    ts: datetime
    close: datetime
    t_left: float
    terms: ContractTerms
    features: BTCFeatures
    regime: BTCRegime
    estimate: ProbabilityEstimate
    yes_bid: float | None
    yes_ask: float | None
    kalshi_move_5m: float | None
    btc_ts: datetime | None
    outcome: str  # yes | no

    @property
    def no_ask(self) -> float | None:
        return None if self.yes_bid is None else 100 - self.yes_bid

    @property
    def no_bid(self) -> float | None:
        return None if self.yes_ask is None else 100 - self.yes_ask

    @property
    def yes_mid(self) -> float | None:
        return None if self.yes_bid is None or self.yes_ask is None else (self.yes_bid + self.yes_ask) / 2


@dataclass
class Dataset:
    markets: dict[str, list[Step]]  # ticker -> steps in time order
    skipped: dict[str, int] = field(default_factory=dict)
    rules_missing: int = 0

    @property
    def days(self) -> list[str]:
        return sorted({s[0].day for s in self.markets.values() if s})

    def by_day(self) -> dict[str, dict[str, list[Step]]]:
        out: dict[str, dict[str, list[Step]]] = defaultdict(dict)
        for t, steps in self.markets.items():
            if steps:
                out[steps[0].day][t] = steps
        return out


def load_bars(cache: Path, asset: str = "BTC") -> list[MinuteBar]:
    bars: dict[datetime, MinuteBar] = {}
    for f in sorted(cache.glob(f"spot_{asset}_*.json.gz")):
        try:
            for b in parse_candles(json.loads(gzip.decompress(f.read_bytes()))):
                bars[b.ts] = b
        except (OSError, ValueError):
            continue
    return [bars[t] for t in sorted(bars)]


def window(bars: Sequence[MinuteBar], keys: Sequence[datetime], as_of: datetime, n: int = BAR_LOOKBACK) -> list[MinuteBar]:
    hi = bisect.bisect_right(keys, as_of)  # bars complete at as_of only
    return list(bars[max(0, hi - n):hi])


def build_market_steps(record: dict[str, Any], bars: Sequence[MinuteBar], keys: Sequence[datetime],
                       cfg: ModelConfig, avg_window_sec: int) -> tuple[list[Step], str | None, bool]:
    """(steps, skip reason, rules missing) for one cached settled market."""
    m = record.get("market") or {}
    if m.get("result") not in ("yes", "no"):
        return [], "not settled yes/no", False
    try:
        info = parse_market_info(dict(m, status="active"), asset="BTC", series_ticker=record.get("series", ""))
    except MalformedResponseError:
        return [], "unparseable market", False
    rules_missing = not (info.rules_primary or info.rules_secondary)
    terms = interpret_contract(info, require_rules=False, assumed_avg_window_sec=avg_window_sec)
    if not terms.verified:
        return [], f"contract not supported: {terms.primary_problem}", rules_missing
    open_t = info.open_time or info.close_time - timedelta(minutes=15)
    candles = [c for c in _record_candles(record) if open_t < c.ts <= info.close_time]
    mids: list[tuple[datetime, float]] = []
    steps = []
    for c in candles:
        if c.yes_bid is None and c.yes_ask is None:
            continue
        if c.yes_bid is not None and c.yes_ask is not None:
            mids.append((c.ts, (c.yes_bid + c.yes_ask) / 2))
        t_left = (info.close_time - c.ts).total_seconds()
        if t_left <= 0:
            continue
        hist = window(bars, keys, c.ts)
        last = hist[-1] if hist else None
        spot = last.close if last is not None and (c.ts - last.ts).total_seconds() <= 120 else None
        feats = compute_btc_features(hist, spot, c.ts, cfg, terms.threshold, t_left, terms.averaging_window_sec)
        est = estimate_probability(terms, spot, hist, c.ts, t_left, cfg)
        prior = [v for t, v in mids if t <= c.ts - timedelta(seconds=240)]
        km = (mids[-1][1] - prior[-1]) if prior and mids and mids[-1][0] == c.ts else None
        steps.append(Step(info.ticker, info.close_time.strftime("%Y-%m-%d"), c.ts, info.close_time, t_left, terms,
                          feats, classify_regime(feats, cfg), est, c.yes_bid, c.yes_ask, km,
                          last.ts if last is not None else None, m["result"]))
    return steps, None if steps else "no usable minutes", rules_missing


def load_dataset(cache: Path, cfg: ModelConfig | None = None, avg_window_sec: int = 60) -> Dataset:
    cfg = cfg or ModelConfig()
    bars = load_bars(cache)
    keys = [b.ts for b in bars]
    ds = Dataset({})
    skipped: dict[str, int] = defaultdict(int)
    for f in sorted(cache.glob("*.json.gz")):
        if f.name.startswith("spot_"):
            continue
        try:
            rec = json.loads(gzip.decompress(f.read_bytes()))
        except (OSError, ValueError):
            skipped["unreadable file"] += 1
            continue
        if not str(rec.get("series", "")).upper().startswith("KXBTC"):
            continue
        steps, why, missing = build_market_steps(rec, bars, keys, cfg, avg_window_sec)
        ds.rules_missing += int(missing)
        if why:
            skipped[why] += 1
        else:
            ds.markets[steps[0].ticker] = steps
    ds.skipped = dict(skipped)
    return ds


# ------------------------------------------------------------------ trading
@dataclass(frozen=True)
class Trade:
    step: Step
    side: str
    ask: float  # cents
    slippage: float  # cents per contract
    fee: float  # cents per contract
    contracts: int = 1
    variant: str = ""
    p_side: float | None = None
    strength: int | None = None

    @property
    def won(self) -> bool:
        return self.step.outcome == self.side

    @property
    def gross(self) -> float:  # dollars, before fees and slippage
        return self.contracts * ((100 if self.won else 0) - self.ask) / 100

    @property
    def costs(self) -> float:
        return self.contracts * (self.fee + self.slippage) / 100

    @property
    def net(self) -> float:
        return self.gross - self.costs


Selector = Callable[[Step, float, CalibrationState], tuple[str | None, int | None]]


@dataclass(frozen=True)
class SimCosts:
    fee_rate: float = 0.07
    slippage_cents: float = 1.0
    order_size: int = 10  # fees round up per order; per-contract fee is taken from an order of this size
    min_t_left: float = 120.0


def fee_per_contract(price: float, costs: SimCosts) -> float:
    return estimate_fee(costs.order_size, price, costs.fee_rate) * 100 / costs.order_size


def _side_dir(step: Step, direction: str) -> str | None:
    return step.terms.side_for_direction(direction)


def simulate(markets: dict[str, list[Step]], select: Selector, param: float, costs: SimCosts,
             calibration: CalibrationState = UNCALIBRATED, variant: str = "") -> list[Trade]:
    trades = []
    for steps in markets.values():
        for st in steps:
            if st.t_left < costs.min_t_left:
                continue
            side, strength = select(st, param, calibration)
            if side is None:
                continue
            ask = st.yes_ask if side == "yes" else st.no_ask
            if ask is None or not 1 <= ask <= 99:
                continue
            fill = min(99.0, ask + costs.slippage_cents)
            probs = st.estimate.p_side(side)
            trades.append(Trade(st, side, ask, fill - ask, fee_per_contract(fill, costs), 1, variant,
                                probs[0] if probs else None, strength))
            break
    return sorted(trades, key=lambda t: t.step.ts)


# ---- strategy variants (each takes one tunable parameter) ------------------
def v_baseline(st: Step, _: float, __: CalibrationState) -> tuple[str | None, int | None]:
    """Naive: whatever side BTC is currently on relative to the threshold persists."""
    d = st.features.dist_bps
    return (None, None) if d is None or d == 0 else (_side_dir(st, "UP" if d > 0 else "DOWN"), None)


def _mom(st: Step, z: float) -> str | None:
    m = st.features.mom_z_5m
    if m is None or abs(m) < z:
        return None
    return "UP" if m > 0 else "DOWN"


def v_momentum(st: Step, z: float, _: CalibrationState) -> tuple[str | None, int | None]:
    d = _mom(st, z)
    return (_side_dir(st, d), None) if d else (None, None)


def v_mom_trend(st: Step, z: float, _: CalibrationState) -> tuple[str | None, int | None]:
    d, ts = _mom(st, z), st.features.trend_strength
    if d is None or ts is None or abs(ts) < 0.3 or (ts > 0) != (d == "UP"):
        return None, None
    return _side_dir(st, d), None


def v_mom_vol(st: Step, z: float, _: CalibrationState) -> tuple[str | None, int | None]:
    if st.regime in (BTCRegime.HIGH_VOLATILITY, BTCRegime.RAPID_REVERSAL, BTCRegime.INSUFFICIENT_DATA):
        return None, None
    return v_momentum(st, z, _)


def v_mom_price(st: Step, z: float, _: CalibrationState) -> tuple[str | None, int | None]:
    d = _mom(st, z)
    if d is None:
        return None, None
    side = _side_dir(st, d)
    ask = st.yes_ask if side == "yes" else st.no_ask
    spread = None if st.yes_ask is None or st.yes_bid is None else st.yes_ask - st.yes_bid
    km = st.kalshi_move_5m
    side_move = None if km is None else (km if side == "yes" else -km)
    if ask is None or ask > 75 or spread is None or spread > 6 or (side_move is not None and side_move >= 15):
        return None, None
    return side, None


def v_mom_model(costs: SimCosts) -> Selector:
    """Momentum direction, taken only when the model's point EV after costs is positive."""
    def sel(st: Step, z: float, _: CalibrationState) -> tuple[str | None, int | None]:
        d = _mom(st, z)
        if d is None or not st.estimate.ok:
            return None, None
        side = _side_dir(st, d)
        ask = st.yes_ask if side == "yes" else st.no_ask
        probs = st.estimate.p_side(side) if side else None
        if ask is None or probs is None:
            return None, None
        fill = min(99.0, ask + costs.slippage_cents)
        if probs[0] * 100 - fill - fee_per_contract(fill, costs) <= 0:
            return None, None
        return side, None
    return sel


def _inputs(st: Step, cal: CalibrationState) -> BTCInputs:
    return BTCInputs(now=st.ts, terms=st.terms, features=st.features, regime=st.regime, estimate=st.estimate,
                     yes_ask=st.yes_ask, no_ask=st.no_ask, yes_bid=st.yes_bid, no_bid=st.no_bid,
                     t_left_sec=st.t_left, kalshi_ts=st.ts, btc_ts=st.btc_ts, liquidity={},
                     kalshi_move_5m=st.kalshi_move_5m, calibration=cal)


def v_model_edge(params: BTCParams) -> Selector:
    def sel(st: Step, margin: float, _: CalibrationState) -> tuple[str | None, int | None]:
        if not st.estimate.ok:
            return None, None
        edges = side_edges(_inputs(st, UNCALIBRATED), params)
        best = max(edges, key=lambda e: e.ev_low_cents) if edges else None
        return (best.side, None) if best and best.ev_low_cents >= margin else (None, None)
    return sel


def v_full(params: BTCParams) -> Selector:
    """The complete live decision engine (all no-trade filters) with min edge = ``margin``."""
    def sel(st: Step, margin: float, cal: CalibrationState) -> tuple[str | None, int | None]:
        sig = decide(_inputs(st, cal), replace(params, min_ev_cents=margin))
        return (sig.side, sig.strength) if sig.approved else (None, None)
    return sel


MOM_GRID = (0.5, 1.0, 1.5, 2.0)
EDGE_GRID = (0.0, 1.0, 2.0, 3.0, 5.0, 8.0)


def params_for(profile: RiskProfile, costs: SimCosts, **over: Any) -> BTCParams:
    base = BTCParams(
        min_ev_cents=profile.min_ev_cents, min_strength=profile.min_btc_strength,
        max_prob_width=profile.max_prob_width, max_spread_cents=profile.max_spread_cents,
        min_liquidity=profile.min_liquidity_contracts, min_time_sec=profile.min_time_remaining_sec,
        blocked_regimes=profile.regimes_blocked, max_late_move_cents=profile.max_late_move_cents,
        require_calibration=False,  # applied separately by the walk-forward (earlier days only)
        fee_rate=costs.fee_rate, slippage_cents=costs.slippage_cents, kalshi_stale_sec=90, btc_stale_sec=150,
        max_skew_sec=150, min_entry_cents=profile.min_entry_price_cents, max_entry_cents=profile.max_entry_price_cents,
    )
    return replace(base, **over)


# ------------------------------------------------------------------ metrics
def _bucket(v: float | None, edges: Sequence[float], fmt: str) -> str:
    if v is None:
        return "n/a"
    for lo, hi in zip(edges, edges[1:], strict=False):
        if lo <= v < hi:
            return fmt.format(lo, hi)
    return fmt.format(edges[-2], edges[-1])


def metrics(trades: Sequence[Trade], capital: float = 100.0) -> dict[str, Any]:
    n = len(trades)
    nets = [t.net for t in trades]
    wins = sum(1 for t in trades if t.won)
    eq = peak = dd = 0.0
    streak = worst = 0
    for x in nets:
        eq += x
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
        streak = streak + 1 if x <= 0 else 0
        worst = max(worst, streak)
    gw, gl = sum(x for x in nets if x > 0), -sum(x for x in nets if x <= 0)
    lo, hi = wilson(wins, n) if n else (0.0, 1.0)
    out: dict[str, Any] = {
        "trades": n, "wins": wins, "losses": n - wins,
        "win_rate": wins / n if n else None, "win_rate_95": [lo, hi],
        "gross_pnl": round(sum(t.gross for t in trades), 4),
        "fees": round(sum(t.contracts * t.fee / 100 for t in trades), 4),
        "slippage": round(sum(t.contracts * t.slippage / 100 for t in trades), 4),
        "net_pnl": round(sum(nets), 4),
        "avg_profit_per_trade": (sum(nets) / n) if n else None,
        "profit_factor": (gw / gl) if gl > 0 else (None if not gw else math.inf),
        "max_drawdown": round(dd, 4),
        "longest_losing_streak": worst,
        "return_on_capital": (sum(nets) / capital) if capital else None,
        "contracts": sum(t.contracts for t in trades),
        "avg_entry_cents": (sum(t.ask for t in trades) / n) if n else None,
    }
    out["expected_value_per_trade"] = out["avg_profit_per_trade"]
    if n >= 10:
        b_lo, b_hi, p_neg = bootstrap_ev(nets)
        out["ev_95"] = [b_lo, b_hi]
        out["p_ev_le_0"] = p_neg
    out["sufficient"] = n >= 30
    return out


def breakdowns(trades: Sequence[Trade]) -> dict[str, dict[str, Any]]:
    keys: dict[str, Callable[[Trade], str]] = {
        "regime": lambda t: t.step.regime.value,
        "entry_price": lambda t: _bucket(t.ask, (0, 30, 45, 55, 65, 75, 85, 100), "{:.0f}-{:.0f}¢"),
        "time_remaining": lambda t: _bucket(t.step.t_left / 60, (0, 3, 6, 9, 12, 16), "{:.0f}-{:.0f}m"),
        "probability": lambda t: _bucket(t.p_side, (0, 0.5, 0.6, 0.7, 0.8, 0.9, 1.01), "{:.0%}-{:.0%}"),
        "signal_strength": lambda t: _bucket(t.strength, (0, 40, 55, 70, 85, 101), "{:.0f}-{:.0f}"),
    }
    out = {}
    for name, key in keys.items():
        groups: dict[str, list[Trade]] = defaultdict(list)
        for t in trades:
            groups[key(t)].append(t)
        out[name] = {k: {m: v for m, v in metrics(g).items() if m in ("trades", "win_rate", "net_pnl",
                                                                      "avg_profit_per_trade")}
                     for k, g in sorted(groups.items())}
    return out


# ------------------------------------------------------------------ walk-forward
@dataclass
class WFResult:
    name: str
    trades: list[Trade]
    chosen: list[tuple[str, float | None]]  # (test day, parameter or None = sat out)


def _calibration_before(by_day: dict[str, dict[str, list[Step]]], days: Sequence[str], min_markets: int,
                        tol: float) -> CalibrationState:
    obs = []
    for d in days:
        for steps in by_day[d].values():
            for st in steps:
                if st.estimate.p_yes is not None:
                    obs.append(_Obs(st.ticker, st.estimate.p_yes, st.yes_mid, st.outcome))
    return calibration_report(obs, min_markets, tol).state


@dataclass(frozen=True)
class _Obs:
    ticker: str
    p: float
    mid: float | None
    outcome: str

    @property
    def features(self) -> dict[str, float]:
        f = {"p_yes": self.p}
        if self.mid is not None:
            f["yes_mid"] = self.mid
        return f


def walk_forward(ds: Dataset, name: str, select: Selector, grid: Iterable[float], costs: SimCosts,
                 train_days: int = 3, val_days: int = 1, min_train_trades: int = 15,
                 require_calibration: bool = False, cal_min_markets: int = 30, cal_tol: float = 0.05) -> WFResult:
    by_day = ds.by_day()
    days = ds.days
    trades: list[Trade] = []
    chosen: list[tuple[str, float | None]] = []
    grid = list(grid)
    for i in range(train_days + val_days, len(days)):
        train_d, val_d, test_d = days[:i - val_days], days[i - val_days:i], days[i]
        cal = _calibration_before(by_day, days[:i], cal_min_markets, cal_tol)
        if require_calibration and not cal.calibrated:
            chosen.append((test_d, None))
            continue
        train = {k: v for d in train_d for k, v in by_day[d].items()}
        best, best_ev = None, None
        for g in grid:
            tr = simulate(train, select, g, costs, cal, name)
            if len(tr) >= min_train_trades:
                ev = sum(t.net for t in tr) / len(tr)
                if ev > 0 and (best_ev is None or ev > best_ev):
                    best, best_ev = g, ev
        if best is not None:
            val = simulate({k: v for d in val_d for k, v in by_day[d].items()}, select, best, costs, cal, name)
            if val and sum(t.net for t in val) <= 0:
                best = None  # profitable in training but not on the validation days: sit out
        chosen.append((test_d, best))
        if best is not None:
            trades += simulate(by_day[test_d], select, best, costs, cal, name)
    return WFResult(name, trades, chosen)


def risk_mode_sim(trades: Sequence[Trade], profile: RiskProfile, capital: float, costs: SimCosts) -> list[Trade]:
    """Re-size out-of-sample trades with the live position sizer and daily loss limit of ``profile``."""
    sizer = PositionSizer(costs.fee_rate)
    bal = capital
    daily: dict[str, float] = defaultdict(float)
    streak, paused_until = 0, None
    out = []
    for t in sorted(trades, key=lambda x: x.step.ts):
        if paused_until and t.step.ts < paused_until:
            continue
        if -daily[t.step.day] >= profile.max_daily_loss_usd or bal <= 0:
            continue
        price = min(99.0, t.ask + t.slippage)
        size = sizer.size(profile, price_cents=price, confidence=100, balance_usd=bal, open_exposure_usd=0.0,
                          liquidity_contracts=None, scale=1.0, p_win=None,
                          remaining_daily_loss_usd=profile.max_daily_loss_usd + min(0.0, daily[t.step.day]))
        if size.contracts < 1:
            continue
        fee = estimate_fee(size.contracts, price, costs.fee_rate) * 100 / size.contracts
        tt = replace(t, contracts=size.contracts, fee=fee)
        out.append(tt)
        bal += tt.net
        daily[t.step.day] += tt.net
        streak = 0 if tt.won else streak + 1
        if streak >= profile.max_consecutive_losses:
            paused_until, streak = t.step.ts + timedelta(seconds=profile.streak_pause_sec), 0
    return out


# ------------------------------------------------------------------ report
def run(ds: Dataset, costs: SimCosts | None = None, capital: float = 100.0, train_days: int = 3,
        val_days: int = 1, risk: RiskLevel = RiskLevel.MEDIUM,
        profiles: dict[RiskLevel, RiskProfile] | None = None) -> dict[str, Any]:
    costs = costs or SimCosts()
    profiles = profiles or load_profiles(read_env=False)
    prm = params_for(profiles[risk], costs)
    need_cal = profiles[risk].require_known_ev  # same calibration gate as the live engine for this profile
    wf = lambda name, sel, grid, **kw: walk_forward(ds, name, sel, grid, costs, train_days, val_days, **kw)  # noqa: E731
    variants = [
        wf("baseline", v_baseline, (0.0,)),
        wf("momentum", v_momentum, MOM_GRID),
        wf("momentum+trend", v_mom_trend, MOM_GRID),
        wf("momentum+volatility filter", v_mom_vol, MOM_GRID),
        wf("momentum+Kalshi price filter", v_mom_price, MOM_GRID),
        wf("momentum+probability model", v_mom_model(costs), MOM_GRID),
        wf("probability model + net edge", v_model_edge(prm), EDGE_GRID),
        wf("full strategy", v_full(prm), EDGE_GRID, require_calibration=need_cal),
        wf("full - late-entry filter", v_full(replace(prm, max_late_move_cents=1e9)), EDGE_GRID, require_calibration=need_cal),
        wf("full - regime filter", v_full(replace(prm, blocked_regimes=frozenset({"INSUFFICIENT_DATA"}))), EDGE_GRID, require_calibration=need_cal),
        wf("full - strength filter", v_full(replace(prm, min_strength=0)), EDGE_GRID, require_calibration=need_cal),
        wf("full - longshot guard", v_full(replace(prm, min_entry_cents=1.0)), EDGE_GRID, require_calibration=need_cal),
    ]
    res = {v.name: v for v in variants}
    days = ds.days
    test_days = days[train_days + val_days:]
    out: dict[str, Any] = {
        "generated": datetime.now().astimezone().isoformat(timespec="seconds"),
        "label": "BACKTEST (historical simulation, out-of-sample walk-forward)",
        "data": {
            "markets": len(ds.markets), "days": len(days), "first_day": days[0] if days else None,
            "last_day": days[-1] if days else None, "test_days": test_days,
            "minutes": sum(len(v) for v in ds.markets.values()), "skipped": ds.skipped,
            "rules_text_missing_markets": ds.rules_missing,
        },
        "assumptions": {
            "fee_rate": costs.fee_rate, "slippage_cents": costs.slippage_cents, "order_size_for_fees": costs.order_size,
            "entry": "ask at the 1-minute candle close + slippage; one trade per market",
            "spot": "Coinbase 1-minute closes (proxy for Kalshi's BRTI index)",
            "missing": "no Kalshi order-book history (liquidity not simulated); 1-minute quote resolution",
            "risk_profile_for_filters": risk.value, "train_days": train_days, "validation_days": val_days,
        },
        "variants": {v.name: {"oos": metrics(v.trades, capital), "chosen": v.chosen,
                              "breakdowns": breakdowns(v.trades)} for v in variants},
    }
    comparisons = [
        ("momentum vs baseline", "momentum", "baseline"),
        ("trend filter", "momentum+trend", "momentum"),
        ("volatility filter", "momentum+volatility filter", "momentum"),
        ("Kalshi price filter", "momentum+Kalshi price filter", "momentum"),
        ("probability model on momentum", "momentum+probability model", "momentum"),
        ("probability model + net edge vs momentum", "probability model + net edge", "momentum"),
        ("no-trade filters on the model", "full strategy", "probability model + net edge"),
        ("late-entry filter", "full strategy", "full - late-entry filter"),
        ("regime filter", "full strategy", "full - regime filter"),
        ("strength filter", "full strategy", "full - strength filter"),
        ("longshot guard", "full strategy", "full - longshot guard"),
    ]
    comp = []
    for label, a, b in comparisons:
        ta, tb = [t.net for t in res[a].trades], [t.net for t in res[b].trades]
        ev_a = sum(ta) / len(ta) if ta else None
        ev_b = sum(tb) / len(tb) if tb else None
        z = diff_z(ta, tb)
        verdict = "insufficient data"
        if len(ta) >= 30 and len(tb) >= 30 and ev_a is not None and ev_b is not None:
            verdict = ("improves out-of-sample EV" if ev_a > ev_b and (z or 0) > 1.0
                       else "hurts out-of-sample EV" if ev_a < ev_b and (z or 0) < -1.0
                       else "no clear difference - keep the simpler version")
        comp.append({"component": label, "with": a, "without": b, "ev_with": ev_a, "ev_without": ev_b,
                     "trades_with": len(ta), "trades_without": len(tb), "z": z, "verdict": verdict})
    out["component_tests"] = comp
    full = res["full strategy"].trades
    out["assumptions"]["calibration_gate"] = (
        "full-strategy variants trade only on days where markets settled EARLIER were calibrated"
        if need_cal else "not required for this risk profile (paper-style evidence collection)")
    out["full_by_risk_mode"] = {}
    for lvl, prof in profiles.items():
        lvl_wf = walk_forward(ds, f"full/{lvl.value}", v_full(params_for(prof, costs)), EDGE_GRID, costs,
                              train_days, val_days, require_calibration=prof.require_known_ev)
        sized = risk_mode_sim(lvl_wf.trades, prof, capital, costs)
        out["full_by_risk_mode"][lvl.value] = {"one_contract": metrics(lvl_wf.trades, capital),
                                               "sized_with_profile": metrics(sized, capital)}
    m = metrics(full, capital)
    if m["trades"] == 0:
        out["verdict"] = ("NO EVIDENCE OF EDGE: the full strategy never found a parameter that was profitable "
                          "on earlier days, so it did not trade out of sample.")
    elif m["trades"] < 30:
        out["verdict"] = f"INSUFFICIENT DATA: {m['trades']} out-of-sample trades (need 30+). Collect more days."
    elif m.get("ev_95") and m["ev_95"][0] > 0:
        out["verdict"] = "POSITIVE out of sample with the whole 95% EV range above zero. Next: paper-trade it live."
    elif (m["avg_profit_per_trade"] or 0) > 0:
        out["verdict"] = "POSITIVE ON AVERAGE but the 95% range includes losses - could be luck. Do not go live yet."
    else:
        out["verdict"] = "NO EDGE after costs out of sample. Do not trade this live."
    return out


def save_report(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, default=lambda o: None if o != o else str(o)))


def load_report(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def parse_day(s: str) -> datetime | None:
    return parse_ts(s)


def format_report(r: dict[str, Any], full: bool = True) -> str:
    """Human-readable text for the console (full) or Telegram (short)."""
    d = r["data"]
    lines = [r.get("label", "BACKTEST"), f"Data: {d['markets']} markets over {d['days']} days "
             f"({d['first_day']} .. {d['last_day']}); out-of-sample test days: {len(d['test_days'])}"]
    if d.get("rules_text_missing_markets"):
        lines.append(f"Note: {d['rules_text_missing_markets']} cached markets had no rules text; "
                     "a 60s BRTI average was assumed for them.")

    def row(name: str, m: dict[str, Any], sized: bool = False) -> str:
        if not m["trades"]:
            return f"{name:<34} 0 trades"
        pf = m["profit_factor"]
        pf_s = "inf" if pf == math.inf else ("n/a" if pf is None else f"{pf:.2f}")
        avg = (f"avg {m['avg_profit_per_trade']:+.2f}$/trade ({m['contracts']} contracts)" if sized
               else f"EV {m['avg_profit_per_trade'] * 100:+.1f}c/contract")
        return (f"{name:<34} {m['trades']:>4} trades  WR {m['win_rate']:.0%}  net {m['net_pnl']:+.2f}$  "
                f"{avg}  PF {pf_s}  maxDD {m['max_drawdown']:.2f}$")

    lines.append("")
    lines.append("OUT-OF-SAMPLE (1 contract per trade):")
    for name, v in r["variants"].items():
        if full or name in ("baseline", "momentum", "probability model + net edge", "full strategy"):
            lines.append(row(name, v["oos"]))
    if full:
        lines += ["", "COMPONENT TESTS (keep a filter only if it improves out-of-sample EV):"]
        for c in r["component_tests"]:
            lines.append(f"  {c['component']:<40} {c['verdict']}")
        lines += ["", "FULL STRATEGY BY RISK MODE (sized with each profile, starting capital per run):"]
        for lvl, v in r["full_by_risk_mode"].items():
            lines.append(row(f"  {lvl.upper()}", v["sized_with_profile"], sized=True))
        fs = r["variants"]["full strategy"]
        for name, groups in fs["breakdowns"].items():
            if groups:
                lines.append(f"  by {name}: " + "; ".join(
                    f"{k} {g['trades']}t {g['net_pnl']:+.2f}$" for k, g in groups.items()))
    lines += ["", "VERDICT: " + r["verdict"]]
    return "\n".join(lines)
