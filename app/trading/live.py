"""Live execution against Kalshi - behind strict safety gates.

A live order is only ever submitted when ALL of the following hold:
  * LIVE_TRADING=true AND PAPER_TRADING=false (Settings.live_allowed)
  * Kalshi credentials are loaded (signed client)
  * the request comes from an authorised admin (enforced in the Telegram layer)
  * the user explicitly confirmed this ticket (or, for auto-trades,
    ALLOW_LIVE_AUTOTRADE=true as well)
  * the RiskManager approved it (enforced by TradeExecutor right before submit)

Order lifecycle (persisted in ``live_orders`` BEFORE anything is sent):
  submitting -> accepted -> filled | partial | no_fill        (normal IOC outcome)
  submitting -> unknown  -> reconciled | not_placed           (timeout / 5xx / unreadable reply)
  submitting -> error                                         (exchange said no)
An accepted order is never assumed filled: quantity, price and fees are read back
from Kalshi's own fills (GET /portfolio/fills?order_id=...), falling back to the
order object, and only then to the POST reply. An order is never resubmitted
after an uncertain outcome; it is looked up by its client_order_id instead.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

from app.config import Settings
from app.errors import (
    KalshiError,
    KalshiTransientError,
    LiveTradingDisabledError,
    MalformedResponseError,
    OrderRejectedError,
    TradingError,
)
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
        fill = await self._place(ticket.client_order_id, ticket.ticker, ticket.side, "buy",
                                 ticket.contracts, ticket.limit_price_cents, body)
        if fill.contracts <= 0:
            raise OrderRejectedError("IOC order was not filled")
        if fill.contracts < ticket.contracts:
            log_event(log, "LIVE_PARTIAL_ENTRY", logging.WARNING, ticker=ticket.ticker, filled=fill.contracts,
                      qty=ticket.contracts)
        pos = Position(
            mode="live", ticker=snap.ticker, event_ticker=snap.info.event_ticker, asset=snap.info.asset,
            label=snap.info.label, side=ticket.side, contracts=fill.contracts, entry_price=fill.price,
            entry_fee=fill.fee, risk_level=ticket.risk_level, confidence=ticket.confidence,
            close_time=snap.info.close_time, client_order_id=ticket.client_order_id,
        )
        pos = self.portfolio.add_open(pos)
        self.portfolio.repo.update_live_order(ticket.client_order_id, position_id=pos.id)
        return pos

    async def sell(self, pos: Position, snap: MarketSnapshot, reason: str = "manual exit",
                   *, confirmed: bool) -> float:
        self.assert_enabled()
        if not confirmed:
            raise LiveTradingDisabledError("live exits require explicit confirmation")
        bid = snap.exit_price(pos.side)
        if bid is None:
            raise OrderRejectedError("no bid available to exit")
        coid = exit_order_id(pos.client_order_id)  # a NEW id for every exit attempt (ids are unique)
        body = build_exit_order(ticker=pos.ticker, outcome_side=pos.side, contracts=pos.contracts,
                                limit_price_cents=bid, client_order_id=coid)
        fill = await self._place(coid, pos.ticker, pos.side, "sell", pos.contracts, bid, body, position_id=pos.id)
        if fill.contracts <= 0:
            raise OrderRejectedError("exit order was not filled (no buyer at the bid)")
        if fill.contracts < pos.contracts:
            total = pos.contracts
            pnl = self.portfolio.close_partial(pos, fill.contracts, fill.price, fill.fee, reason)
            log_event(log, "LIVE_PARTIAL_EXIT", logging.WARNING, ticker=pos.ticker, filled=fill.contracts,
                      qty=total, pnl=pnl)
            raise OrderRejectedError(f"partial exit: {fill.contracts}/{total} sold (P&L {pnl:+.2f} booked); "
                                     f"{pos.contracts} still open")
        return self.portfolio.close(pos, fill.price, fill.fee, reason)

    # ---------------------------------------------------------------- core
    async def _place(self, coid: str, ticker: str, side: str, action: str, qty: int, price: float,
                     body: dict[str, Any], position_id: int | None = None) -> Fill:
        repo = self.portfolio.repo
        repo.add_live_order(client_order_id=coid, ticker=ticker, side=side, action=action, contracts=qty,
                            price=price, status="submitting", request=body, position_id=position_id)
        log_event(log, "LIVE_ORDER_SUBMIT", ticker=ticker, action=f"{action.upper()}_{side.upper()}",
                  qty=qty, price=price / 100)
        sent_at = int(time.time()) - 120
        try:
            resp = await self.client.create_order_v2(body)
        except (KalshiTransientError, MalformedResponseError) as exc:
            # Timeout / connection loss / 5xx / unreadable 2xx: the order MAY have reached the exchange.
            # Never resubmit; look it up by client_order_id instead.
            repo.update_live_order(coid, status="unknown", error=str(exc)[:500])
            log_event(log, "LIVE_ORDER_UNKNOWN", logging.ERROR, ticker=ticker, error=exc)
            order = await self._find_order(coid, ticker, sent_at)
            repo.update_live_order(coid, status="reconciled", exchange_order_id=str(order.get("order_id", "")),
                                   response=order)
            return await self._confirm(coid, str(order.get("order_id", "")), side, price, fallback=order)
        except KalshiError as exc:
            repo.update_live_order(coid, status="error", error=str(exc)[:500])
            log_event(log, "LIVE_ORDER_ERROR", logging.ERROR, ticker=ticker, error=exc)
            raise OrderRejectedError(f"exchange rejected order: {exc}") from exc
        oid = str(resp.get("order_id", ""))
        repo.update_live_order(coid, status="accepted", exchange_order_id=oid, response=resp)
        return await self._confirm(coid, oid, side, price, fallback=resp)

    async def _find_order(self, coid: str, ticker: str, since_ts: int, attempts: int = 4) -> dict[str, Any]:
        """Locate an order with an unknown outcome. Concludes 'not placed' only after several successful
        lookups (all pages) spread over a few seconds; if Kalshi cannot be reached, the market is blocked."""
        repo = self.portfolio.repo
        clean_misses = 0
        for i in range(attempts):
            if i:
                await asyncio.sleep(self.retry_delay * i)
            try:
                orders = await self.client.get_orders_all(ticker, min_ts=since_ts)
            except KalshiError:
                continue
            match = next((o for o in orders if o.get("client_order_id") == coid), None)
            if match is not None:
                log_event(log, "LIVE_ORDER_FOUND", ticker=ticker, status=match.get("status"))
                return match
            clean_misses += 1
        if clean_misses >= 2:
            repo.update_live_order(coid, status="not_placed")
            raise OrderRejectedError("order outcome was unknown; verified it was NOT placed at Kalshi")
        self.portfolio.unresolved.add(ticker)  # blocks new orders on this market until resolved
        raise TradingError(f"order status UNKNOWN for {ticker}: new orders on this market are blocked; "
                           "check Kalshi before trading it again")

    async def _confirm(self, coid: str, order_id: str, side: str, limit_cents: float,
                       fallback: dict[str, Any]) -> Fill:
        """Quantity, average price (for the side we hold) and fees, from Kalshi's own records."""
        fill: Fill | None = None
        if order_id:
            for i in range(3):
                try:
                    fill = fill_from_fills(await self.client.get_fills(order_id=order_id), side)
                    if fill is None or fill.contracts == 0:  # fills can lag the order by a moment
                        fill = fill_from_order(await self.client.get_order(order_id), side) or fill
                    break
                except KalshiError as exc:
                    log_event(log, "LIVE_FILL_LOOKUP_FAILED", logging.WARNING, order_id=order_id, error=exc)
                    await asyncio.sleep(self.retry_delay * (i + 1))
        source = "exchange"
        if fill is None:
            fill = (fill_from_order(fallback, side) if "fill_count_fp" in fallback  # an order object
                    else None) or fill_from_create_response(fallback, side, limit_cents)
            source = "order reply"
        status = "no_fill" if fill.contracts <= 0 else "filled" if fill.complete else "partial"
        self.portfolio.repo.update_live_order(coid, status=status, fee=fill.fee,
                                              error=None if source == "exchange" else "fills not confirmed")
        log_event(log, "LIVE_ORDER_RESULT", order_id=order_id, status=status, filled=fill.contracts,
                  price=fill.price, fee=fill.fee, source=source)
        return fill

    retry_delay = 1.0


