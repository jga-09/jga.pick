"""Build Kalshi V2 order requests (POST /portfolio/events/orders).

V2 orders are quoted from the YES book: ``side='bid'`` buys YES at ``price``;
``side='ask'`` sells YES at ``price`` which is economically buying NO at
``1 - price``. Prices are fixed-point dollar strings; counts are fixed-point
contract strings.
"""

from __future__ import annotations

from typing import Any


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
    if outcome_side == "yes":
        book_side, yes_price = "bid", limit_price_cents
    else:
        book_side, yes_price = "ask", 100 - limit_price_cents
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
    if outcome_side == "yes":  # sell YES
        book_side, yes_price = "ask", limit_price_cents
    else:  # sell NO == buy YES at 1 - no_price
        book_side, yes_price = "bid", 100 - limit_price_cents
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
