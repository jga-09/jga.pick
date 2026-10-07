"""RiskManager: "is this trade acceptable under the selected risk profile?"."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from app.kalshi.market_data import MarketSnapshot
from app.risk.profiles import RiskLevel, RiskProfile
from app.risk.sizing import PositionSizer
from app.strategy.signals import SignalResult, Validity
from app.utils.logging import log_event
from app.utils.time import utcnow

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RiskContext:
    """Portfolio / bot state the risk checks need (built by the portfolio)."""

    trading_enabled: bool
    disabled_reason: str
    mode: str  # "paper" | "live"
    mode_permitted: bool
    balance_usd: float
    open_positions: int
    open_exposure_usd: float
    open_tickers: frozenset[str]
    inflight_tickers: frozenset[str]
    daily_realized_pnl: float
    last_loss_at: datetime | None = None
    last_trade_at: dict[str, datetime] = field(default_factory=dict)


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reason: str
    risk_level: RiskLevel
    position_size: int = 0
    side: str | None = None
    price_cents: float | None = None  # current ask
    limit_price_cents: float | None = None  # ask + tolerance (max price we accept)
    cost_usd: float = 0.0
    fee_usd: float = 0.0
    failures: tuple[str, ...] = ()
    timestamp: datetime = field(default_factory=utcnow)

    @property
    def total_usd(self) -> float:
        return round(self.cost_usd + self.fee_usd, 2)


class RiskManager:
    def __init__(self, sizer: PositionSizer, stale_after_sec: float = 30.0, price_buffer_cents: float = 2.0) -> None:
        self.sizer = sizer
        self.stale_after_sec = stale_after_sec
        self.price_buffer_cents = price_buffer_cents

    def evaluate(
        self,
        signal: SignalResult,
        snap: MarketSnapshot,
        profile: RiskProfile,
        ctx: RiskContext,
        now: datetime | None = None,
    ) -> RiskDecision:
        now = now or utcnow()
        fails: list[str] = []
        lvl = profile.level.title

        if not ctx.trading_enabled:
            fails.append(f"Trading disabled: {ctx.disabled_reason}")
        if not ctx.mode_permitted:
            fails.append(f"{ctx.mode.upper()} execution not permitted by configuration")

        side = signal.leaning.side if signal.direction.value == "WAIT" else signal.direction.side
        if signal.validity is not Validity.VALID:
            fails.append(f"Signal invalid: {signal.validity.value}")
        if side is None:
            fails.append("No directional signal")
        if signal.confidence < profile.min_confidence:
            fails.append(f"Confidence {signal.confidence} below {lvl} threshold ({profile.min_confidence})")

        if snap.info.status not in ("", "active", "open"):
            fails.append(f"Market not active ({snap.info.status})")
        remaining = snap.time_remaining(now)
        if remaining <= 0:
            fails.append("Market closed")
        elif remaining < profile.min_time_remaining_sec:
            fails.append(f"Too close to expiry ({int(remaining)}s < {profile.min_time_remaining_sec}s)")
        age = snap.age_sec(now)
        if age > self.stale_after_sec:
            fails.append(f"Stale market data ({int(age)}s old)")

        spread = snap.spread
        if spread is None:
            fails.append("Spread unavailable")
        elif spread > profile.max_spread_cents:
            fails.append(f"Spread {spread:.0f}¢ > max {profile.max_spread_cents:.0f}¢")

        price = snap.entry_price(side) if side else None
        if side and price is None:
            fails.append(f"No ask available for {side.upper()}")
        elif price is not None and not (profile.min_entry_price_cents <= price <= profile.max_entry_price_cents):
            fails.append(
                f"Entry {price:.0f}¢ outside {profile.min_entry_price_cents:.0f}-{profile.max_entry_price_cents:.0f}¢"
            )
        liquidity = snap.ask_liquidity(side) if side else 0.0
        if side and snap.orderbook is not None and liquidity < profile.min_liquidity_contracts:
            fails.append(f"Thin liquidity ({liquidity:.0f} < {profile.min_liquidity_contracts:.0f} contracts)")

        if -ctx.daily_realized_pnl >= profile.max_daily_loss_usd:
            fails.append(f"Daily loss limit reached (${profile.max_daily_loss_usd:.2f})")
        if ctx.open_positions >= profile.max_open_positions:
            fails.append(f"Max open positions reached ({profile.max_open_positions})")
        if ctx.open_exposure_usd >= profile.max_total_exposure_usd:
            fails.append(f"Max exposure reached (${profile.max_total_exposure_usd:.2f})")
        if snap.ticker in ctx.open_tickers:
            fails.append("Already holding a position in this market")
        if snap.ticker in ctx.inflight_tickers:
            fails.append("Order already in flight for this market")
        if ctx.last_loss_at and (now - ctx.last_loss_at).total_seconds() < profile.loss_cooldown_sec:
            left = profile.loss_cooldown_sec - (now - ctx.last_loss_at).total_seconds()
            fails.append(f"Loss cooldown ({int(left)}s left)")
        last = ctx.last_trade_at.get(snap.ticker)
        if last and (now - last).total_seconds() < profile.trade_cooldown_sec:
            fails.append("Recent trade cooldown on this market")

        size = None
        if price is not None:
            limit = min(99.0, price + self.price_buffer_cents)
            size = self.sizer.size(
                profile,
                price_cents=limit,  # size at the worst-case limit price
                confidence=signal.confidence,
                balance_usd=ctx.balance_usd,
                open_exposure_usd=ctx.open_exposure_usd,
                liquidity_contracts=liquidity if snap.orderbook is not None else None,
            )
            if size.contracts < 1:
                fails.append(f"Position size is zero (limited by {size.limiting_factor})")

        approved = not fails
        decision = RiskDecision(
            approved=approved,
            reason="All risk checks passed" if approved else fails[0],
            risk_level=profile.level,
            position_size=size.contracts if size and approved else 0,
            side=side,
            price_cents=price,
            limit_price_cents=min(99.0, price + self.price_buffer_cents) if price is not None else None,
            cost_usd=size.cost_usd if size and approved else 0.0,
            fee_usd=size.fee_usd if size and approved else 0.0,
            failures=tuple(fails),
            timestamp=now,
        )
        log_event(log, "RISK_EVAL", logging.DEBUG, ticker=snap.ticker, profile=lvl, approved=approved,
                  reason=decision.reason)
        return decision