@dataclass(frozen=True)
class Fill:
    contracts: int
    price: float  # cents, average, for the outcome side held
    fee: float  # USD, total
    complete: bool = True


def exit_order_id(entry_coid: str) -> str:
    suffix = "-x" + uuid.uuid4().hex[:8]
    return entry_coid[: 64 - len(suffix)] + suffix


def _cents(v: Any) -> float | None:
    return dollars_to_cents(v)


def fill_from_fills(fills: list[dict[str, Any]], side: str) -> Fill | None:
    """Aggregate Kalshi fills of one order. Prices are read for the outcome side we hold."""
    if not fills:
        return None
    qty = cost = fee = 0.0
    for f in fills:
        n = to_float(f.get("count_fp", f.get("count"))) or 0.0
        p = _cents(f.get(f"{side}_price_dollars", f.get(f"{side}_price")))
        if p is None:
            yes = _cents(f.get("yes_price_dollars"))
            p = None if yes is None else (yes if side == "yes" else 100 - yes)
        if n <= 0 or p is None:
            continue
        qty += n
        cost += n * p
        fee += to_float(f.get("fee_cost")) or 0.0
    if qty <= 0:
        return Fill(0, 0.0, 0.0, False)
    return Fill(int(qty), round(cost / qty, 4), round(fee, 2))


def fill_from_order(order: dict[str, Any], side: str) -> Fill | None:
    """From the order object: fill_count_fp, fill cost and fees (taker + maker)."""
    n = to_float(order.get("fill_count_fp"))
    if n is None:
        return None
    if n <= 0:
        return Fill(0, 0.0, 0.0, False)
    cost = (to_float(order.get("taker_fill_cost_dollars")) or 0.0) + (to_float(order.get("maker_fill_cost_dollars")) or 0.0)
    fees = (to_float(order.get("taker_fees_dollars")) or 0.0) + (to_float(order.get("maker_fees_dollars")) or 0.0)
    if cost > 0:
        price = round(cost / n * 100, 4)
    else:
        limit = _cents(order.get(f"{side}_price_dollars"))
        price = limit if limit is not None else 0.0
    remaining = to_float(order.get("remaining_count_fp", order.get("remaining_count"))) or 0.0
    return Fill(int(n), price, round(fees, 2), complete=remaining <= 0)


def fill_from_create_response(resp: dict[str, Any], side: str, limit_cents: float) -> Fill:
    """Last resort: the POST reply's fill_count / average_fill_price (YES book) / average_fee_paid."""
    filled = int(to_float(resp.get("fill_count")) or 0)
    avg_yes = _cents(resp.get("average_fill_price"))
    price = limit_cents if avg_yes is None else (avg_yes if side == "yes" else round(100 - avg_yes, 4))
    fee_each = to_float(resp.get("average_fee_paid")) or 0.0
    remaining = to_float(resp.get("remaining_count")) or 0.0
    return Fill(filled, price, round(fee_each * filled, 2), complete=remaining <= 0)
