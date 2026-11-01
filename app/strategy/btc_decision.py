"""BTC decision engine: direction, signal strength, net edge and the profitability filter.

Direction (UP / DOWN) and trade action are separate: the engine first finds the
side whose *executable* price offers the best conservative expected value, then
runs every filter. Any critical failure -> ⚪ NO TRADE with named reasons, the
most important first.

Net expected value per contract (cents), for buying ``side`` at its ask A:

    cost      = A + slippage + fee(A)                 fee(A) = rate * A * (1 - A)   (A in dollars)
    EV_point  = 100 * P(side wins)        - cost
    EV_low    = 100 * P_low(side wins)    - cost      <- the gate uses this conservative value

A standard Kalshi binary pays $1 to the winning side and $0 to the losing side,
so the maximum loss per contract is the full cost.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from app.kalshi.contract import ContractTerms
from app.strategy.btc_model import BTCFeatures, BTCRegime, ProbabilityEstimate


class NoTrade(StrEnum):
    """Rejection reasons, in priority order (the first failing one is shown in Telegram)."""

    TRADING_PAUSED = "Trading paused"
    STALE_DATA = "Market data is stale"
    SETTLEMENT_UNVERIFIED = "Market settlement conditions not verified"
    INSUFFICIENT_TIME = "Insufficient time remaining"
    DAILY_LOSS = "Daily loss limit reached"
    POSITION_LIMIT = "Position limit reached"
    EXCESSIVE_SPREAD = "Excessive spread"
    INSUFFICIENT_LIQUIDITY = "Insufficient liquidity"
    UNFAVORABLE_REGIME = "Unfavorable volatility regime"
    EXCESSIVE_UNCERTAINTY = "Excessive model uncertainty"
    CONFLICT = "BTC and Kalshi movement conflict"
    PRICE_REFLECTS_MOVE = "Current price already reflects the move"
    WEAK_DIRECTION = "Weak directional evidence"
    LONGSHOT = "Long-shot contract (cheap contracts lose money on average)"
    INSUFFICIENT_EV = "Insufficient expected value"
    INSUFFICIENT_EVIDENCE = "Insufficient historical evidence"
    NOT_CALIBRATED = "Probability estimate poorly calibrated"


@dataclass(frozen=True)
class Rejection:
    code: NoTrade
    detail: str = ""
    critical: bool = True

    @property
    def text(self) -> str:
        return f"{self.code.value}: {self.detail}" if self.detail else self.code.value


@dataclass(frozen=True)
class CalibrationState:
    status: str  # CALIBRATED | NOT_CALIBRATED | INSUFFICIENT
    n_markets: int = 0
    ece: float | None = None
    brier_model: float | None = None
    brier_market: float | None = None

    @property
    def calibrated(self) -> bool:
        return self.status == "CALIBRATED"

    @property
    def label(self) -> str:
        if self.status == "CALIBRATED":
            return f"✅ CALIBRATED (n={self.n_markets})"
        if self.status == "NOT_CALIBRATED":
            return f"⚠️ POORLY CALIBRATED (n={self.n_markets})"
        return f"⚠️ MODEL NOT YET CALIBRATED (n={self.n_markets})"


UNCALIBRATED = CalibrationState("INSUFFICIENT")


@dataclass(frozen=True)
class BTCParams:
    """Thresholds for one decision (built from the active risk profile + settings)."""

    min_ev_cents: float
    min_strength: int
    max_prob_width: float
    max_spread_cents: float
    min_liquidity: float
    min_time_sec: float
    blocked_regimes: frozenset[str]
    max_late_move_cents: float
    require_calibration: bool
    fee_rate: float = 0.07
    slippage_cents: float = 1.0
    kalshi_stale_sec: float = 30.0
    btc_stale_sec: float = 15.0
    max_skew_sec: float = 20.0
    min_entry_cents: float = 5.0
    max_entry_cents: float = 95.0
    conflict_kalshi_cents: float = 5.0
    conflict_mom_z: float = 1.0


@dataclass(frozen=True)
class BTCInputs:
    now: datetime
    terms: ContractTerms
    features: BTCFeatures
    regime: BTCRegime
    estimate: ProbabilityEstimate
    yes_ask: float | None  # cents
    no_ask: float | None
    yes_bid: float | None
    no_bid: float | None
    t_left_sec: float
    kalshi_ts: datetime | None
    btc_ts: datetime | None
    liquidity: dict[str, float | None] = field(default_factory=dict)  # side -> contracts at best ask (None = unknown)
    kalshi_move_5m: float | None = None  # YES mid change over the last 5 minutes, cents
    calibration: CalibrationState = UNCALIBRATED
    paused_reason: str = ""
    live: bool = False

    @property
    def spread(self) -> float | None:
        if self.yes_ask is None or self.yes_bid is None:
            return None
        return self.yes_ask - self.yes_bid


@dataclass(frozen=True)
class SideEdge:
    side: str
    ask: float
    cost_cents: float  # ask + slippage + fee per contract
    p_point: float
    p_low: float
    ev_cents: float
    ev_low_cents: float


@dataclass(frozen=True)
class BTCSignal:
    direction: str  # UP | DOWN | WAIT (WAIT = ⚪ NO TRADE)
    leaning: str  # direction of the best-priced side before filters (UP/DOWN/WAIT)
    side: str | None  # yes/no to buy when approved; best candidate otherwise
    strength: int  # 0-100 signal strength - NOT a probability
    p_yes: float | None
    p_yes_low: float | None
    p_yes_high: float | None
    calibration: CalibrationState
    edge: SideEdge | None
    edges: tuple[SideEdge, ...]
    regime: BTCRegime
    features: BTCFeatures
    terms: ContractTerms
    supporting: tuple[str, ...]
    conflicting: tuple[str, ...]
    rejections: tuple[Rejection, ...]
    warnings: tuple[str, ...]
    data_age_sec: dict[str, float | None]
    t_left_sec: float

    @property
    def approved(self) -> bool:
        return not any(r.critical for r in self.rejections)

    @property
    def eligible(self) -> bool:
        return self.approved

    @property
    def primary_reason(self) -> str:
        crit = [r for r in self.rejections if r.critical]
        return crit[0].text if crit else ""

    @property
    def emoji(self) -> str:
        return {"UP": "🟢", "DOWN": "🔴"}.get(self.direction, "⚪")

    @property
    def label(self) -> str:
        return {"UP": "🟢 UP", "DOWN": "🔴 DOWN"}.get(self.direction, "⚪ NO TRADE")

    @property
    def p_side(self) -> float | None:
        return self.edge.p_point if self.edge else None


def fee_cents(price_cents: float, rate: float) -> float:
    p = price_cents / 100
    return rate * p * (1 - p) * 100


def side_edges(inp: BTCInputs, prm: BTCParams) -> list[SideEdge]:
    out = []
    for side, ask in (("yes", inp.yes_ask), ("no", inp.no_ask)):
        probs = inp.estimate.p_side(side)
        if ask is None or not 1 <= ask <= 99 or probs is None:
            continue
        cost = ask + prm.slippage_cents + fee_cents(ask, prm.fee_rate)
        p, lo, _ = probs
        out.append(SideEdge(side, ask, cost, p, lo, p * 100 - cost, lo * 100 - cost))
    return out


def _clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def strength_score(f: BTCFeatures, direction: str) -> tuple[int, list[str], list[str]]:
    """0-100 agreement of BTC evidence with ``direction`` (uncalibrated) + explanations."""
    if direction not in ("UP", "DOWN"):
        return 0, [], []
    d = 1.0 if direction == "UP" else -1.0
    sup: list[str] = []
    con: list[str] = []
    conviction = 0.0
    if f.dist_sigma is not None:
        conviction = _clamp(d * f.dist_sigma / 2.5, 0.0, 1.0)
        (sup if d * f.dist_sigma > 0.25 else con if d * f.dist_sigma < -0.25 else []).append(
            f"BTC {abs(f.dist_bps or 0):.0f} bps {'above' if (f.dist_bps or 0) > 0 else 'below'} the threshold "
            f"({f.dist_sigma:+.2f}σ to expiry)")
    mom = _clamp(d * (f.mom_z_5m or 0) / 2)
    if f.mom_z_5m is not None and abs(f.mom_z_5m) >= 0.5:
        (sup if mom > 0 else con).append(f"5-min momentum {f.ret_5m_bps:+.0f} bps ({f.mom_z_5m:+.1f}σ)")
    trend = _clamp(d * (f.trend_strength or 0) / 1.5)
    if f.trend_strength is not None and abs(f.trend_strength) >= 0.3:
        (sup if trend > 0 else con).append(f"EMA trend {'up' if f.trend_strength > 0 else 'down'} "
                                           f"({f.trend_strength:+.2f})")
    vol = 1.0 if (f.volume_supports_move and mom > 0) else 0.0
    if f.volume_spike:
        (sup if mom > 0 else con).append(f"volume spike ({f.rel_volume:.1f}x)")
    if f.momentum_state == "REVERSING":
        con.append("momentum reversing")
    elif f.momentum_state == "WEAKENING":
        con.append("momentum weakening")
    elif f.momentum_state in ("ACCELERATING_UP", "ACCELERATING_DOWN"):
        same = (f.momentum_state == "ACCELERATING_UP") == (d > 0)
        (sup if same else con).append(f"momentum {f.momentum_state.replace('_', ' ').lower()}")
    raw = (0.40 * conviction + 0.25 * max(0.0, mom) + 0.20 * max(0.0, trend) + 0.15 * vol
           - 0.25 * max(0.0, -mom) - 0.20 * max(0.0, -trend))
    return int(round(100 * _clamp(raw, 0.0, 1.0))), sup, con


def decide(inp: BTCInputs, prm: BTCParams) -> BTCSignal:
    rej: list[Rejection] = []
    warn: list[str] = []
    f, est, terms = inp.features, inp.estimate, inp.terms

    def no(code: NoTrade, detail: str = "", critical: bool = True) -> None:
        rej.append(Rejection(code, detail, critical))

    # ---- data health & synchronisation -----------------------------------
    k_age = (inp.now - inp.kalshi_ts).total_seconds() if inp.kalshi_ts else None
    b_age = (inp.now - inp.btc_ts).total_seconds() if inp.btc_ts else None
    if inp.paused_reason:
        no(NoTrade.TRADING_PAUSED, inp.paused_reason)
    if k_age is None or k_age > prm.kalshi_stale_sec:
        no(NoTrade.STALE_DATA, "Kalshi quotes " + ("missing" if k_age is None else f"{k_age:.0f}s old"))
    if b_age is None or b_age > prm.btc_stale_sec:
        no(NoTrade.STALE_DATA, "BTC feed " + ("unavailable" if b_age is None else f"{b_age:.0f}s old"))
    if inp.kalshi_ts and inp.btc_ts and abs((inp.kalshi_ts - inp.btc_ts).total_seconds()) > prm.max_skew_sec:
        no(NoTrade.STALE_DATA, f"BTC and Kalshi data {abs((inp.kalshi_ts - inp.btc_ts).total_seconds()):.0f}s apart")

    # ---- contract & time ----------------------------------------------------
    if not terms.verified:
        no(NoTrade.SETTLEMENT_UNVERIFIED, terms.primary_problem)
    min_t = max(prm.min_time_sec, terms.averaging_window_sec + 30)
    if inp.t_left_sec < min_t:
        no(NoTrade.INSUFFICIENT_TIME, f"{inp.t_left_sec:.0f}s left < {min_t:.0f}s")

    # ---- edge ---------------------------------------------------------------
    edges = side_edges(inp, prm) if est.ok else []
    if not est.ok:
        for p in est.problems:
            if "history" in p:
                no(NoTrade.INSUFFICIENT_EVIDENCE, p)
            elif "averaging" in p:
                no(NoTrade.INSUFFICIENT_TIME, p)
            elif "spot" in p:
                no(NoTrade.STALE_DATA, p)
    best = max(edges, key=lambda e: e.ev_low_cents) if edges else None
    if best is not None and best.ev_low_cents <= 0:
        # No side has value: report the side the MODEL favours (not the "least bad" price).
        best = max(edges, key=lambda e: e.p_point)
    leaning = terms.direction_for_side(best.side) if best and terms.kind else "WAIT"
    strength, sup, con = strength_score(f, leaning)

    # ---- execution quality ------------------------------------------------
    spread = inp.spread
    if spread is None:
        no(NoTrade.EXCESSIVE_SPREAD, "spread unavailable")
    elif spread > prm.max_spread_cents:
        no(NoTrade.EXCESSIVE_SPREAD, f"{spread:.0f}¢ > {prm.max_spread_cents:.0f}¢")
    if best is not None:
        liq = inp.liquidity.get(best.side)
        if liq is not None and liq < prm.min_liquidity:
            no(NoTrade.INSUFFICIENT_LIQUIDITY, f"{liq:.0f} < {prm.min_liquidity:.0f} contracts at the ask")
        if best.ask < prm.min_entry_cents:
            # Kalshi-wide data (Bürgi, Deng & Whelan 2025): cheap contracts lose heavily on average,
            # especially for takers, so the model's "edge" there is more likely model error.
            no(NoTrade.LONGSHOT, f"entry {best.ask:.0f}¢ < {prm.min_entry_cents:.0f}¢ minimum")
        elif best.ask > prm.max_entry_cents:
            no(NoTrade.INSUFFICIENT_EV, f"entry {best.ask:.0f}¢ > {prm.max_entry_cents:.0f}¢ maximum")

    # ---- regime & uncertainty ---------------------------------------------
    if inp.regime.value in prm.blocked_regimes:
        no(NoTrade.UNFAVORABLE_REGIME, inp.regime.badge)
    if est.width is not None and est.width > prm.max_prob_width:
        no(NoTrade.EXCESSIVE_UNCERTAINTY, f"probability range {est.width * 100:.0f} pts > "
                                          f"{prm.max_prob_width * 100:.0f}")

    # ---- direction quality, conflict and late entry --------------------------
    if best is not None:
        d = 1.0 if leaning == "UP" else -1.0
        km = inp.kalshi_move_5m
        if km is not None and f.mom_z_5m is not None:
            k_up = km if terms.kind == "above" else -km  # Kalshi move expressed as UP-direction cents
            if (abs(km) >= prm.conflict_kalshi_cents and abs(f.mom_z_5m) >= prm.conflict_mom_z
                    and k_up * f.mom_z_5m < 0):
                no(NoTrade.CONFLICT, f"BTC {f.ret_5m_bps:+.0f} bps vs Kalshi YES {km:+.0f}¢ over 5 min")
        side_move = (km if best.side == "yes" else -km) if km is not None else None
        if side_move is not None and side_move >= prm.max_late_move_cents and best.ev_low_cents < 2 * prm.min_ev_cents:
            no(NoTrade.PRICE_REFLECTS_MOVE, f"{best.side.upper()} already +{side_move:.0f}¢ in 5 min; "
                                            f"remaining edge {best.ev_low_cents:+.1f}¢")
        elif best.ask >= 75 and f.momentum_state in ("WEAKENING", "REVERSING") and d * (f.mom_z_5m or 0) > 0:
            no(NoTrade.PRICE_REFLECTS_MOVE, f"{best.ask:.0f}¢ entry while momentum is "
                                            f"{f.momentum_state.lower()}")
        if strength < prm.min_strength:
            no(NoTrade.WEAK_DIRECTION, f"strength {strength} < {prm.min_strength}")
        if best.ev_low_cents < prm.min_ev_cents:
            no(NoTrade.INSUFFICIENT_EV, f"net edge (conservative) {best.ev_low_cents:+.1f}¢ < "
                                        f"{prm.min_ev_cents:+.1f}¢")
    elif est.ok:
        no(NoTrade.INSUFFICIENT_EV, "no executable ask on either side")

    # ---- calibration evidence ---------------------------------------------
    cal = inp.calibration
    if not cal.calibrated:
        code = NoTrade.INSUFFICIENT_EVIDENCE if cal.status == "INSUFFICIENT" else NoTrade.NOT_CALIBRATED
        must = prm.require_calibration or inp.live
        detail = cal.label.replace("⚠️ ", "")
        if must:
            no(code, detail)
        else:
            no(code, detail + " - paper trading allowed to collect evidence", critical=False)
            warn.append(cal.label + " (paper only)")

    order = list(NoTrade)
    rej.sort(key=lambda r: (not r.critical, order.index(r.code)))
    approved = not any(r.critical for r in rej)
    return BTCSignal(
        direction=leaning if approved else "WAIT",
        leaning=leaning,
        side=best.side if best else None,
        strength=strength,
        p_yes=est.p_yes, p_yes_low=est.p_yes_low, p_yes_high=est.p_yes_high,
        calibration=cal,
        edge=best,
        edges=tuple(edges),
        regime=inp.regime,
        features=f,
        terms=terms,
        supporting=tuple(sup),
        conflicting=tuple(con),
        rejections=tuple(rej),
        warnings=tuple(warn),
        data_age_sec={"kalshi": k_age, "btc": b_age},
        t_left_sec=inp.t_left_sec,
    )
