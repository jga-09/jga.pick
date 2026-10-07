"""Live execution against Kalshi - behind strict safety gates.

A live order is only ever submitted when ALL of the following hold:
  * LIVE_TRADING=true AND PAPER_TRADING=false (Settings.live_allowed)
  * Kalshi credentials are loaded (signed client)
  * the request comes from an authorised admin (enforced in the Telegram layer)
  * the user explicitly confirmed this ticket (or, for auto-trades,
    ALLOW_LIVE_AUTOTRADE=true as well)
  * the RiskManager approved it (enforced by TradeExecutor right before submit)
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import Settings
from app.errors import KalshiError, LiveTradingDisabledError, OrderRejectedError
from app.kalshi.execution import build_entry_order, build_exit_order
from app.kalshi.market_data import MarketSnapshot, dollars_to_cents, to_float
from app.trading.portfolio import Portfolio
from app.trading.positions import Position
from app.trading.tickets import TradeTicket
from app.utils.logging import log_event

log = logging.getLogger(__name__)


class LiveExecutor:
    mode = "live"

    def __init__(self, client: Any, settings: Settings, portfolio: Portfolio) -> None:
        self.client = client
        self.settings = settings
        self.portfolio = portfolio

    def assert_enabled(self) -> None:
        if not self.settings.live_allowed:
            raise LiveTradingDisabledError("live trading disabled (requires LIVE_TRADING=true and PAPER_TRADING=false)")
        if getattr(self.client, "signer", None) is None:
            raise LiveTradingDisabledError("live trading requires Kalshi API credentials")

    def _assert_authorised(self, ticket: TradeTicket, confirmed: bool) -> None:
        self.assert_enabled()
        if confirmed:
            return
        if ticket.origin == "auto" and self.settings.live_autotrade_allowed:
            return
        raise LiveTradingDisabledError("live orders require explicit confirmation")

    async def buy(self, ticket: TradeTicket, snap: MarketSnapshot, *, confirmed: bool) -> Position:
        self._assert_authorised(ticket, confirmed)
        body = build_entry_order(
            ticker=ticket.ticker, outcome_side=ticket.side, contracts=ticket.contracts,
            limit_price_cents=ticket.limit_price_cents, client_order_id=ticket.client_order_id,
        )
        resp = await self._submit(ticket.client_order_id, ticket.ticker, ticket.side, "buy",
                                  ticket.contracts, ticket.limit_price_cents, body)
        filled, price, fee = self._fill(resp, ticket.side, ticket.limit_price_cents)
        pos = Position(
            mode="live", ticker=snap.ticker, event_ticker=snap.info.event_ticker, asset=snap.info.asset,
            label=snap.info.label, side=ticket.side, contracts=filled, entry_price=price, entry_fee=fee,
            risk_level=ticket.risk_level, confidence=ticket.confidence, close_time=snap.info.close_time,
            client_order_id=ticket.client_order_id,
        )
        return self.portfolio.add_open(pos)

    async def sell(self, pos: Position, snap: MarketSnapshot, reason: str = "manual exit",
                   *, confirmed: bool) -> float:
        self.assert_enabled()
        if not confirmed:
            raise LiveTradingDisabledError("live exits require explicit confirmation")
        bid = snap.exit_price(pos.side)
        if bid is None:
            raise OrderRejectedError("no bid available to exit")
        coid = f"{pos.client_order_id}-x"[:64]
        body = build_exit_order(ticker=pos.ticker, outcome_side=pos.side, contracts=pos.contracts,
                                limit_price_cents=bid, client_order_id=coid)
        resp = await self._submit(coid, pos.ticker, pos.side, "sell", pos.contracts, bid, body)
        filled, price, fee = self._fill(resp, pos.side, bid)
        if filled < pos.contracts:
            log_event(log, "LIVE_PARTIAL_EXIT", logging.WARNING, ticker=pos.ticker, filled=filled, qty=pos.contracts)
            pos.contracts -= filled
            self.portfolio.repo.save_position(pos)
            raise OrderRejectedError(f"partial exit: {filled}/{pos.contracts + filled} filled")
        return self.portfolio.close(pos, price, fee, reason)

    async def _submit(self, coid: str, ticker: str, side: str, action: str, qty: int,
                      price: float, body: dict[str, Any]) -> dict[str, Any]:
        repo = self.portfolio.repo
        repo.add_live_order(client_order_id=coid, ticker=ticker, side=side, action=action, contracts=qty,
                            price=price, status="submitting", request=body)
        log_event(log, "LIVE_ORDER_SUBMIT", ticker=ticker, action=f"{action.upper()}_{side.upper()}",
                  qty=qty, price=price / 100)
        try:
            resp = await self.client.create_order_v2(body)
        except KalshiError as exc:
            repo.update_live_order(coid, status="error", error=str(exc)[:500])
            log_event(log, "LIVE_ORDER_ERROR", logging.ERROR, ticker=ticker, error=exc)
            raise OrderRejectedError(f"exchange rejected order: {exc}") from exc
        repo.update_live_order(coid, status="accepted", exchange_order_id=str(resp.get("order_id", "")),
                               response=resp)
        return resp

    def _fill(self, resp: dict[str, Any], side: str, limit_cents: float) -> tuple[int, float, float]:
        filled = int(to_float(resp.get("fill_count")) or 0)
        if filled <= 0:
            raise OrderRejectedError("IOC order was not filled")
        # V2 prices are quoted from the YES book; convert to the outcome side we hold.
        avg_yes = dollars_to_cents(resp.get("average_fill_price"))
        if avg_yes is None:
            price = limit_cents
        else:
            price = avg_yes if side == "yes" else round(100 - avg_yes, 4)
        fee_each = to_float(resp.get("average_fee_paid")) or 0.0
        return filled, price, round(fee_each * filled, 2)
