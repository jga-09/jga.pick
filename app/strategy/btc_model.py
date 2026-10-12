"""BTC features, market regime and the contract probability model.

Everything here is a pure function of data available at decision time ``as_of``:
only 1-minute bars that have *ended* by ``as_of`` are used, so the same code runs
live, in replays and in backtests without look-ahead.

Probability model (baseline, interpretable, no fitted parameters):

    P(BTC_settle > K) = Phi( ln(S / K) / (sigma * sqrt(T_eff)) )

* S = current BTC spot, K = contract threshold, sigma = per-sqrt-second realised
  volatility of the previous ``sigma_window`` minutes of 1-minute log returns.
* T_eff accounts for an averaged settlement: if the contract settles on the mean
  of the last W seconds, Var(ln average) ~= sigma^2 * (T - W + W/3) for T >= W.
* Uncertainty: sigma has sampling error ~ sigma / sqrt(2n); the Coinbase price is a
  proxy for Kalshi's settlement index (``basis_bps``). The probability interval is
  the range over those perturbations. Trades are judged on the conservative end.

Because nothing is fitted, every prediction is out-of-sample by construction; its
calibration still has to be measured on settled markets before it is trusted.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
from statistics import NormalDist

from app.data.btc_feed import MinuteBar
from app.kalshi.contract import ContractTerms

_ND = NormalDist()


class BTCRegime(StrEnum):
    STRONG_UPTREND = "STRONG_UPTREND"
    STRONG_DOWNTREND = "STRONG_DOWNTREND"
    SIDEWAYS = "SIDEWAYS"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    RAPID_REVERSAL = "RAPID_REVERSAL"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"

    @property
    def badge(self) -> str:
        return {
            "STRONG_UPTREND": "🟢 STRONG UPTREND", "STRONG_DOWNTREND": "🔴 STRONG DOWNTREND",
            "SIDEWAYS": "🟡 SIDEWAYS", "HIGH_VOLATILITY": "🟠 HIGH VOLATILITY",
            "RAPID_REVERSAL": "⚠️ RAPID REVERSAL", "INSUFFICIENT_DATA": "⚪ INSUFFICIENT DATA",
        }[self.value]

    @property
    def code(self) -> int:
        return list(BTCRegime).index(self)


@dataclass(frozen=True)
class ModelConfig:
    sigma_window_min: int = 60  # minutes of 1-minute returns used for volatility
    min_bars: int = 20  # below this: INSUFFICIENT_DATA, no probability
    basis_bps: float = 3.0  # Coinbase vs BRTI uncertainty (basis points)
    sigma_z: float = 1.28  # ~80% interval on the volatility estimate
    # Regime cut-offs (a-priori round numbers; evaluate per regime in the backtest before changing)
    trend_z: float = 2.0  # |15-min move| in sigmas for a strong trend
    trend_consistency: float = 0.6
    high_vol_ratio: float = 1.8  # recent 15-min vol / 60-min vol
    shock_z: float = 4.0  # a single 1-minute move this many sigmas = shock
    reversal_z: float = 1.5


# ------------------------------------------------------------------ helpers
def bars_until(bars: Sequence[MinuteBar], as_of: datetime) -> list[MinuteBar]:
    """Only bars that are complete at ``as_of`` (look-ahead guard)."""
    return [b for b in bars if b.ts <= as_of]


def _log_returns(bars: Sequence[MinuteBar]) -> list[float]:
    return [math.log(b.close / a.close) for a, b in zip(bars, bars[1:], strict=False) if a.close > 0]


def _ema(values: Sequence[float], n: int) -> float | None:
    if len(values) < n:
        return None
    k = 2 / (n + 1)
    e = sum(values[:n]) / n
    for v in values[n:]:
        e = v * k + e * (1 - k)
    return e


def _sd(xs: Sequence[float]) -> float | None:
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def realized_sigma_per_sec(bars: Sequence[MinuteBar], window_min: int) -> tuple[float | None, int]:
    """(per-sqrt-second volatility, number of returns) from the last ``window_min`` 1-minute returns."""
    rets = _log_returns(list(bars)[-(window_min + 1):])
    if len(rets) < 10:
        return None, len(rets)
    # Root-mean-square (no mean removal): over minutes the drift is negligible vs noise.
    rms = math.sqrt(sum(r * r for r in rets) / len(rets))
    return rms / math.sqrt(60.0), len(rets)


def effective_horizon(t_left_sec: float, avg_window_sec: int) -> float | None:
    """Seconds of variance left before settlement; None inside the averaging window (not modelled)."""
    if avg_window_sec <= 0:
        return max(t_left_sec, 1.0)
    if t_left_sec < avg_window_sec:
        return None
    return (t_left_sec - avg_window_sec) + avg_window_sec / 3.0


def p_above(spot: float, threshold: float, sigma: float, horizon_sec: float) -> float:
    if spot <= 0 or threshold <= 0:
        return 0.5
    s = max(sigma, 1e-9) * math.sqrt(max(horizon_sec, 1.0))
    return min(0.999, max(0.001, _ND.cdf(math.log(spot / threshold) / s)))


# ------------------------------------------------------------------ features
@dataclass
class BTCFeatures:
    as_of: datetime
    bars: int
    spot: float | None
    # A. momentum (log returns in basis points, z = move / expected noise)
    ret_1m_bps: float | None = None
    ret_3m_bps: float | None = None
    ret_5m_bps: float | None = None
    ret_15m_bps: float | None = None
    mom_z_5m: float | None = None
    mom_z_15m: float | None = None
    consistency_10m: float | None = None  # share of last 10 1m returns with the sign of the 10m move
    # B. trend
    ema_fast: float | None = None
    ema_slow: float | None = None
    ema_slope_bps: float | None = None  # fast-EMA change over 3 bars, bps per minute
    trend_strength: float | None = None  # (fast - slow) / (sigma_1m * price)
    trend_reversal: bool = False  # fast EMA crossed slow within the last 3 bars
    # C. acceleration
    accel_z: float | None = None  # last-2m velocity vs the previous 3m, in noise units
    momentum_state: str = "n/a"  # ACCELERATING_UP/DOWN, WEAKENING, REVERSING, STEADY
    # D. volatility
    sigma_1m: float | None = None  # per-minute log-return sd (last sigma_window minutes)
    sigma_15m_recent: float | None = None
    vol_ratio: float | None = None
    shock: bool = False
    # E. volume
    rel_volume: float | None = None  # last 5m avg volume / last 60m avg
    volume_spike: bool = False
    volume_supports_move: bool | None = None
    # F. structure
    high_60m: float | None = None
    low_60m: float | None = None
    breakout: int = 0  # +1 above prior 30m high, -1 below prior 30m low
    failed_breakout: int = 0  # poked through and closed back inside within 5m
    # G/H. threshold & time (filled with contract context)
    threshold: float | None = None
    dist_bps: float | None = None
    dist_sigma: float | None = None
    t_left_sec: float | None = None

    def as_dict(self) -> dict[str, float | int | str | bool | None]:
        d = asdict(self)
        d["as_of"] = self.as_of.isoformat()
        return d


def compute_btc_features(bars_all: Sequence[MinuteBar], spot: float | None, as_of: datetime,
                         cfg: ModelConfig | None = None, threshold: float | None = None,
                         t_left_sec: float | None = None, avg_window_sec: int = 0) -> BTCFeatures:
    cfg = cfg or ModelConfig()
    bars = bars_until(bars_all, as_of)
    f = BTCFeatures(as_of=as_of, bars=len(bars), spot=spot, threshold=threshold, t_left_sec=t_left_sec)
    if len(bars) < 2:
        return f
    closes = [b.close for b in bars]
    price = spot or closes[-1]
    rets = _log_returns(bars)

    def ret(n: int) -> float | None:
        return math.log(price / closes[-1 - n]) * 1e4 if len(closes) > n else None

    f.ret_1m_bps, f.ret_3m_bps, f.ret_5m_bps, f.ret_15m_bps = ret(1), ret(3), ret(5), ret(15)
    win = rets[-cfg.sigma_window_min:]
    f.sigma_1m = math.sqrt(sum(r * r for r in win) / len(win)) if len(win) >= 10 else None
    s1 = f.sigma_1m
    if s1:
        if f.ret_5m_bps is not None:
            f.mom_z_5m = f.ret_5m_bps / 1e4 / (s1 * math.sqrt(5))
        if f.ret_15m_bps is not None:
            f.mom_z_15m = f.ret_15m_bps / 1e4 / (s1 * math.sqrt(15))
    if len(rets) >= 10:
        last10 = rets[-10:]
        tot = sum(last10)
        f.consistency_10m = sum(1 for r in last10 if r * tot > 0) / 10 if tot else 0.0

    f.ema_fast, f.ema_slow = _ema(closes, 5), _ema(closes, 20)
    if len(closes) >= 9:
        prev_fast = _ema(closes[:-3], 5)
        if prev_fast and f.ema_fast:
            f.ema_slope_bps = math.log(f.ema_fast / prev_fast) * 1e4 / 3
    if f.ema_fast and f.ema_slow and s1:
        f.trend_strength = (f.ema_fast - f.ema_slow) / (s1 * price)
        if len(closes) >= 24:
            pf, ps = _ema(closes[:-3], 5), _ema(closes[:-3], 20)
            if pf and ps:
                f.trend_reversal = (pf - ps) * (f.ema_fast - f.ema_slow) < 0

    if s1 and len(rets) >= 5:
        v_recent, v_prior = sum(rets[-2:]) / 2, sum(rets[-5:-2]) / 3
        noise = s1 * math.sqrt(1 / 2 + 1 / 3)
        f.accel_z = (v_recent - v_prior) / noise
        if abs(v_recent) < 0.2 * s1 and abs(v_prior) < 0.2 * s1:
            f.momentum_state = "STEADY"
        elif v_recent * v_prior < 0 and abs(v_recent) > 0.5 * s1:
            f.momentum_state = "REVERSING"
        elif f.accel_z > 0.8 and v_recent > 0:
            f.momentum_state = "ACCELERATING_UP"
        elif f.accel_z < -0.8 and v_recent < 0:
            f.momentum_state = "ACCELERATING_DOWN"
        elif abs(v_recent) < abs(v_prior) * 0.6:
            f.momentum_state = "WEAKENING"
        else:
            f.momentum_state = "STEADY"

    recent15 = rets[-15:]
    f.sigma_15m_recent = _sd(recent15) if len(recent15) >= 8 else None
    if f.sigma_15m_recent and s1:
        f.vol_ratio = f.sigma_15m_recent / s1
    if s1 and rets[-3:]:
        f.shock = max(abs(r) for r in rets[-3:]) >= cfg.shock_z * s1

    vols = [b.volume for b in bars]
    if len(vols) >= 30:
        base = sum(vols[-60:]) / len(vols[-60:])
        last5 = sum(vols[-5:]) / 5
        f.rel_volume = last5 / base if base > 0 else None
        f.volume_spike = bool(f.rel_volume and f.rel_volume >= 2.5)
        if f.ret_5m_bps is not None and f.rel_volume is not None:
            f.volume_supports_move = abs(f.ret_5m_bps) > 0 and f.rel_volume >= 1.0

    last60 = bars[-60:]
    f.high_60m, f.low_60m = max(b.high for b in last60), min(b.low for b in last60)
    if len(bars) >= 36:
        prior = bars[-36:-5]
        hi, lo = max(b.high for b in prior), min(b.low for b in prior)
        if price > hi:
            f.breakout = 1
        elif price < lo:
            f.breakout = -1
        recent = bars[-5:]
        if f.breakout == 0 and max(b.high for b in recent) > hi:
            f.failed_breakout = 1
        elif f.breakout == 0 and min(b.low for b in recent) < lo:
            f.failed_breakout = -1

    if threshold and price:
        f.dist_bps = math.log(price / threshold) * 1e4
        h = effective_horizon(t_left_sec, avg_window_sec) if t_left_sec is not None else None
        if s1 and h:
            f.dist_sigma = math.log(price / threshold) / ((s1 / math.sqrt(60)) * math.sqrt(h))
    return f


def classify_regime(f: BTCFeatures, cfg: ModelConfig | None = None) -> BTCRegime:
    cfg = cfg or ModelConfig()
    if f.bars < cfg.min_bars or f.sigma_1m is None:
        return BTCRegime.INSUFFICIENT_DATA
    if f.shock or (f.vol_ratio is not None and f.vol_ratio >= cfg.high_vol_ratio):
        return BTCRegime.HIGH_VOLATILITY
    z5, z15 = f.mom_z_5m, f.mom_z_15m
    if (z5 is not None and z15 is not None and z5 * z15 < 0
            and abs(z5) >= cfg.reversal_z and abs(z15) >= cfg.reversal_z):
        return BTCRegime.RAPID_REVERSAL
    if z15 is not None and abs(z15) >= cfg.trend_z and (f.consistency_10m or 0) >= cfg.trend_consistency:
        aligned = f.trend_strength is None or f.trend_strength * z15 > 0
        if aligned:
            return BTCRegime.STRONG_UPTREND if z15 > 0 else BTCRegime.STRONG_DOWNTREND
    return BTCRegime.SIDEWAYS


# ------------------------------------------------------------------ probability
@dataclass(frozen=True)
class ProbabilityEstimate:
    p_yes: float | None
    p_yes_low: float | None
    p_yes_high: float | None
    sigma_per_sec: float | None
    n_returns: int
    horizon_sec: float | None
    problems: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return self.p_yes is not None and not self.problems

    @property
    def width(self) -> float | None:
        if self.p_yes_low is None or self.p_yes_high is None:
            return None
        return self.p_yes_high - self.p_yes_low

    def p_side(self, side: str) -> tuple[float, float, float] | None:
        """(point, conservative low, high) probability that buying ``side`` wins."""
        if self.p_yes is None or self.p_yes_low is None or self.p_yes_high is None:
            return None
        if side == "yes":
            return self.p_yes, self.p_yes_low, self.p_yes_high
        return 1 - self.p_yes, 1 - self.p_yes_high, 1 - self.p_yes_low


def estimate_probability(terms: ContractTerms, spot: float | None, bars: Sequence[MinuteBar], as_of: datetime,
                         t_left_sec: float, cfg: ModelConfig | None = None) -> ProbabilityEstimate:
    cfg = cfg or ModelConfig()
    problems: list[str] = []
    if not terms.verified or terms.threshold is None or terms.kind is None:
        problems.append("contract terms not verified")
    if spot is None or spot <= 0:
        problems.append("no BTC spot price")
    usable = bars_until(bars, as_of)
    sigma, n = realized_sigma_per_sec(usable, cfg.sigma_window_min)
    if sigma is None or len(usable) < cfg.min_bars:
        problems.append(f"not enough BTC history for volatility ({len(usable)} bars)")
    horizon = effective_horizon(t_left_sec, terms.averaging_window_sec)
    if horizon is None:
        problems.append("inside the settlement averaging window - outcome partly fixed, not modelled")
    if problems:
        return ProbabilityEstimate(None, None, None, sigma, n, horizon, tuple(problems))
    assert spot is not None and sigma is not None and horizon is not None and terms.threshold is not None

    k = terms.threshold
    rel = cfg.sigma_z / math.sqrt(2 * max(n, 1))
    sig_lo, sig_hi = sigma * max(0.2, 1 - rel), sigma * (1 + rel)
    basis = cfg.basis_bps / 1e4
    grid = [p_above(spot * (1 + b), k, s, horizon) for s in (sig_lo, sigma, sig_hi) for b in (-basis, 0.0, basis)]
    p = p_above(spot, k, sigma, horizon)
    lo, hi = min(grid), max(grid)
    if terms.kind == "below":
        p, lo, hi = 1 - p, 1 - hi, 1 - lo
    return ProbabilityEstimate(p, lo, hi, sigma, n, horizon)
