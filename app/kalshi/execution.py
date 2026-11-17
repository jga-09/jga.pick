"""Build Kalshi V2 order requests (POST /portfolio/events/orders).

V2 orders are quoted from the YES book: ``side='bid'`` buys YES at ``price``;
``side='ask'`` sells YES at ``price`` which is economically buying NO at
``1 - price``. Prices are fixed-point dollar strings; counts are fixed-point
contract strings.
"""

from __future__ import annotations

from typing import Any


import math

# Kalshi "tapered_deci_cent" price structure (KX...15M crypto): 0.1c steps below 10c and above 90c, whole cents
# between. Orders at any other price are rejected, so every limit price is snapped to a valid tick.
TAPERED_DECI_CENT: tuple[tuple[float, float, float], ...] = ((0.0, 10.0, 0.1), (10.0, 90.0, 1.0), (90.0, 100.0, 0.1))


def tick_at(price_cents: float, ranges: tuple[tuple[float, float, float], ...] = TAPERED_DECI_CENT) -> float:
    for start, end, step in ranges:
        if start <= price_cents < end or (price_cents == end == 100.0):
            return step
    return 1.0


def snap_price(price_cents: float, direction: str,
               ranges: tuple[tuple[float, float, float], ...] = TAPERED_DECI_CENT) -> float:
    """Round a YES price (cents) to a valid tick: 'down' never pays more, 'up' never accepts less."""
    step = tick_at(price_cents, ranges)
    units = price_cents / step
    units = math.floor(units + 1e-9) if direction == "down" else math.ceil(units - 1e-9)
    snapped = round(units * step, 4)
    # crossing a range edge can land on a coarser grid: re-check once with that range's step
    step2 = tick_at(snapped, ranges)
    if step2 != step:
        units = snapped / step2
        units = math.floor(units + 1e-9) if direction == "down" else math.ceil(units - 1e-9)
        snapped = round(units * step2, 4)
    return snapped


def _dollars(cents: float) -> str:
    return f"{cents / 100:.4f}"


def build_entry_order(
    *, ticker: str, outcome_side: str, contracts: int, limit_price_cents: float, client_order_id: str
) -> dict[str, Any]:
    """Immediate-or-cancel limit order buying ``outcome_side`` at no worse than the limit."""
    if outcome_side not in ("yes", "no"):
        raise ValueError("outcome_side must be 'yes' or 'no'")
    if contracts < 1:
        raise ValueError("contracts must be >= 1")
    if not 1 <= limit_price_cents <= 99:
        raise ValueError("limit price must be within 1..99 cents")
    if outcome_side == "yes":  # buy YES: never pay more than the limit
        book_side, yes_price = "bid", snap_price(limit_price_cents, "down")
    else:  # buy NO at <= limit  <=>  sell YES at >= 100 - limit
        book_side, yes_price = "ask", snap_price(100 - limit_price_cents, "up")
    return {
        "ticker": ticker,
        "client_order_id": client_order_id,
        "side": book_side,
        "count": f"{contracts}.00",
        "price": _dollars(yes_price),
        "time_in_force": "immediate_or_cancel",
        "self_trade_prevention_type": "taker_at_cross",
        "cancel_order_on_pause": True,
    }


def build_exit_order(
    *, ticker: str, outcome_side: str, contracts: int, limit_price_cents: float, client_order_id: str
) -> dict[str, Any]:
    """Reduce-only IOC order closing an existing ``outcome_side`` position."""
    if outcome_side == "yes":  # sell YES at >= the limit
        book_side, yes_price = "ask", snap_price(limit_price_cents, "up")
    else:  # sell NO at >= limit  <=>  buy YES at <= 100 - limit
        book_side, yes_price = "bid", snap_price(100 - limit_price_cents, "down")
    return {
        "ticker": ticker,
        "client_order_id": client_order_id,
        "side": book_side,
        "count": f"{contracts}.00",
        "price": _dollars(yes_price),
        "time_in_force": "immediate_or_cancel",
        "self_trade_prevention_type": "taker_at_cross",
        "reduce_only": True,
    }
