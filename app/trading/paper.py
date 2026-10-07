"""Paper (simulated) execution. Never talks to the Kalshi order API."""

from __future__ import annotations

import logging

from app.errors import OrderRejectedError
from app.kalshi.market_data import MarketSnapshot
from app.trading.fees import estimate_fee
from app.trading.portfolio import Portfolio
from app.trading.positions import Position
from app.trading.tickets import TradeTicket
from app.utils.logging import log_event

log = logging.getLogger(__name__)


class PaperExecutor:
    mode = "paper"

    def __init__(self, portfolio: Portfolio, slippage_cents: int = 1, fee_rate: float = 0.07) -> None:
        self.portfolio = portfolio
        self.slippage = slippage_cents
        self.fee_rate = fee_rate

    async def buy(self, ticket: TradeTicket, snap: MarketSnapshot) -> Position:
        ask = snap.entry_price(ticket.side)
        if ask is None:
            raise OrderRejectedError("no ask available")
        fill = min(99.0, ask + self.slippage)
        if fill > ticket.limit_price_cents:
            raise OrderRejectedError(f"price moved: fill {fill:.0f}¢ > limit {ticket.limit_price_cents:.0f}¢")
        fee = estimate_fee(ticket.contracts, fill, self.fee_rate)
        cost = ticket.contracts * fill / 100
        if cost + fee > self.portfolio.paper_balance():
            raise OrderRejectedError("insufficient paper balance")
        pos = Position(
            mode="paper", ticker=snap.ticker, event_ticker=snap.info.event_ticker, asset=snap.info.asset,
            label=snap.info.label, side=ticket.side, contracts=ticket.contracts, entry_price=fill,
            entry_fee=fee, risk_level=ticket.risk_level, confidence=ticket.confidence,
            close_time=snap.info.close_time, client_order_id=ticket.client_order_id,
        )
        self.portfolio.add_open(pos)
        self.portfolio.repo.add_paper_order(
            client_order_id=ticket.client_order_id, ticker=snap.ticker, side=ticket.side, action="buy",
            contracts=ticket.contracts, price=fill, fee=fee, status="filled", position_id=pos.id,
        )
        log_event(log, "PAPER_ORDER", action=f"BUY_{ticket.side.upper()}", price=fill / 100,
                  qty=ticket.contracts, ticker=snap.ticker)
        return pos

    async def sell(self, pos: Position, snap: MarketSnapshot, reason: str = "manual exit") -> float:
        bid = snap.exit_price(pos.side)
        if bid is None:
            raise OrderRejectedError("no bid available to exit")
        fill = max(1.0, bid - self.slippage)
        fee = estimate_fee(pos.contracts, fill, self.fee_rate)
        self.portfolio.repo.add_paper_order(
            client_order_id=f"{pos.client_order_id}-x", ticker=pos.ticker, side=pos.side, action="sell",
            contracts=pos.contracts, price=fill, fee=fee, status="filled", position_id=pos.id,
        )
        return self.portfolio.close(pos, fill, fee, reason)

    def settle(self, pos: Position, result: str) -> float:
        """Settle at expiry: winning side pays 100¢, losing side 0¢ (no fee)."""
        exit_price = 100.0 if result == pos.side else 0.0
        return self.portfolio.close(pos, exit_price, 0.0, f"settled {result.upper()}", action="SETTLE")
