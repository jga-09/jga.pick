"""TradeExecutor: the single entry point for opening/closing positions.

Enforces (in order): global trading state / emergency stop, duplicate-order
protection, a *fresh* RiskManager approval, then routes to paper or live.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace

from app.errors import DuplicateOrderError, LiveTradingDisabledError, OrderRejectedError, TradingDisabledError
from app.kalshi.market_data import MarketSnapshot
from app.risk.manager import RiskDecision
from app.state import StateStore
from app.trading.live import LiveExecutor
from app.trading.paper import PaperExecutor
from app.trading.portfolio import Portfolio
from app.trading.positions import Position
from app.trading.tickets import TradeTicket
from app.utils.logging import log_event

log = logging.getLogger(__name__)

Revalidator = Callable[[TradeTicket], Awaitable[tuple[RiskDecision, MarketSnapshot]]]


class TradeExecutor:
    def __init__(
        self,
        state: StateStore,
        portfolio: Portfolio,
        paper: PaperExecutor,
        live: LiveExecutor | None,
    ) -> None:
        self.state = state
        self.portfolio = portfolio
        self.paper = paper
        self.live = live
        self._locks: dict[str, asyncio.Lock] = {}
        self._executed: set[str] = set()

    def _check_enabled(self) -> None:
        st = self.state.state
        if st.emergency_stop:
            raise TradingDisabledError("EMERGENCY STOP active - orders blocked")
        if not st.trading_allowed:
            raise TradingDisabledError(f"trading disabled: {st.disabled_reason}")

    async def execute(self, ticket: TradeTicket, *, confirmed: bool, revalidate: Revalidator) -> Position:
        self._check_enabled()
        if ticket.client_order_id in self._executed or self.portfolio.repo.client_order_exists(ticket.client_order_id):
            raise DuplicateOrderError("this ticket was already executed")
        lock = self._locks.setdefault(ticket.ticker, asyncio.Lock())
        if lock.locked():
            raise DuplicateOrderError("an order for this market is already in flight")
        async with lock:
            decision, snap = await revalidate(ticket)
            if not decision.approved:
                raise OrderRejectedError(decision.reason)
            if decision.side != ticket.side:
                raise OrderRejectedError("signal direction changed since the ticket was created")
            mode = self.state.state.mode
            if mode != ticket.mode:
                raise OrderRejectedError("execution mode changed since the ticket was created")
            contracts = min(ticket.contracts, decision.position_size)
            if contracts < ticket.contracts:
                ticket = replace(ticket, contracts=contracts)
            self._check_enabled()  # last check immediately before submission
            self._executed.add(ticket.client_order_id)
            self.portfolio.inflight.add(ticket.ticker)
            try:
                if mode == "live":
                    if self.live is None:
                        raise LiveTradingDisabledError("live executor not configured")
                    pos = await self.live.buy(ticket, snap, confirmed=confirmed)
                else:
                    pos = await self.paper.buy(ticket, snap)
            finally:
                self.portfolio.inflight.discard(ticket.ticker)
        log_event(log, "TRADE_OPENED", mode=mode, ticker=pos.ticker, side=pos.side, qty=pos.contracts,
                  price=pos.entry_price, origin=ticket.origin)
        return pos

    async def close(self, pos: Position, snap: MarketSnapshot, *, confirmed: bool, reason: str = "manual exit") -> float:
        if not pos.is_open:
            raise DuplicateOrderError("position already closed")
        lock = self._locks.setdefault(pos.ticker, asyncio.Lock())
        if lock.locked():
            raise DuplicateOrderError("an order for this market is already in flight")
        async with lock:
            if pos.mode == "live":
                if self.state.state.emergency_stop:
                    raise TradingDisabledError("EMERGENCY STOP active - orders blocked")
                if self.live is None:
                    raise LiveTradingDisabledError("live executor not configured")
                return await self.live.sell(pos, snap, reason, confirmed=confirmed)
            return await self.paper.sell(pos, snap, reason)
