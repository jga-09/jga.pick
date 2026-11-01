"""One decision record per evaluated market: every number behind TRADE / NO TRADE, in one place.

Keeps four things apart that must never be confused:
  * signal strength / confidence - a 0-100 score, NOT a probability
  * model probability            - the model's P(win) for the side (conservative bound where available)
  * market-implied probability   - Kalshi's mid price
  * expected value               - model probability x $1 - ask - Kalshi fee (rounded up per order) - slippage
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.kalshi.market_data import MarketSnapshot
from app.trading.fees import estimate_fee
from app.utils.time import utcnow


@dataclass(frozen=True)
class DecisionRecord:
    asset: str
    ticker: str
    engine: str  # btc | quality | confirm
    direction: str  # UP | DOWN | WAIT
    side: str | None
    strength: int | None  # 0-100, not a probability
    model_p: float | None  # P(side wins), point estimate
    model_p_low: float | None  # conservative bound (used for the EV gate) when the model gives one
    market_p: float | None  # Kalshi mid for the side
    ask: float | None  # cents, executable
    fee_c: float | None  # cents per contract, 1-contract order (worst case)
    slippage_c: float
    ev_c: float | None  # cents per contract after fee + slippage (conservative)
    spread: float | None
    liquidity: float | None  # contracts at the ask (None = no order book)
    t_left_sec: float
    data_age_sec: dict[str, float | None]
    reliability: str  # e.g. "CALIBRATED n=412" / "INSUFFICIENT n=12" / "asset:BTC n=80"
    sample_size: int
    approved: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def verdict(self) -> str:
        return "TRADE" if self.approved else "NO TRADE"

    @property
    def edge_pts(self) -> float | None:
        """Model probability minus market probability, in percentage points."""
        if self.model_p is None or self.market_p is None:
            return None
        return (self.model_p - self.market_p) * 100


def _side_mid(snap: MarketSnapshot, side: str | None) -> float | None:
    mid = snap.yes_mid
    if mid is None or side is None:
        return None
    return (mid if side == "yes" else 100 - mid) / 100


def from_btc(sig: Any, snap: MarketSnapshot, slippage: float, fee_rate: float) -> DecisionRecord:
    b = sig.btc
    e = b.edge
    ask = e.ask if e else None
    cal = b.calibration
    return DecisionRecord(
        asset=snap.info.asset, ticker=snap.ticker, engine="btc", direction=b.direction, side=b.side,
        strength=b.strength, model_p=e.p_point if e else None, model_p_low=e.p_low if e else None,
        market_p=_side_mid(snap, b.side), ask=ask,
        fee_c=estimate_fee(1, ask, fee_rate) * 100 if ask is not None else None, slippage_c=slippage,
        ev_c=e.ev_low_cents if e else None, spread=snap.spread,
        liquidity=snap.ask_liquidity(b.side) if (snap.orderbook and b.side) else None,
        t_left_sec=b.t_left_sec, data_age_sec=dict(b.data_age_sec),
        reliability=f"{cal.status} n={cal.n_markets}", sample_size=cal.n_markets, approved=b.approved,
        reasons=tuple(f"{r.code.value}{': ' + r.detail if r.detail else ''}" for r in b.rejections if r.critical),
    )


def from_legacy(sig: Any, snap: MarketSnapshot, decision: Any, slippage: float, fee_rate: float,
                engine: str) -> DecisionRecord:
    side = getattr(sig.leaning, "side", None)
    ask = snap.entry_price(side) if side else None
    est = getattr(decision, "estimate", None)
    p = getattr(est, "p_model", None)
    ev = getattr(est, "ev_low_cents", None)
    scope = getattr(est, "scope", "") or "none"
    n = getattr(est, "n_markets", 0) or 0
    age = (utcnow() - snap.ts).total_seconds() if snap.ts else None
    return DecisionRecord(
        asset=snap.info.asset, ticker=snap.ticker, engine=engine, direction=getattr(sig.leaning, "value", "WAIT"),
        side=side, strength=sig.confidence, model_p=p, model_p_low=None, market_p=_side_mid(snap, side), ask=ask,
        fee_c=estimate_fee(1, ask, fee_rate) * 100 if ask is not None else None, slippage_c=slippage, ev_c=ev,
        spread=snap.spread, liquidity=snap.ask_liquidity(side) if (snap.orderbook and side) else None,
        t_left_sec=snap.time_remaining(), data_age_sec={"kalshi": age},
        reliability=f"{scope} n={n}" + ("" if getattr(est, "sufficient", False) else " (insufficient)"),
        sample_size=n, approved=bool(getattr(decision, "approved", False)),
        reasons=tuple(getattr(decision, "failures", ()) or ()),
    )
